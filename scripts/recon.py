"""对账命令行：`python scripts/recon.py [--biz-date 2025-01-01] [--project-id 1]`

为什么 worker 有常驻进程、对账却用命令行：
  对账是**日终跑一次**的批处理，不是常驻服务。cron / 调度系统里挂一条命令最简单，
  失败了看退出码、看打印出来的差异单，不需要额外的服务发现和监控探针。

退出码：0 无差异（或交互模式）；1 有差异且加了 `--strict`（给调度系统当告警用）。
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys
from typing import Optional

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.service.recon import list_discrepancies, run_recon   # noqa: E402

# 差异会直接打印在下面，不需要 logger 再复述一遍（LOG 里重复一遍只会让人以为跑了两遍）
logging.getLogger("tdp.recon").setLevel(logging.CRITICAL)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="日终对账：明细 vs 汇总 vs 失败是否提单")
    ap.add_argument("--biz-date", default=None, help="只对这一天（YYYY-MM-DD）")
    ap.add_argument("--project-id", type=int, default=None)
    ap.add_argument("--strict", action="store_true", help="有差异时以退出码 1 结束")
    args = ap.parse_args(argv)

    scope = []
    if args.biz_date:
        scope.append(f"业务日 {args.biz_date}")
    if args.project_id:
        scope.append(f"项目 {args.project_id}")
    print("=" * 74)
    print("日终对账 · " + ("、".join(scope) if scope else "全量重对"))
    print("=" * 74)

    result = run_recon(biz_date=args.biz_date, project_id=args.project_id)
    print(f"\n检查了 {result['pairs_checked']} 个 (日, 项目) 组合，"
          f"其中 {result['pairs_with_diff']} 个有差异，共 {result['total']} 条差异\n")

    if result["total"]:
        header = f"  {'业务日':<12} {'项目':>6}  {'类型':<20} {'应有':>5} {'实际':>5}  说明"
        print(header)
        print("  " + "-" * (len(header) + 10))
        for item in result["items"]:
            print(f"  {item['biz_date']:<12} {item['project_id']:>6}  "
                  f"{item['kind']:<20} {item['expected']:>5} {item['actual']:>5}  "
                  f"{item['label']}")
        print("\n  差异单已写入 recon_discrepancies（同一 (日,项目,类型) 只会有一行）")

        open_items = list_discrepancies(resolved=False, limit=200)["items"]
        if open_items:
            print(f"  当前未解决差异共 {len(open_items)} 条")
    else:
        print("  没有差异：明细、汇总、质量规则三方一致 ✅")

    print("\n" + "=" * 74)
    if result["total"] and args.strict:
        print("  结论：有差异（--strict 模式下退出码 1）")
        print("=" * 74)
        return 1
    print(f"  结论：{'有差异，需要处理' if result['total'] else '通过'}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
