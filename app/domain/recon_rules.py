"""对账规则：**纯函数**，把"三方数据"翻译成差异单（技术方案 §4.6）。

三方分别是：

  ① 明细侧：`executions` 按 (日, 项目, 版本) 聚合
  ② 汇总侧：`daily_exec_summary`
  ③ 质量侧：`result='fail'` 的执行**必须**关联一条问题单（`defect_links` 或 `defects.execution_id`）

为什么"质量规则"也算对账：
数字全对、但失败没提单，报表看起来一片绿，实际质量事故被漏掉了。
这条检查的不是数据一致性，而是**流程有没有被遵守**——它比数字核对更容易被漏，也更有价值。

差异种类（对应 `recon_discrepancies.kind`）：

| kind | 含义 | 业务后果 |
| --- | --- | --- |
| `missing_summary` | 有明细没汇总 | 少算 |
| `extra_summary` | 有汇总没明细 | 多算 / 脏数据 |
| `summary_mismatch` | 数字对不上 | 日报口径错，向上汇报失真 |
| `fail_without_defect` | 失败但没提单 | **质量问题被漏掉** |

两个刻意的口径，写在这里防止以后"顺手改了"：

1. **`total` 含 `skip`**。`daily_exec_summary` 没有 `skipped` 列，所以
   `passed + failed + blocked <= total` 是正常的（差值就是 skip 数）。
   比对时只比这四列，**不要**试图让它们相加相等——否则每天每个 skip 都会变成一条假差异。
2. **按 (日, 项目, 版本) 对齐，而不是按执行 id 对齐**。
   对账要回答的是"这一天的日报数字对不对"，不是"这两条记录是不是同一条"。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Dict, Iterable, List, Tuple

KIND_MISSING_SUMMARY = "missing_summary"
KIND_EXTRA_SUMMARY = "extra_summary"
KIND_SUMMARY_MISMATCH = "summary_mismatch"
KIND_FAIL_WITHOUT_DEFECT = "fail_without_defect"

KINDS: Tuple[str, ...] = (
    KIND_MISSING_SUMMARY, KIND_EXTRA_SUMMARY,
    KIND_SUMMARY_MISMATCH, KIND_FAIL_WITHOUT_DEFECT,
)

KIND_LABEL = {
    KIND_MISSING_SUMMARY: "有明细无汇总（少算）",
    KIND_EXTRA_SUMMARY: "有汇总无明细（多算）",
    KIND_SUMMARY_MISMATCH: "汇总与明细数字不一致",
    KIND_FAIL_WITHOUT_DEFECT: "执行失败但未提问题单",
}

# 汇总与明细要逐列比对的字段。注意没有 skipped —— 表里就没这一列。
COUNT_FIELDS: Tuple[str, ...] = ("total", "passed", "failed", "blocked")

__all__ = ["COUNT_FIELDS", "Discrepancy", "KINDS", "KIND_EXTRA_SUMMARY",
           "KIND_FAIL_WITHOUT_DEFECT", "KIND_LABEL", "KIND_MISSING_SUMMARY",
           "KIND_SUMMARY_MISMATCH", "counts_of", "reconcile"]


@dataclass(frozen=True)
class Discrepancy:
    """一条差异：`expected` 是"应该是什么"，`actual` 是"现在是什么"。"""

    kind: str
    expected: int
    actual: int
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> Dict[str, Any]:
        return {"kind": self.kind, "expected": self.expected,
                "actual": self.actual, "detail": self.detail}


def counts_of(row: Dict[str, Any]) -> Dict[str, int]:
    """把一行聚合结果归一成四个整数（缺字段按 0，避免 None 参与比较）。"""
    return {name: int(row.get(name) or 0) for name in COUNT_FIELDS}


def reconcile(*, detail: Iterable[Dict[str, Any]],
              summary: Iterable[Dict[str, Any]],
              fails: Iterable[Dict[str, Any]]) -> Dict[Tuple[date_cls, int], List[Discrepancy]]:
    """三方比对，返回 `{(biz_date, project_id): [Discrepancy, ...]}`。

    入参约定（字段名固定，方便 service 层直接喂 SQL 结果）：
      detail  : biz_date, project_id, version_id, total, passed, failed, blocked
      summary : biz_date, project_id, version_id, total, passed, failed, blocked
      fails   : biz_date, project_id, execution_id（**没有**关联问题单的失败执行）

    返回里**只包含有差异的 (日, 项目)**；没有差异的组合不出现（否则会写一堆空行）。
    """
    d_by_key: Dict[Tuple[date_cls, int, int], Dict[str, int]] = {}
    for row in detail:
        key = (row["biz_date"], int(row["project_id"]), int(row["version_id"]))
        d_by_key[key] = counts_of(row)

    s_by_key: Dict[Tuple[date_cls, int, int], Dict[str, int]] = {}
    for row in summary:
        key = (row["biz_date"], int(row["project_id"]), int(row["version_id"]))
        s_by_key[key] = counts_of(row)

    missing_keys = sorted(set(d_by_key) - set(s_by_key))
    extra_keys = sorted(set(s_by_key) - set(d_by_key))
    common_keys = sorted(set(d_by_key) & set(s_by_key))

    out: Dict[Tuple[date_cls, int], List[Discrepancy]] = defaultdict(list)

    # ① 有明细没汇总（少算）：把同一天多个版本的明细汇总成一条差异
    grouped: Dict[Tuple[date_cls, int], Dict[str, Any]] = defaultdict(
        lambda: {"expected": 0, "versions": []})
    for biz, pid, vid in missing_keys:
        bucket = grouped[(biz, pid)]
        bucket["expected"] += d_by_key[(biz, pid, vid)]["total"]
        bucket["versions"].append(vid)
    for (biz, pid), bucket in sorted(grouped.items()):
        out[(biz, pid)].append(Discrepancy(
            kind=KIND_MISSING_SUMMARY,
            expected=int(bucket["expected"]), actual=0,
            detail={"version_ids": bucket["versions"],
                    "hint": "明细有执行记录，但汇总表没有这一行（worker 没跑 / 消息丢了）"},
        ))

    # ② 有汇总没明细（多算/脏数据）
    grouped_extra: Dict[Tuple[date_cls, int], Dict[str, Any]] = defaultdict(
        lambda: {"actual": 0, "versions": []})
    for biz, pid, vid in extra_keys:
        bucket = grouped_extra[(biz, pid)]
        bucket["actual"] += s_by_key[(biz, pid, vid)]["total"]
        bucket["versions"].append(vid)
    for (biz, pid), bucket in sorted(grouped_extra.items()):
        out[(biz, pid)].append(Discrepancy(
            kind=KIND_EXTRA_SUMMARY,
            expected=0, actual=int(bucket["actual"]),
            detail={"version_ids": bucket["versions"],
                    "hint": "汇总表有这一行，但明细查不到对应执行（脏数据或明细被删）"},
        ))

    # ③ 数字对不上：逐列给出明细值 vs 汇总值，方便一眼看出哪一列错了
    grouped_mismatch: Dict[Tuple[date_cls, int], Dict[str, Any]] = defaultdict(
        lambda: {"expected": 0, "actual": 0, "fields": [], "versions": []})
    for key in common_keys:
        d, s = d_by_key[key], s_by_key[key]
        if d == s:
            continue
        biz, pid, vid = key
        bucket = grouped_mismatch[(biz, pid)]
        bucket["expected"] += d["total"]
        bucket["actual"] += s["total"]
        bucket["versions"].append(vid)
        bucket["fields"].append({
            "version_id": vid,
            "detail": d, "summary": s,
            "diff": {name: s[name] - d[name] for name in COUNT_FIELDS if s[name] != d[name]},
        })
    for (biz, pid), bucket in sorted(grouped_mismatch.items()):
        out[(biz, pid)].append(Discrepancy(
            kind=KIND_SUMMARY_MISMATCH,
            expected=int(bucket["expected"]), actual=int(bucket["actual"]),
            detail={"version_ids": bucket["versions"], "fields": bucket["fields"],
                    "hint": "汇总 != 明细聚合；先看 diff 里哪一列差，再查 worker 口径"},
        ))

    # ④ 失败但没提单：expected = 失败执行数，actual = 已提单的失败执行数（这里恒为 0，
    #    因为入参 fails 只给"没关联单据"的那些；保留 expected/actual 两个字段是为了
    #    让差异单上"应有多少、实际多少"的语义统一）
    grouped_fail: Dict[Tuple[date_cls, int], Dict[str, Any]] = defaultdict(
        lambda: {"ids": [], "expected": 0})
    for row in fails:
        bucket = grouped_fail[(row["biz_date"], int(row["project_id"]))]
        bucket["ids"].append(int(row["execution_id"]))
        bucket["expected"] += 1
    for (biz, pid), bucket in sorted(grouped_fail.items()):
        out[(biz, pid)].append(Discrepancy(
            kind=KIND_FAIL_WITHOUT_DEFECT,
            expected=int(bucket["expected"]), actual=0,
            detail={"execution_ids": sorted(bucket["ids"])[:50],
                    "truncated": len(bucket["ids"]) > 50,
                    "hint": "执行失败但没有关联问题单（defect_links / defects.execution_id）"},
        ))

    return dict(out)
