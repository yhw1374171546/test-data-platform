"""测试夹具：**跑真库**（本机绿色版 PG，端口 5433），用完清理。

为什么不在 SQLite 上跑：
这个项目全部技术点都依赖 PG 的特性——唯一约束、部分唯一索引 `WHERE status='active'`、
`(created_at, id)` 行值比较、`ON CONFLICT DO NOTHING`、以及最关键的
**被阻塞 UPDATE 重新求值谓词**（"恰好一人领到"就靠它）。换成 SQLite 测，
等于把被测对象替换掉了，测出来的"通过"没有意义。

DB 连不上时**跳过**而不是失败：开发机上没起 PG 时不该把 `pytest` 变成一片红。

命名约定：所有造出来的数据都带一个 8 位随机 tag，
清理时按 **id + 前缀** 双保险删，既不会误伤 bench 数据，也不会留下垃圾。
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List

import pytest
from sqlalchemy import text

from app.domain.biz_time import TZ
from app.infra.db import session_scope

# 每个测试进程一个独立前缀：清理时只删自己造的幂等键，不误伤 bench 数据
KEY_PREFIX = f"t{uuid.uuid4().hex[:6]}-"


def idem_key() -> str:
    """测试专用幂等键：自带唯一性，且能被统一清理。"""
    return f"{KEY_PREFIX}{uuid.uuid4().hex}"


@pytest.fixture()
def idem():
    """把 idem_key 做成夹具，测试里 `idem()` 拿一个新键。"""
    return idem_key


@pytest.fixture(scope="session")
def db() -> None:
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception as exc:                      # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用（{type(exc).__name__}），跳过集成测试")


@pytest.fixture()
def refs(db: None) -> Iterator[Dict[str, Any]]:
    """一套干净的主数据：项目 / 车辆 / 测试员 / 用例 / 版本。"""
    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        project_id = s.execute(text("""
            INSERT INTO projects (code, name, model)
            VALUES (:c, '单测项目', 'U8') RETURNING id
        """), {"c": f"TEST-{tag}"}).scalar_one()
        vehicle_id = s.execute(text("""
            INSERT INTO vehicles (vin, model) VALUES (:v, 'U8') RETURNING id
        """), {"v": f"VIN{tag.upper()}"}).scalar_one()
        tester_id = s.execute(text("""
            INSERT INTO testers (employee_no, name) VALUES (:e, '单测员') RETURNING id
        """), {"e": f"E{tag}"}).scalar_one()
        case_id = s.execute(text("""
            INSERT INTO test_cases (case_no, module, title)
            VALUES (:c, '领航', '单测用例') RETURNING id
        """), {"c": f"TC-{tag}"}).scalar_one()
        version_id = s.execute(text("""
            INSERT INTO versions (model, ver) VALUES ('U8', :v) RETURNING id
        """), {"v": f"V-{tag}"}).scalar_one()

    data: Dict[str, Any] = {
        "tag": tag,
        "project_id": int(project_id),
        "vehicle_id": int(vehicle_id),
        "tester_id": int(tester_id),
        "case_id": int(case_id),
        "version_id": int(version_id),
        "vin": f"VIN{tag.upper()}",
    }

    yield data

    _cleanup(data)


def _cleanup(refs: Dict[str, Any]) -> None:
    """按外键依赖倒序删除；额外造的数据用前缀兜底，避免遗漏。"""
    tag = refs["tag"]
    pid, vid = refs["project_id"], refs["vehicle_id"]
    statements = [
        ("DELETE FROM defect_transitions WHERE defect_id IN "
         "(SELECT id FROM defects WHERE project_id = :pid)", {"pid": pid}),
        ("DELETE FROM defect_links WHERE defect_id IN "
         "(SELECT id FROM defects WHERE project_id = :pid)", {"pid": pid}),
        ("DELETE FROM defects WHERE project_id = :pid", {"pid": pid}),
        ("DELETE FROM executions WHERE queue_id IN "
         "(SELECT id FROM execution_queue WHERE project_id = :pid)", {"pid": pid}),
        ("DELETE FROM execution_queue WHERE project_id = :pid", {"pid": pid}),
        # outbox / 汇总要在 versions、projects 之前清：
        # 消息里带着 project_id，汇总表还有 project_id / version_id 两个外键
        ("DELETE FROM outbox WHERE payload ->> 'project_id' = :pid_like",
         {"pid_like": str(pid)}),
        ("DELETE FROM daily_exec_summary WHERE project_id = :pid", {"pid": pid}),
        ("DELETE FROM recon_discrepancies WHERE project_id = :pid", {"pid": pid}),
        ("DELETE FROM bookings WHERE project_id = :pid", {"pid": pid}),
        ("DELETE FROM vehicle_slots WHERE vehicle_id = :vid", {"vid": vid}),
        ("DELETE FROM vehicles WHERE id = :vid", {"vid": vid}),
        ("DELETE FROM test_cases WHERE case_no LIKE :case_like",
         {"case_like": f"TC-{tag}%"}),
        ("DELETE FROM testers WHERE employee_no LIKE :tester_like",
         {"tester_like": f"E{tag}%"}),
        ("DELETE FROM versions WHERE model = 'U8' AND ver LIKE :ver_like",
         {"ver_like": f"V-{tag}%"}),
        ("DELETE FROM projects WHERE id = :pid", {"pid": pid}),
        ("DELETE FROM idempotency_keys WHERE key LIKE :key_like",
         {"key_like": f"{KEY_PREFIX}%"}),
    ]
    with session_scope() as s:
        for sql, params in statements:
            s.execute(text(sql), params)


# ────────────────────────── 造数据的小工厂 ──────────────────────────
class Biz:
    """一个测试专属的业务日（UTC+8 的某一天）。

    起点故意取在过去（2024 年）：`submit_execution` 会拒绝"超过一天以后的未来"，
    拿 2030 年当测试日期会直接 400 —— 那个校验本身是对的，别绕过它。
    每个测试一个独立日期，配合 `biz_date=` 过滤，可以和数据里其它积压完全隔离。
    """

    def __init__(self) -> None:
        self.day: date = date(2024, 1, 1) + timedelta(days=uuid.uuid4().int % 700)

    @property
    def iso(self) -> str:
        return self.day.isoformat()

    def at(self, hour: int = 10) -> datetime:
        """当天本地 hour 点的时刻（用来做补录时间）。"""
        return datetime.combine(self.day, time(hour), tzinfo=TZ).astimezone(timezone.utc)

    def plus(self, days: int) -> "Biz":
        other = Biz.__new__(Biz)
        other.day = self.day + timedelta(days=days)
        return other


@pytest.fixture()
def biz() -> Biz:
    return Biz()


@pytest.fixture()
def make_task(refs: Dict[str, Any]) -> Callable[..., int]:
    """造一条 pending 执行任务（用例本身也一起造，避免 UNIQUE(project,version,case) 冲突）。"""

    def _make(title: str = "队列用例", status: str = "pending") -> int:
        tag = uuid.uuid4().hex[:6]
        with session_scope() as s:
            case_id = s.execute(text("""
                INSERT INTO test_cases (case_no, module, title)
                VALUES (:c, '领航', :t) RETURNING id
            """), {"c": f"TC-{refs['tag']}-{tag}", "t": title}).scalar_one()
            return int(s.execute(text("""
                INSERT INTO execution_queue (project_id, version_id, case_id, status)
                VALUES (:p, :v, :c, :s) RETURNING id
            """), {"p": refs["project_id"], "v": refs["version_id"],
                   "c": case_id, "s": status}).scalar_one())

    return _make


@pytest.fixture()
def make_testers(refs: Dict[str, Any]) -> Callable[..., List[int]]:
    """批量造测试员（并发实验要 20 个人同时抢）。"""

    def _make(count: int) -> List[int]:
        tag = uuid.uuid4().hex[:6]
        ids: List[int] = []
        with session_scope() as s:
            for i in range(count):
                ids.append(int(s.execute(text("""
                    INSERT INTO testers (employee_no, name, team)
                    VALUES (:e, :n, '并发组') RETURNING id
                """), {"e": f"E{refs['tag']}-{tag}-{i}",
                       "n": f"并发员{i}"}).scalar_one()))
        return ids

    return _make


# ────────────────────────── HTTP 客户端（带 / 不带 token）──────────────────────────
# 认证默认是开的（TDP_AUTH=on），所以业务用例的客户端必须带 token，否则全是 401。
# 这里直接签发 token 而不是走 /v1/auth/token：省一次 DB 往返，也让"认证测试"和
# "业务测试"解耦 —— 登录流程本身由 tests/test_auth.py 单独验。
def _make_client(role: str | None):
    from fastapi.testclient import TestClient

    from app.infra.auth import issue_token
    from app.main import create_app

    headers = {}
    if role:
        token, _ = issue_token(f"pytest-{role}", role, ttl_seconds=600)
        headers["Authorization"] = f"Bearer {token}"
    # raise_server_exceptions=False：让 500 也走中间件，才能真正断言错误体形状
    return TestClient(create_app(), raise_server_exceptions=False, headers=headers)


@pytest.fixture()
def client(refs: Dict[str, Any]):
    """默认客户端：admin 身份（绝大多数用例只关心业务行为）。"""
    with _make_client("admin") as c:
        yield c


@pytest.fixture()
def tester_client(refs: Dict[str, Any]):
    with _make_client("tester") as c:
        yield c


@pytest.fixture()
def viewer_client(refs: Dict[str, Any]):
    with _make_client("viewer") as c:
        yield c


@pytest.fixture()
def anon_client(refs: Dict[str, Any]):
    """不带 token 的客户端，用来验 401 / 限流（限流按来源分桶，匿名更干净）。"""
    with _make_client(None) as c:
        yield c
