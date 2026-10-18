"""幂等键：**唯一可信的防线是数据库唯一约束**（技术方案 §三 约定 + §4.2）。

为什么不能用进程内 dict：
多进程 / 多副本部署时每个进程各存一份，"同 key 20 并发只生效 1 次"立刻失效。
所以 `idempotency_keys(scope, key)` 的 UNIQUE 必须在库里，并且：

  · **与业务写入同一事务**——先占键、再干活、最后把首次响应写回同一事务；
  · 业务失败 → 整个事务回滚 → 键一起消失 → 客户端重试仍能再抢（这是有意的：
    "因为档期被占而失败"不属于幂等结果，重试是有意义的）；
  · 同 key 换 body → 返回 `body_mismatch`，由接入层给 422——
    **绝不能当重放返回旧结果**，否则调用方会以为改动生效了。

并发时序（这是本条技术点的全部要害）：

    T1: INSERT ... ON CONFLICT DO NOTHING -> 拿到 id   （占键成功）
    T2: INSERT ... ON CONFLICT DO NOTHING -> 无返回     （撞唯一索引，阻塞到 T1 提交）
    T1: 业务写入 + UPDATE 幂等键 response -> COMMIT
    T2: 回查 response -> 原样返回首次结果（不重复产生副作用）

注意 T2 回查必须在 T1 提交之后：PG 的 ON CONFLICT 会等 T1 结束，
拿到"冲突"结论时 T1 已经提交，所以能读到完整 response。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# 占键时先写 status_code=0 / response={}，表示"已占位但还没有结论"。
# 这样如果进程在业务执行中途崩掉，事务回滚会把这个占位一起带走，不会留下"半个幂等键"。
_PENDING_CODE = 0


@dataclass(frozen=True)
class Replay:
    """一次重放：调用方直接返回首次响应，不再执行业务。"""

    status_code: int
    response: Dict[str, Any] = field(default_factory=dict)
    body_mismatch: bool = False


def body_hash_of(**fields: Any) -> str:
    """请求体指纹：用于识别"同 key 换 body"。

    sort_keys 保证字段顺序不影响结果；default=str 让 date/datetime 也能进指纹。
    """
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim(session: Session, *, scope: str, key: str, body_hash: str) -> Optional[Replay]:
    """尝试占幂等键。

    返回 None          → 本次是首次请求，继续执行业务；
    返回 Replay        → 重放（`body_mismatch=True` 表示同 key 换了 body，应回 422）。

    注意：本函数**不 commit**，占键行必须与业务写入同生共死。
    """
    if not key:
        raise ValueError("幂等键不能为空")

    inserted = session.execute(text("""
        INSERT INTO idempotency_keys (scope, key, body_hash, status_code, response)
        VALUES (:scope, :key, :bh, :code, CAST('{}' AS JSONB))
        ON CONFLICT (scope, key) DO NOTHING
        RETURNING id
    """), {"scope": scope, "key": key, "bh": body_hash, "code": _PENDING_CODE}).first()

    if inserted is not None:
        return None

    # 并发/重试路径：回查首次结果
    row = session.execute(text("""
        SELECT body_hash, status_code, response
          FROM idempotency_keys
         WHERE scope = :scope AND key = :key
    """), {"scope": scope, "key": key}).one()

    if row.body_hash != body_hash:
        return Replay(status_code=422, body_mismatch=True,
                      response={"reason": "same idempotency key, different body"})

    response = row.response or {}
    # status_code=0 说明首次请求还没得出结论（理论上只会出现在"读已提交"的极端时序上），
    # 保守按 200 返回，让调用方按 response 内容判断，避免把内部占位值泄漏出去。
    return Replay(status_code=row.status_code or 200, response=dict(response))


def store(session: Session, *, scope: str, key: str,
          status_code: int, response: Dict[str, Any]) -> None:
    """把首次响应写回幂等键（同一事务）。重放时客户端拿到的就是这份 JSON。"""
    session.execute(text("""
        UPDATE idempotency_keys
           SET status_code = :code, response = CAST(:resp AS JSONB)
         WHERE scope = :scope AND key = :key
    """), {
        "code": int(status_code),
        "resp": json.dumps(response, ensure_ascii=False, default=str),
        "scope": scope,
        "key": key,
    })
