"""HTTP 层压测：把同一场竞争**走真实 HTTP** 再测一遍（技术方案 §4.1 的补充）。

为什么需要这个实验：
其余 8 个 bench 都是直接调 service 层函数的，测出来的 P99 **不含** uvicorn 的解析、
JSON 序列化、ASGI 中间件和 socket 往返。真实用户感知的是 HTTP 那一层，
所以这里补上：同一台机器、同样的并发，service 层 vs HTTP 层各测一次，
差值就是"框架 + 网络"的固定开销。

用法：
    python bench/http_load.py                    # 自己起 uvicorn（随机空闲端口）
    python bench/http_load.py --base-url http://127.0.0.1:8000   # 打已有服务
"""
from __future__ import annotations

import argparse
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import httpx                                                 # noqa: E402
from sqlalchemy import text                                  # noqa: E402

from app.infra.db import session_scope                       # noqa: E402
from app.service.booking import SlotNotAvailable, create_booking  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed(day: date) -> Dict[str, int]:
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        pid = int(s.execute(text("""
            INSERT INTO projects (code, name, model) VALUES (:c, 'HTTP 压测项目', 'U8')
            RETURNING id
        """), {"c": f"BENCH-HTTP-{tag}"}).scalar_one())
        vid = int(s.execute(text("""
            INSERT INTO vehicles (vin, model) VALUES (:v, 'U8') RETURNING id
        """), {"v": f"VINHTTP{tag.upper()}"}).scalar_one())
        tid = int(s.execute(text("""
            INSERT INTO testers (employee_no, name) VALUES (:e, 'HTTP 压测员') RETURNING id
        """), {"e": f"EHTTP{tag}"}).scalar_one())
        s.execute(text("""
            INSERT INTO vehicle_slots (vehicle_id, slot_date, half, status)
            VALUES (:v, :d, 0, 'free')
        """), {"v": vid, "d": day})
    return {"project_id": pid, "vehicle_id": vid, "tester_id": tid}


def percentile(values: List[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


_LOG_HANDLES: List[Any] = []      # 保持日志文件句柄存活，进程结束再统一关


def start_server() -> Tuple[subprocess.Popen, str]:
    port = free_port()
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    # 压测要绕过限流：默认配额 600/分钟，而这里一次跑 600+ 请求，
    # 不放开的话测出来的是"限流器多快"，不是"接口多快"（第一版就踩了：读全是 429）。
    env["TDP_RATE_LIMIT"] = os.environ.get("TDP_HTTP_BENCH_RATE_LIMIT", "1000000")
    # 日志必须落到文件，不能用 PIPE：PIPE 没人读，缓冲区（约 64KB）写满后
    # uvicorn 会阻塞在写日志上，请求直接超时 —— 这个坑排查起来像"服务卡死"。
    log_path = Path(tempfile.gettempdir()) / f"tdp-http-load-{port}.log"
    log_file = log_path.open("w+", encoding="utf-8")
    _LOG_HANDLES.append((log_file, log_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE), env=env,
        stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                return proc, base_url
        except Exception:                              # noqa: BLE001 - 还没起来就重试
            time.sleep(0.2)
        if proc.poll() is not None:
            log_file.flush()
            log_file.seek(0)
            raise RuntimeError(f"uvicorn 启动失败：{log_file.read()[-1500:]}")
    raise RuntimeError("uvicorn 30s 内没起来")


def login(base_url: str, username: str, password: str) -> str:
    """接口默认要认证；没有 token 的压测全是 401，数字毫无意义。"""
    resp = httpx.post(f"{base_url}/v1/auth/token", timeout=10,
                      json={"username": username, "password": password})
    if resp.status_code != 200:
        raise SystemExit(f"登录失败（{resp.status_code}）：{resp.text[:200]}\n"
                         f"  先跑 `dev.cmd init` 灌演示账号，或用 --username/--password 指定")
    return f"Bearer {resp.json()['access_token']}"


def http_race(base_url: str, env: Dict[str, int], day: date,
              concurrency: int, token: str = "") -> Dict[str, Any]:
    """N 个线程同时 POST /v1/bookings 抢同一个档期。"""
    latencies: List[float] = []
    codes: Dict[str, int] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency, timeout=60)

    def worker() -> None:
        # trust_env=False：别让系统的 HTTP_PROXY 把本地压测导到代理上
        with httpx.Client(timeout=30, trust_env=False) as client:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            started = time.perf_counter()
            try:
                resp = client.post(
                    f"{base_url}/v1/bookings",
                    headers={"Idempotency-Key": f"http-bench-{uuid.uuid4().hex}",
                             **({"Authorization": token} if token else {})},
                    json={"project_id": env["project_id"], "vehicle_id": env["vehicle_id"],
                          "testers_id": env["tester_id"], "slot_date": str(day),
                          "half": 0},
                )
                if resp.status_code == 201:
                    tag = "created"
                elif resp.status_code == 409:
                    tag = f"409:{(resp.json() or {}).get('error', '?')}"
                else:
                    tag = f"HTTP {resp.status_code}"
            except Exception as exc:                   # noqa: BLE001
                tag = f"error:{type(exc).__name__}"
            elapsed = time.perf_counter() - started
            with lock:
                codes[tag] = codes.get(tag, 0) + 1
                latencies.append(elapsed)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    return {"codes": codes, "latencies": latencies, "wall": wall,
            "rps": concurrency / wall if wall else 0.0}


def service_race(env: Dict[str, int], day: date, concurrency: int) -> Dict[str, Any]:
    """同样的竞争，直接调 service 层（不经过 HTTP），用来算框架开销。"""
    latencies: List[float] = []
    codes: Dict[str, int] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(concurrency, timeout=60)

    def worker() -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        started = time.perf_counter()
        try:
            create_booking(idem_key=f"svc-bench-{uuid.uuid4().hex}",
                           project_id=env["project_id"], vehicle_id=env["vehicle_id"],
                           testers_id=env["tester_id"], slot_date=str(day), half=0)
            tag = "created"
        except SlotNotAvailable:
            tag = "slot_taken"
        except Exception as exc:                       # noqa: BLE001
            tag = f"error:{type(exc).__name__}"
        elapsed = time.perf_counter() - started
        with lock:
            codes[tag] = codes.get(tag, 0) + 1
            latencies.append(elapsed)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started

    return {"codes": codes, "latencies": latencies, "wall": wall,
            "rps": concurrency / wall if wall else 0.0}


def http_reads(base_url: str, vin: str, total: int, concurrency: int,
               token: str = "") -> Dict[str, Any]:
    """预热后并发读车辆详情：验证缓存命中 + 量一下 HTTP 读的 P99。"""
    warm = httpx.get(
        f"{base_url}/v1/vehicles/{vin}", timeout=10,
        headers={"Authorization": token} if token else {}, )
    latencies: List[float] = []
    cache_headers: Dict[str, int] = {}
    errors = 0
    lock = threading.Lock()
    per_thread = max(1, total // concurrency)

    def worker() -> None:
        nonlocal errors
        local_lat: List[float] = []
        local_cache: Dict[str, int] = {}
        bad = 0
        client_headers = {"Authorization": token} if token else {}
        with httpx.Client(timeout=30, trust_env=False,
                          headers=client_headers) as client:
            for _ in range(per_thread):
                started = time.perf_counter()
                try:
                    resp = client.get(f"{base_url}/v1/vehicles/{vin}")
                    local_lat.append(time.perf_counter() - started)
                    if resp.status_code != 200:
                        bad += 1
                        continue
                    key = resp.headers.get("X-Cache", "-")
                    local_cache[key] = local_cache.get(key, 0) + 1
                except Exception:                      # noqa: BLE001
                    bad += 1
        with lock:
            latencies.extend(local_lat)
            errors += bad
            for k, v in local_cache.items():
                cache_headers[k] = cache_headers.get(k, 0) + v

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - started
    return {"warm_status": warm.status_code, "warm_cache": warm.headers.get("X-Cache"),
            "latencies": latencies, "cache": cache_headers, "errors": errors,
            "wall": wall, "rps": len(latencies) / wall if wall else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=50, help="抢档期的并发线程数")
    ap.add_argument("--reads", type=int, default=2000, help="缓存读的总次数")
    ap.add_argument("--base-url", default=None, help="打已有服务；不传就自己起 uvicorn")
    ap.add_argument("--username", default=os.environ.get("TDP_SMOKE_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("TDP_SMOKE_PASSWORD", "Admin@12345"))
    args = ap.parse_args()

    print("=" * 78)
    print(f"HTTP 层压测 · 写并发 {args.concurrency} · 读 {args.reads} 次")
    print("=" * 78)

    proc: Optional[subprocess.Popen] = None
    try:
        if args.base_url:
            base_url = args.base_url.rstrip("/")
            print(f"目标：已有服务 {base_url}")
        else:
            proc, base_url = start_server()
            print(f"已启动临时 uvicorn：{base_url}")

        token = login(base_url, args.username, args.password)
        print(f"已登录：{args.username}")

        day = date(2032, 3, 1)

        # ── 写：同一档期 N 并发 ──
        env = seed(day)
        svc = service_race(env, day, args.concurrency)
        print(f"\n[service 层] 同一档期 {args.concurrency} 并发：{svc['codes']}  "
              f"耗时 {svc['wall']:.2f}s")

        http_day = date(2032, 3, 2)
        env_http = seed(http_day)
        http = http_race(base_url, env_http, http_day, args.concurrency, token)
        print(f"[HTTP 层]    同一档期 {args.concurrency} 并发：{http['codes']}  "
              f"耗时 {http['wall']:.2f}s  {http['rps']:.0f} req/s")

        # ── 读：缓存命中 ──
        print(f"\n[HTTP 读]    并发 {min(args.concurrency, 50)} 个线程 × "
              f"{args.reads // min(args.concurrency, 50)} 次")
        # 真实 vin 从库里拿（seed 用了随机后缀）
        with session_scope() as s:
            vin = str(s.execute(text(
                "SELECT vin FROM vehicles WHERE id = :v"),
                {"v": env_http["vehicle_id"]}).scalar_one())
        reads = http_reads(base_url, vin, args.reads, min(args.concurrency, 50), token)

        print("\n" + "=" * 78)
        print("结果")
        print("=" * 78)
        print(f"  {'场景':<22}{'P50':>9}{'P95':>9}{'P99':>9}{'max':>9}{'吞吐':>12}")
        for name, data in (("写·service 层", svc), ("写·HTTP 层", http),
                           ("读·HTTP（缓存命中）", reads)):
            lat = data["latencies"]
            if not lat:
                continue
            print(f"  {name:<22}{statistics.median(lat) * 1000:>8.1f}ms"
                  f"{percentile(lat, 0.95) * 1000:>8.1f}ms"
                  f"{percentile(lat, 0.99) * 1000:>8.1f}ms"
                  f"{max(lat) * 1000:>8.1f}ms"
                  f"{data['rps']:>9.0f}/s")

        overhead = (percentile(http["latencies"], 0.99)
                    - percentile(svc["latencies"], 0.99)) * 1000
        print(f"\n  HTTP 相对 service 层的 P99 增量：{overhead:+.1f}ms"
              f"（uvicorn 解析 + 序列化 + socket 往返）")

        print("\n" + "=" * 78)
        print("校验")
        print("=" * 78)
        failed = 0
        created_svc = svc["codes"].get("created", 0)
        created_http = http["codes"].get("created", 0)
        rejected_http = sum(v for k, v in http["codes"].items() if k.startswith("409:"))
        errs = sum(v for k, v in http["codes"].items() if not k.startswith(("created", "409:")))
        hit = reads["cache"].get("hit", 0)
        miss_other = sum(v for k, v in reads["cache"].items() if k != "hit")

        checks = [
            ("service 层：恰好 1 人成功", created_svc == 1, f"created={created_svc}"),
            ("HTTP 层：恰好 1 人成功（并发语义没被框架破坏）",
             created_http == 1, f"created={created_http}"),
            ("HTTP 层：其余全部 409 slot_not_available",
             rejected_http == args.concurrency - created_http,
             f"409={rejected_http} / 其他={errs}"),
            ("HTTP 层没有 5xx 或异常", errs == 0, f"异常={errs}"),
            ("缓存读全部命中（预热后 X-Cache 应恒为 hit）",
             hit == len(reads["latencies"]) and miss_other == 0,
             f"{reads['cache']}"),
            ("缓存读没有被卡住（P99 < 500ms 的健全性检查）",
             percentile(reads["latencies"], 0.99) < 0.5,
             f"p99={percentile(reads['latencies'], 0.99) * 1000:.1f}ms"),
        ]
        for label, ok, detail in checks:
            print(f"  [{'OK' if ok else 'FAIL'}] {label}：{detail}")
            failed += 0 if ok else 1

        print("\n  读法（数字背后的两件事）：")
        print("  · 写场景的 P99 大头是**数据库行锁排队**（所有请求抢同一行），不是框架开销；")
        print("    HTTP 与 service 的差值才是框架的固定成本。")
        print("  · 这个差值偏大是**两级排队**造成的，不是 uvicorn 慢：")
        print("    同步 endpoint 由 anyio 默认 40 个线程执行，而 DB 连接池也是 40 条，")
        print("    50 个并发请求要在「线程池」和「连接池」各排一次队。")
        print("    生产上要么调大 --limit-concurrency / 线程池，要么起多个 worker 进程。")
        print("  · 读场景走缓存、不碰 DB，所以它的 P99 才是比较干净的框架成本；")
        print("    绝对值和机器的核数、GIL 调度强相关，别当基准线用。")

        print("\n" + "=" * 78)
        print(f"  结论：{'全部通过' if failed == 0 else f'{failed} 项未通过'}")
        print("=" * 78)
        return 0 if failed == 0 else 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
