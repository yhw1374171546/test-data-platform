"""提交执行结果：**一个事务里写四张表**（技术方案 §4.4）。

一次提交做四件事，缺一不可：

    ① execution_queue: claimed/running → done（条件 UPDATE，防重复提交/防顶替）
    ② executions:       写真实结果（含补录时间 executed_at）
    ③ outbox:           topic='exec_result_recorded'（**与 ② 同一事务**）
    ④ result='fail' 且调用方带了缺陷 → defects + defect_links + 「(new)→open」审计

为什么 ③ 必须和 ② 同一事务：
第 3 周的 worker 靠 outbox 驱动 `daily_exec_summary`。如果先提交 executions、
再单独写 outbox，中间崩了就是"明细有、汇总永远没有"——对账会报 `missing_summary`，
但谁也补不回来（明细已经落库，没人知道它该不该被汇总）。
同一事务里要么两条都在、要么都不在，worker 才能把"最终一致"收敛住。

为什么不在这个接口里直接 upsert daily_exec_summary：
汇总表是 (日期, 项目, 版本) 粒度的聚合写入，放进同一个事务会拉长事务、放大行锁竞争，
而且一次提交要再做一次聚合读。用 outbox 换"最终一致"，代价是汇总短暂延迟——
这正是技术方案 §4.4 想讲的取舍。

关于"失败没提单"：
`defect` 是**可选**的。调用方不带，就真的只记一条 fail 执行、不建缺陷——
这不是漏了，而是留给第 3 周的对账去发现（`fail_without_defect`）。
把"必须提单"写成接口层的强制校验，会让"提报失败"变成"结果提交失败"，
反而丢掉了更重要的执行明细。业务硬规则用对账兜，不用接口兜。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import text

from app.domain.biz_time import ensure_aware, now as utc_now, to_biz_date
from app.domain.queue_state import OWNED
from app.infra.db import session_scope
from app.infra.idempotency import body_hash_of, claim, store
from app.service.booking import RefNotFound
from app.service.claim import QueueConflict, QueueTaskNotFound, ensure_tester
from app.service.defect import insert_defect

SUBMIT_SCOPE = "submit_result"
OUTBOX_TOPIC = "exec_result_recorded"

# 与 executions.result 的取值保持一致。用元组而不是 Enum：
# 数据库里就是 TEXT，多一层类型转换只会让"写入非法值"的报错更难读。
RESULT_VALUES = ("pass", "fail", "blocked", "skip")


@dataclass
class ExecutionResult:
    ok: bool
    code: str                        # recorded | replayed | body_mismatch
    execution_id: Optional[int] = None
    task_id: Optional[int] = None
    result: str = ""
    biz_date: Optional[date_cls] = None
    executed_at: Optional[datetime] = None
    defect_no: Optional[str] = None
    status_code: int = 201
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def replay(self) -> bool:
        return self.code == "replayed"


def submit_execution(*, idem_key: str, task_id: int, tester_id: int, result: str,
                     note: str = "", vehicle_id: Optional[int] = None,
                     executed_at: Optional[datetime] = None,
                     defect: Optional[Dict[str, Any]] = None) -> ExecutionResult:
    """提交一条执行结果。

    `executed_at` 允许客户端传：真实场景里测试员常是**跑完再补录**，
    提交时刻和执行时刻不是一回事。不传则取当前时间。
    """
    if not idem_key:
        raise ValueError("submit_execution 必须带幂等键")

    result = str(result or "").strip().lower()
    if result not in RESULT_VALUES:
        raise ValueError(f"result 只能是 {'/'.join(RESULT_VALUES)}，收到 {result!r}")

    note = (note or "").strip()
    moment = _check_executed_at(executed_at)
    defect_payload = _normalize_defect(defect)

    # 幂等指纹必须算**请求体**，不能算服务端派生值：
    # executed_at 缺省时会被补成 now()，若拿 moment 参与哈希，
    # 同一个请求每次重试的时间戳都不同 → 被判成"同 key 换 body"（422）。
    # 这个 bug 是写完测试才暴露的，注释留在这里防止以后再犯。
    body_hash = body_hash_of(
        task_id=task_id, tester_id=tester_id, result=result, note=note,
        vehicle_id=vehicle_id,
        executed_at=executed_at.isoformat() if executed_at else None,
        defect=defect_payload)

    with session_scope() as s:
        # ── ① 幂等键：同 key 重放返回同一个 execution_id（这次是真的同一个 id）──
        replay = claim(s, scope=SUBMIT_SCOPE, key=idem_key, body_hash=body_hash)
        if replay is not None:
            if replay.body_mismatch:
                return ExecutionResult(False, "body_mismatch", task_id=task_id,
                                       result=result, status_code=422,
                                       detail=dict(replay.response))
            payload = replay.response
            return ExecutionResult(True, "replayed",
                                   execution_id=payload.get("execution_id"),
                                   task_id=payload.get("task_id"),
                                   result=payload.get("result", result),
                                   biz_date=_parse_date(payload.get("biz_date")),
                                   executed_at=_parse_dt(payload.get("executed_at")),
                                   defect_no=payload.get("defect_no"),
                                   status_code=replay.status_code)

        ensure_tester(s, tester_id)
        _ensure_vehicle(s, vehicle_id)

        row = s.execute(text("""
            SELECT id, project_id, version_id, case_id, status, claimed_by
              FROM execution_queue WHERE id = :id
        """), {"id": task_id}).first()
        if row is None:
            raise QueueTaskNotFound(task_id)

        # ── ② 前置语义校验：给调用方一个"该做什么"的明确答案 ──
        # 顺序刻意是"先看状态、再看归属"：任务还是 pending 时 claimed_by 为 NULL，
        # 先判归属会返回 not_claim_owner，让调用方误以为要换人。
        status = str(row.status).strip().lower()
        if status == "pending":
            raise QueueConflict("not_claimed", {
                "task_id": task_id, "status": status,
                "hint": "任务还没被领取，先 POST /v1/queue/claim 再提交结果",
            })
        if status == "done":
            raise QueueConflict("already_submitted", {
                "task_id": task_id, "status": status,
                "hint": "该任务已提交过结果；换 key 重复提交不受理，要重跑请新建任务",
            })
        if status == "cancelled":
            raise QueueConflict("not_submittable", {
                "task_id": task_id, "status": status,
                "hint": "任务已取消，不能再提交结果",
            })
        if int(row.claimed_by or 0) != int(tester_id):
            raise QueueConflict("not_claim_owner", {
                "task_id": task_id, "claimed_by": row.claimed_by,
                "status": status,
                "hint": "只有领取人可以提交结果",
            })

        # ── ③ 条件 UPDATE：把任务推到 done（并发下只有一次成功）──
        owned = list(sorted(OWNED))
        moved = s.execute(text("""
            UPDATE execution_queue
               SET status = 'done', version = version + 1
             WHERE id = :id AND status = ANY(CAST(:owned AS TEXT[]))
               AND claimed_by = :t
            RETURNING version
        """), {"id": task_id, "owned": owned, "t": tester_id}).first()

        if moved is None:
            # affected=0：状态或归属在读取之后被改过，重读给准确结论
            latest = s.execute(text("""
                SELECT status, claimed_by FROM execution_queue WHERE id = :id
            """), {"id": task_id}).one()
            code = ("already_submitted" if latest.status == "done"
                    else "not_claim_owner" if int(latest.claimed_by or 0) != int(tester_id)
                    else "state_conflict")
            raise QueueConflict(code, {"task_id": task_id, "status": latest.status,
                                       "hint": "任务状态在提交过程中被并发修改，请重新读取"})

        # ── ④ executions ──
        execution_id = s.execute(text("""
            INSERT INTO executions (queue_id, vehicle_id, tester_id, result, note, executed_at)
            VALUES (:q, :v, :t, :r, :note, :at)
            RETURNING id
        """), {"q": task_id, "v": vehicle_id, "t": tester_id, "r": result,
               "note": note, "at": moment}).scalar_one()

        # ── ⑤ 失败带出缺陷（同一事务）──
        defect_no: Optional[str] = None
        if defect_payload:
            defect_id, defect_no = insert_defect(
                s,
                title=defect_payload["title"],
                severity=defect_payload["severity"],
                project_id=int(row.project_id),
                actor=f"tester:{tester_id}",
                owner_id=defect_payload.get("owner_id"),
                execution_id=int(execution_id),
            )
            # defect_links 同时写一条：defects.execution_id 是"来源"，
            # defect_links 是"复现关联"的集合。第 3 周对账只查 defect_links 就能覆盖两者，
            # ON CONFLICT 保证重复写不会炸。
            s.execute(text("""
                INSERT INTO defect_links (defect_id, execution_id)
                VALUES (:d, :e)
                ON CONFLICT (defect_id, execution_id) DO NOTHING
            """), {"d": defect_id, "e": int(execution_id)})

        # ── ⑥ outbox：与上面全部写在同一个事务里 ──
        biz_date = to_biz_date(moment)
        s.execute(text("""
            INSERT INTO outbox (topic, payload)
            VALUES (:topic, CAST(:payload AS JSONB))
        """), {"topic": OUTBOX_TOPIC, "payload": json.dumps({
            "execution_id": int(execution_id),
            "queue_id": task_id,
            "project_id": int(row.project_id),
            "version_id": int(row.version_id),
            "case_id": int(row.case_id),
            "tester_id": tester_id,
            "result": result,
            "biz_date": biz_date.isoformat(),
            "executed_at": moment.isoformat(),
        }, ensure_ascii=False)})

        # ── ⑦ 幂等键记下首次结果（同一事务）──
        store(s, scope=SUBMIT_SCOPE, key=idem_key, status_code=201, response={
            "execution_id": int(execution_id), "task_id": task_id,
            "result": result, "biz_date": biz_date.isoformat(),
            "executed_at": moment.isoformat(), "defect_no": defect_no,
        })

    return ExecutionResult(True, "recorded", execution_id=int(execution_id),
                           task_id=task_id, result=result, biz_date=biz_date,
                           executed_at=moment, defect_no=defect_no,
                           status_code=201, detail={"version": int(moved.version)})


# ────────────────────────── 内部 ──────────────────────────
def _check_executed_at(executed_at: Optional[datetime]) -> datetime:
    """补录时间：允许过去任意时间，但不接受"超过一天以后的未来"（时钟歪了/写错了）。"""
    moment = ensure_aware(executed_at) if executed_at else utc_now()
    if moment > utc_now() + timedelta(days=1):
        raise ValueError(f"executed_at 不能超过当前时间一天以上：{moment.isoformat()}")
    return moment


def _normalize_defect(defect: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """缺陷载荷归一：只有带了 title 才算"要提单"，避免空字典造出一条空缺陷。"""
    if not defect:
        return None
    title = str(defect.get("title") or "").strip()
    if not title:
        raise ValueError("defect.title 不能为空（要么不带 defect，要么带完整字段）")
    severity = str(defect.get("severity") or "").strip().upper()
    if not severity:
        raise ValueError("defect.severity 不能为空")
    normalized: Dict[str, Any] = {"title": title, "severity": severity}
    owner_id = defect.get("owner_id")
    if owner_id is not None:
        normalized["owner_id"] = int(owner_id)
    return normalized


def _ensure_vehicle(session: Any, vehicle_id: Optional[int]) -> None:
    if vehicle_id is None:
        return
    if session.execute(text("SELECT 1 FROM vehicles WHERE id = :i"),
                       {"i": vehicle_id}).first() is None:
        raise RefNotFound("vehicle", vehicle_id)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_date(value: Any) -> Optional[date_cls]:
    if not value:
        return None
    try:
        return date_cls.fromisoformat(str(value))
    except ValueError:
        return None
