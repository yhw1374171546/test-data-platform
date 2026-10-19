"""上线前自检：把"公网上会出事"的默认值一条条查出来。

用法（在服务器上、compose 起来之后）：
    python scripts/prod_check.py --base-url http://127.0.0.1:8000

为什么要有这个脚本：
这个项目的开发默认值是「方便」优先 —— 默认 JWT 密钥、demo 口令、AUTH 可关。
这些默认值在本地是对的，但**一旦放公网就是事故**。靠人记着改不可靠，
所以把"必须改成什么"写成可执行的检查，部署流程里跑一遍。

退出码：0 = 全过（可能有警告）；1 = 有 FAIL，不要对外开。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEV_SECRET_HINT = "dev-only"
DEFAULT_ADMIN = ("admin", "Admin@12345")

Result = Tuple[str, bool, str]   # (检查项, 是否通过, 说明)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _http(url: str, *, method: str = "GET", body: Optional[Dict[str, Any]] = None,
          token: str = "", timeout: float = 5.0) -> Tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001 - 网络问题也算检查失败
        return 0, f"{type(exc).__name__}: {exc}"


def check_env() -> List[Result]:
    out: List[Result] = []

    secret = _env("TDP_JWT_SECRET")
    if not secret:
        out.append(("JWT 密钥", False, "TDP_JWT_SECRET 未设置 —— 会用开发默认密钥，任何人都能伪造 token"))
    elif secret in {"change-me"} or secret.startswith(("dev", "test")) or len(secret) < 32:
        out.append(("JWT 密钥", False, f"TDP_JWT_SECRET 太弱（{len(secret)} 位）：至少 32 位随机串，"
                                      f"可用 python -c \"import secrets;print(secrets.token_urlsafe(48))\""))
    else:
        out.append(("JWT 密钥", True, f"已设置（{len(secret)} 位）"))

    auth = _env("TDP_AUTH", "on").lower()
    out.append(("认证开关", auth not in {"off", "0", "false"},
                "TDP_AUTH=off 时匿名按 admin 放行，公网上绝不能用" if auth in {"off", "0", "false"}
                else "TDP_AUTH 为开启状态"))

    pg = _env("POSTGRES_PASSWORD")
    if not pg:
        out.append(("数据库口令", False, "POSTGRES_PASSWORD 未设置"))
    elif pg in {"postgres", "123456", "password", "tdp"} or len(pg) < 12:
        out.append(("数据库口令", False, "POSTGRES_PASSWORD 是弱口令"))
    else:
        out.append(("数据库口令", True, f"已设置（{len(pg)} 位）"))

    backend = _env("TDP_RATE_LIMIT_BACKEND", "auto")
    out.append(("限流后端", backend == "redis",
                "TDP_RATE_LIMIT_BACKEND 不是 redis：多副本各限各的，配额会放大" if backend != "redis"
                else "使用 Redis 共享配额"))

    demo_user = _env("TDP_DEMO_USER")
    demo_pwd = _env("TDP_DEMO_PASSWORD")
    if demo_user and not demo_pwd:
        out.append(("只读演示账号", False, "TDP_DEMO_USER 设了但 TDP_DEMO_PASSWORD 为空"))
    elif demo_user:
        out.append(("只读演示账号", True, f"将创建 viewer 账号 {demo_user!r}（只读）"))
    else:
        out.append(("只读演示账号", True, "未配置（线上建议配一个只读账号给面试官点）"))
    return out


def check_runtime(base: str) -> List[Result]:
    out: List[Result] = []

    status, body = _http(f"{base}/healthz")
    out.append(("服务存活 /healthz", status == 200, f"HTTP {status}"))

    status, body = _http(f"{base}/readyz")
    ok = status == 200 and isinstance(body, dict) and body.get("db") == "ok"
    out.append(("数据库就绪 /readyz", ok, f"HTTP {status} {str(body)[:80]}"))

    status, body = _http(f"{base}/ui/")
    out.append(("前端 /ui 可打开", status == 200 and "<div id=\"app\">" in str(body),
                f"HTTP {status}（镜像里没拷 web/dist 时这里会 404）"))

    # 匿名应该被挡：401 才算对，200 说明认证没生效
    status, body = _http(f"{base}/v1/queue?limit=1")
    out.append(("匿名访问被拦截", status == 401, f"HTTP {status}"))

    # 默认 admin 口令必须已经改掉：如果能登录成功，就是 FAIL
    user, pwd = DEFAULT_ADMIN
    status, body = _http(f"{base}/v1/auth/token", method="POST",
                         body={"username": user, "password": pwd})
    out.append(("默认 admin 口令已改", status != 200,
                "admin / Admin@12345 还能登录 —— 公网上必须改掉" if status == 200 else f"HTTP {status}"))

    # 只读账号只能读
    demo_user, demo_pwd = _env("TDP_DEMO_USER"), _env("TDP_DEMO_PASSWORD")
    if demo_user and demo_pwd:
        status, body = _http(f"{base}/v1/auth/token", method="POST",
                             body={"username": demo_user, "password": demo_pwd})
        if status == 200 and isinstance(body, dict):
            token = body.get("access_token", "")
            role = body.get("role", "")
            out.append(("演示账号是 viewer", role == "viewer", f"role={role}"))
            status_w, _ = _http(f"{base}/v1/bookings", method="POST", token=token,
                                body={"project_id": 1, "vehicle_id": 1, "testers_id": 1,
                                      "slot_date": "2000-01-01", "half": 0})
            out.append(("演示账号改不动数据", status_w in {401, 403}, f"写入返回 HTTP {status_w}"))
        else:
            out.append(("演示账号可登录", False, f"HTTP {status}"))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="上线前自检")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--env-only", action="store_true", help="只查环境变量（不连服务）")
    args = ap.parse_args(argv)

    checks = check_env()
    if not args.env_only:
        checks += check_runtime(args.base_url.rstrip("/"))

    failed = 0
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        failed += 0 if ok else 1
        print(f"[{mark}] {name}: {detail}")

    print()
    if failed:
        print(f"结论：{failed} 项不通过，先别对外开放。")
        print("需要新密钥时：python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
        return 1
    print("结论：全部通过，可以对外。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
