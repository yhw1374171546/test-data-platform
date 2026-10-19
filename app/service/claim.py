"""任务领取：**乐观锁抢占**（技术方案 §4.2）。

一条 UPDATE 定胜负：

    UPDATE execution_queue
       SET status='claimed', claimed_by=:t, claimed_at=now(), version=version+1
     WHERE id=:id AND status='pending' AND version=:v
    RETURNING version, claimed_at

为什么 `WHERE status='pending'` 就能保证"恰好一人领到"：
PG 在 READ COMMITTED 下对**被阻塞的 UPDATE 会重新求值谓词**（EvalPlanQual）——
后到的事务等到行锁醒来时，发现 status 已经被改成 claimed，affected=0。
如果写成"先 SELECT 判断 pending、再无脑 UPDATE"，20 个并发会全部成功。

那为什么还要 `version`？因为 `status='pending'` 防得住"同时抢"，防不住 ABA：

    领取(v1) → 释放(v2) → 再次领取(v3)

一个拿着 v1 的客户端，如果在"释放之后、再次领取之前"这个窗口里重放请求，
只看 status 会以为自己领到了同一份工作，而事实上任务已经被别人重新领走了。
`version` 让这种陈旧请求得到 409 `version_conflict`，而不是一个"看起来成功"的错结果。

affected=0 之后**要重读一次**才能给出正确语义，因为两种失败客户端该做的事完全不同：

| 重读结果 | 返回 | 客户端该做什么 |
| --- | --- | --- |
| 状态已不是 pending | 409 `already_claimed` | 换一条任务，或等对方释放 |
| 仍是 pending 但版本变了 | 409 `version_conflict` | 重读版本号后立即重试 |

幂等：同 key 同 body 重放返回**同一个领取结果**（同一个 task_id + version + claimed_at），
不再执行 UPDATE。技术方案原文写的是"返回同一个 execution_id"——`executions` 记录
要到提交结果时才产生（week 3 的 POST /v1/executions），所以这里返回的是同一个"占位"，
即同一次领取的版本号与时间戳；语义等价，代码注释在这里说明，避免以后对不上文档。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text

from app.domain.queue_state import (can_release, is_claimable,
                                    normalize_status)
from app.infra import cache
from app.infra.db import session_scope
from app.infra.idempotency import body_hash_of, claim, store
from app.service.booking import RefNotFound
from app.service.catalog import after_commit_invalidate, enqueue_invalidation

ENQUEUE_SCOPE = "enqueue_task"
CLAIM_SCOPE = "claim_task"
RELEASE_SCOPE = "release_task"


class QueueTaskNotFound(Exception):
    def __init__(self, task_id: int):
        self.task_id = task_id
        super().__init__(f"执行任务不存在：{task_id}")


class QueueConflict(Exception):
    """队列上的业务冲突（一律 409）。子类不单独定义，靠 `code` 区分，避免映射表爆炸。

    code 取值：
      already_claimed   任务已被别人领走
      version_conflict  状态仍是 pending，但版本号已变（ABA / 陈旧请求）
      not_claim_owner   不是领取人却想释放/推进
      not_releasable    当前状态不允许释放
    """

    def __init__(self, code: str, detail: Dict[str, Any]):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass
class ClaimResult:
    ok: bool
    code: str                        # claimed | replayed | body_mismatch
    task_id: Optional[int] = None
    tester_id: Optional[int] = None
    status: str = ""
    version: int = 0
    claimed_at: Optional[datetime] = None
    status_code: int = 200
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def replay(self) -> bool:
        return self.code == "replayed"


@dataclass
class ReleaseResult:
    ok: bool
    code: str                        # released | replayed | body_mismatch
    task_id: Optional[int] = None
    status: str = ""
    version: int = 0
    status_code: int = 200
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def replay(self) -> bool:
        return self.code == "replayed"


@dataclass
class EnqueueResult:
    ok: bool
    code: str                        # enqueued | replayed | body_mismatch
    task_id: Optional[int] = None
    case_id: Optional[int] = None
    status: str = "pending"
    version: int = 0
    status_code: int = 201
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def replay(self) -> bool:
        return self.code == "replayed"


# ────────────────────────── 排队 ──────────────────────────
def enqueue_task(*, idem_key: str, project_id: int, version_id: int,
                 case_no: str, case_title: str, module: str = "导入",
                 priority: str = "P2") -> EnqueueResult:
    """把一条用例排进任务池；用例不存在就先建。

    为什么需要这个接口（技术方案的接口清单里没有）：
    没有它，任务只能靠 `scripts/init_db.py` 灌种子数据——演示脚本跑几轮就把待领取的池子耗空了，
    而且真实平台的用例本来也是"从测试计划导入"进来的。它同时承担"导入一条用例"的职责：
    `case_no` 已存在时按 title/priority 更新（用例改了要能同步），不存在则新建。

    三处校验，都是业务正确性问题，不是防御性编程：
      1. 项目/版本必须存在（否则 FK 报 500）
      2. **版本必须属于该项目车型**：`versions.model = projects.model`。
         不校验的话，U8 的项目能排上别的车型的软件版本，跑出来的结果毫无意义。
      3. 同一 (项目, 版本, 用例) 只能有一条任务（表上的 UNIQUE 兜底）——
         重复排队返回 409 `task_exists` 并把已有 task_id 带回去，调用方可以直接用它。
    """
    if not idem_key:
        raise ValueError("enqueue_task 必须带幂等键")

    case_no = (case_no or "").strip()
    case_title = (case_title or "").strip()
    if not case_no or not case_title:
        raise ValueError("case_no / case_title 不能为空")

    body_hash = body_hash_of(project_id=project_id, version_id=version_id,
                             case_no=case_no, case_title=case_title,
                             module=module, priority=priority)

    with session_scope() as s:
        replay = claim(s, scope=ENQUEUE_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return EnqueueResult(False, "body_mismatch", status_code=422,
                                     detail=dict(replay.response))
            payload = replay.response
            return EnqueueResult(True, "replayed",
                                 task_id=payload.get("task_id"),
                                 case_id=payload.get("case_id"),
                                 status=payload.get("status", "pending"),
                                 version=int(payload.get("version") or 0),
                                 status_code=replay.status_code)

        project = s.execute(text("SELECT id, model FROM projects WHERE id = :i"),
                            {"i": project_id}).first()
        if project is None:
            raise RefNotFound("project", project_id)

        version = s.execute(text("SELECT id, model, ver FROM versions WHERE id = :i"),
                            {"i": version_id}).first()
        if version is None:
            raise RefNotFound("version", version_id)

        if str(project.model) != str(version.model):
            raise QueueConflict("model_mismatch", {
                "project_model": project.model,
                "version_model": version.model,
                "hint": "版本必须与项目车型一致，否则跑出来的结果没有意义",
            })

        # 用例 upsert：同一 case_no 视为同一条用例，改标题/优先级要能同步过来
        case_id = s.execute(text("""
            INSERT INTO test_cases (case_no, module, title, priority)
            VALUES (:no, :module, :title, :prio)
            ON CONFLICT (case_no) DO UPDATE
               SET title = EXCLUDED.title, priority = EXCLUDED.priority
            RETURNING id
        """), {"no": case_no, "module": module, "title": case_title,
               "prio": priority}).scalar_one()

        inserted = s.execute(text("""
            INSERT INTO execution_queue (project_id, version_id, case_id)
            VALUES (:p, :v, :c)
            ON CONFLICT (project_id, version_id, case_id) DO NOTHING
            RETURNING id
        """), {"p": project_id, "v": version_id, "c": case_id}).first()

        if inserted is None:
            existing = s.execute(text("""
                SELECT id, status FROM execution_queue
                 WHERE project_id = :p AND version_id = :v AND case_id = :c
            """), {"p": project_id, "v": version_id, "c": case_id}).one()
            raise QueueConflict("task_exists", {
                "task_id": int(existing.id),
                "status": existing.status,
                "hint": "该用例已在该项目/版本的任务池里，直接领它即可",
            })

        task_id = int(inserted.id)
        store(s, scope=ENQUEUE_SCOPE, key=idem_key, status_code=201, response={
            "task_id": task_id, "case_id": int(case_id),
            "status": "pending", "version": 0,
        })

        # 用例 upsert 可能改了 title/priority —— **用例详情缓存必须失效**。
        # 这是一条真实的跨模块失效路径（不是为实验造的），漏了就会一直看到旧标题。
        case_key = cache.key_case(case_no)
        enqueue_invalidation(s, [case_key], reason="case_updated")

    after_commit_invalidate(case_key, reason="case_updated")
    return EnqueueResult(True, "enqueued", task_id=task_id, case_id=int(case_id),
                         status="pending", version=0, status_code=201)


# ────────────────────────── 领取 ──────────────────────────
def claim_task(*, idem_key: str, task_id: int, tester_id: int,
               expect_version: Optional[int] = None) -> ClaimResult:
    """抢占一条执行任务。

    `expect_version` 可选：调用方带上自己看到的版本号就是**严格乐观锁**（推荐）；
    不带则由服务端在本事务内读一次当前版本再参与条件更新——同样能防 ABA，
    因为 version 只增不减，中间被改过就一定会对不上。
    """
    if not idem_key:
        raise ValueError("claim_task 必须带幂等键")

    body_hash = body_hash_of(task_id=task_id, tester_id=tester_id,
                             expect_version=expect_version)

    with session_scope() as s:
        # ── ① 先占幂等键：同 key 重放直接返回首次结果 ──
        replay = claim(s, scope=CLAIM_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return ClaimResult(False, "body_mismatch", task_id=task_id,
                                   status_code=422, detail=dict(replay.response))
            payload = replay.response
            return ClaimResult(True, "replayed",
                               task_id=payload.get("task_id"),
                               tester_id=payload.get("tester_id"),
                               status=payload.get("status", "claimed"),
                               version=int(payload.get("version") or 0),
                               claimed_at=_parse_dt(payload.get("claimed_at")),
                               status_code=replay.status_code)

        # ── ② 外键先校验：把 FK violation（500）变成 404 ──
        ensure_tester(s, tester_id)

        row = s.execute(text("""
            SELECT id, status, version, claimed_by
              FROM execution_queue WHERE id = :id
        """), {"id": task_id}).first()
        if row is None:
            raise QueueTaskNotFound(task_id)

        if not is_claimable(row.status):
            raise QueueConflict("already_claimed", _claim_conflict_detail(
                task_id, row.status, row.claimed_by, row.version))

        version = int(row.version) if expect_version is None else int(expect_version)

        # ── ③ 一条条件 UPDATE 定胜负 ──
        updated = s.execute(text("""
            UPDATE execution_queue
               SET status = 'claimed', claimed_by = :t, claimed_at = now(),
                   version = version + 1
             WHERE id = :id AND status = 'pending' AND version = :v
            RETURNING version, claimed_at
        """), {"id": task_id, "t": tester_id, "v": version}).first()

        if updated is None:
            # affected=0：重读一次，区分"被别人领走"和"版本过期"（见模块头部表）
            now = s.execute(text("""
                SELECT status, version, claimed_by FROM execution_queue WHERE id = :id
            """), {"id": task_id}).one()
            if not is_claimable(now.status):
                raise QueueConflict("already_claimed", _claim_conflict_detail(
                    task_id, now.status, now.claimed_by, now.version))
            raise QueueConflict("version_conflict", {
                "task_id": task_id,
                "expected_version": version,
                "actual_version": int(now.version),
                "hint": "任务仍待领取，但版本号已变（可能刚被别人领走又释放），请重读后重试",
            })

        claimed_at: datetime = updated.claimed_at
        new_version = int(updated.version)

        # ── ④ 幂等键记下首次结果（同一事务）──
        store(s, scope=CLAIM_SCOPE, key=idem_key, status_code=200, response={
            "task_id": task_id,
            "tester_id": tester_id,
            "status": "claimed",
            "version": new_version,
            "claimed_at": claimed_at.isoformat(),
        })

    return ClaimResult(True, "claimed", task_id=task_id, tester_id=tester_id,
                       status="claimed", version=new_version,
                       claimed_at=claimed_at, status_code=200)


# ────────────────────────── 释放 ──────────────────────────
def release_task(*, idem_key: str, task_id: int, tester_id: int) -> ReleaseResult:
    """把持有中的任务放回 `pending`（领错了/临时被叫走）。

    为什么释放一定要有：
    没有释放，"领取→释放→再领取"这条 ABA 路径就不存在，`version` 也就成了装饰品。
    有了释放，`version` 的价值可以当场演示：拿旧版本号重试会被 409 挡掉。

    释放必须由**领取人本人**发起：别人替你放掉，`claimed_by` 就成了不可信数据，
    第 3 周"谁跑的这条用例"直接失去意义。
    """
    if not idem_key:
        raise ValueError("release_task 必须带幂等键")

    body_hash = body_hash_of(task_id=task_id, tester_id=tester_id)

    with session_scope() as s:
        replay = claim(s, scope=RELEASE_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return ReleaseResult(False, "body_mismatch", task_id=task_id,
                                     status_code=422, detail=dict(replay.response))
            payload = replay.response
            return ReleaseResult(True, "replayed", task_id=payload.get("task_id"),
                                 status=payload.get("status", "pending"),
                                 version=int(payload.get("version") or 0),
                                 status_code=replay.status_code)

        row = s.execute(text("""
            SELECT id, status, version, claimed_by
              FROM execution_queue WHERE id = :id
        """), {"id": task_id}).first()
        if row is None:
            raise QueueTaskNotFound(task_id)

        if row.claimed_by is None or int(row.claimed_by) != int(tester_id):
            raise QueueConflict("not_claim_owner", {
                "task_id": task_id,
                "tester_id": tester_id,
                "claimed_by": row.claimed_by,
                "status": row.status,
                "hint": "只有领取人本人可以释放任务",
            })

        if not can_release(row.status):
            raise QueueConflict("not_releasable", {
                "task_id": task_id,
                "status": row.status,
                "hint": "只有 claimed / running 可以释放；done 要重跑请新建任务",
            })

        released = s.execute(text("""
            UPDATE execution_queue
               SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                   version = version + 1
             WHERE id = :id AND status IN ('claimed', 'running') AND claimed_by = :t
            RETURNING version
        """), {"id": task_id, "t": tester_id}).first()

        if released is None:
            # 并发：刚被别人改过（例如同一个人两次释放、或另一方向前进到 done）
            now = s.execute(text("""
                SELECT status, claimed_by, version FROM execution_queue WHERE id = :id
            """), {"id": task_id}).one()
            if now.claimed_by is None or int(now.claimed_by) != int(tester_id):
                raise QueueConflict("not_claim_owner", {
                    "task_id": task_id, "claimed_by": now.claimed_by, "status": now.status})
            raise QueueConflict("not_releasable", {"task_id": task_id, "status": now.status})

        new_version = int(released.version)
        store(s, scope=RELEASE_SCOPE, key=idem_key, status_code=200, response={
            "task_id": task_id, "status": "pending", "version": new_version,
        })

    return ReleaseResult(True, "released", task_id=task_id, status="pending",
                         version=new_version, status_code=200)


# ────────────────────────── 查询 ──────────────────────────
def list_queue(*, project_id: Optional[int] = None,
               project_ids: Optional[Set[int]] = None,
               status: Optional[str] = None,
               after_id: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
    """任务池列表（keyset 按 id 翻页）。

    队列的翻页天然简单：`id` 就是单调递增的序，`WHERE id > :after ORDER BY id` 直接命中主键，
    不需要像问题单那样拼 `(created_at, id)` 复合游标。

    `project_ids` 是 ABAC 的可见范围（`None`=管理员看全部；空集合=真的空页）。
    """
    limit = max(1, min(int(limit or 20), 100))
    where: List[str] = ["1 = 1"]
    params: Dict[str, Any] = {}

    if project_id is not None:
        where.append("q.project_id = :pid")
        params["pid"] = int(project_id)
    if project_ids is not None:
        if not project_ids:
            return {"items": [], "next_cursor": None, "limit": limit, "has_more": False}
        where.append("q.project_id = ANY(:pids)")
        params["pids"] = [int(p) for p in sorted(project_ids)]
    if status:
        normalized = normalize_status(status)
        where.append("q.status = :st")
        params["st"] = normalized
    if after_id is not None:
        where.append("q.id > :after")
        params["after"] = int(after_id)

    params["lim"] = limit + 1

    sql = f"""
        SELECT q.id, q.project_id, q.version_id, q.case_id, q.status,
               q.claimed_by, q.claimed_at, q.version,
               c.case_no, c.title, c.priority, v.ver
          FROM execution_queue q
          JOIN test_cases c ON c.id = q.case_id
          JOIN versions  v ON v.id = q.version_id
         WHERE {' AND '.join(where)}
         ORDER BY q.id
         LIMIT :lim
    """
    with session_scope() as s:
        rows = s.execute(text(sql), params).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_task_dict(r) for r in rows]
    next_cursor = str(int(rows[-1].id)) if has_more and rows else None

    return {"items": items, "next_cursor": next_cursor, "limit": limit,
            "has_more": has_more}


# ────────────────────────── 内部 ──────────────────────────
def _task_dict(row: Any) -> Dict[str, Any]:
    return {
        "task_id": int(row.id),
        "project_id": int(row.project_id),
        "version_id": int(row.version_id),
        "case_id": int(row.case_id),
        "case_no": row.case_no,
        "case_title": row.title,
        "priority": row.priority,
        "ver": row.ver,
        "status": row.status,
        "claimed_by": row.claimed_by,
        "claimed_at": row.claimed_at,
        "version": int(row.version),
    }


def _claim_conflict_detail(task_id: int, status: str, claimed_by: Any,
                          version: Any) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "status": status,
        "claimed_by": claimed_by,
        "version": int(version or 0),
        "hint": "任务已被领取；换一条任务，或等对方释放/完成后重试",
    }


def ensure_tester(session: Any, tester_id: int) -> None:
    """测试员是否存在。公开函数：提交执行结果那边也要用同一个 404 语义。"""
    if session.execute(text("SELECT 1 FROM testers WHERE id = :i"),
                       {"i": tester_id}).first() is None:
        raise RefNotFound("tester", tester_id)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
