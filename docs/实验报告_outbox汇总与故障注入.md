# 实验报告 · outbox 汇总与故障注入

> 目标：技术方案 §4.4 —— **"写 executions + 写 outbox 同一事务；worker 轮询 outbox → upsert 汇总 → 置 done；
> 失败指数退避，> 5 次置 dead + 告警；dead 可人工重放"**，验收线是
> **"故障注入后汇总与明细最终一致（差异 0）、dead 可重放成功"**。
>
> 本报告只记录实际跑出来的输出。

---

## 0. 环境与参数

| 项 | 值 |
| --- | --- |
| 消息数 | 20 条（每条 = 一次执行结果） |
| `max_attempts` | 3（生产默认 5，实验里压小以便快速看到 dead） |
| 退避基数 | 0.3s（生产默认 5s），退避序列 0.3 → 0.6 → 1.2 |
| 业务时区 | `Asia/Shanghai`（`biz_date` 按本地自然日归集） |

> 参数可调：`python bench/outbox_fault.py --messages 50 --max-attempts 5 --backoff 5`。
> 报告里的数字都随参数打印，不做"看起来很好看"的包装。

---

## 1. 三个设计决策

**① 汇总是「重算」而不是「累加」**

```sql
-- 每次处理消息：按 (日, 项目, 版本) 从 executions 重新聚合并整体 upsert
INSERT INTO daily_exec_summary (...) VALUES (...)
ON CONFLICT (biz_date, project_id, version_id) DO UPDATE SET total = EXCLUDED.total, ...
```

outbox 是 at-least-once：进程可能在"写汇总"和"置 done"之间崩掉，消息会被再处理一次。
`total = total + 1` 的写法在重放时就会多算。重算让"处理 1 次"和"处理 100 次"结果完全一样，
而且**能自愈**：某条消息丢了，同键只要有另一条消息被处理，整天的汇总就会被算对。

**② 一条消息一个事务 + `FOR UPDATE SKIP LOCKED`**

多副本部署时每个 worker 都轮询，`SKIP LOCKED` 让它们各拿各的、不互相等。
事务边界卡在单条消息：一条坏消息不连累同批其它消息，也不会让长事务锁住 outbox。

**③ 失败 ≠ 丢弃，但"永远不会成功"的消息当场 dead**

`attempts+1` + 指数退避；超过 `max_attempts` 置 dead。
未知 topic、payload 缺字段、项目/版本已被删这类消息**重试也不会成功**，直接 dead
——否则 5 次重试只会在日志里刷 5 遍同样的错，还让告警失去意义。

---

## 2. 实验：故障注入 → 死信 → 重放 → 收敛

**脚本**：`bench/outbox_fault.py`　**命令**：`python bench/outbox_fault.py --messages 20`

三幕：注入故障 → 只恢复不重放 → 人工重放。

### 原始输出

```text
outbox 故障注入 · 20 条消息 · max_attempts=3 · backoff=0.3s

已提交 20 条执行结果（业务日 2025-06-09），产生同样条数的 outbox 消息
退避预算：0.3s × (2^3-1) ≈ 2.1s —— 故障超过这个时长，消息就会被打成死信

[第一幕] 注入故障：跑到没有 pending 为止（6 轮 / 1.1s）
    outbox：done=0 dead=20 pending=0
    重试次数：合计 60 次，单条最多 3 次（理论 3 次后置 dead）
    [OK] 重试预算耗尽 → 全部进入死信（不丢弃、不静默）

[第二幕] 故障恢复，但**不重放**死信
    本轮处理：0 条（死信不会被自动捡起）
    汇总差异：1 个 (日,项目,版本) 有偏差，其中未汇总的明细 20 条
    [OK] 没有重放就不会一致 —— 死信必须告警 + 人工处理

[第三幕] 人工重放 20 条死信后重新处理（1 轮）
    本轮处理：done=20 failed=0 dead=0
    outbox：done=20 dead=0 pending=0
    汇总差异：0
    [OK] 最终一致：汇总 == 明细

[汇总行] 2025-06-09  total=20 passed=5 failed=5 blocked=5
         skip=5（只进 total：汇总表没有 skipped 列，这是既定口径）

  结论：全部通过
```

### 结论

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 重试次数 | 按指数退避累加 | **20 条 × 3 次 = 60 次**，单条最多 3 次 |
| 失败处理 | 不丢、不静默 | **20 条全部 dead**，`last_error` 保留原因 |
| 故障恢复后 | 若未重放则**不一致** | **0 条被处理，20 条明细未汇总** |
| 重放后 | 最终一致 | **20 条 done，差异 0** |
| 汇总口径 | total 与明细一致 | `total=20`；`skip=5` 只进 total |

**第二幕是这个实验最有价值的一幕**：
很多人（包括我自己最初）会默认"故障恢复 = 自动一致"。事实是死信不会自己复活，
必须有人看到告警、执行重放。所以 `dead > 0` 必须有指标（`tdp_outbox_dead`）和运维入口
（`GET /v1/admin/outbox?status=dead` + `POST /v1/admin/outbox/replay`）。

### 一个反直觉的细节（写下来提醒自己）

因为汇总是"重算"的，同一个 (日, 项目, 版本) **只要有一条消息被成功处理，整天就是对的**。
所以 dead 造成少算的前提是"这个键一条消息都没成功过"。
这不代表 dead 可以不处理——它意味着 worker 曾经持续失败，
而且在重放之前你**无法确认**到底影响了哪些键。

---

## 3. 并发与幂等的验证（pytest）

```text
$ python -m pytest
107 passed
```

`tests/test_outbox_worker.py` 10 条，覆盖了实验里不方便展示的部分：

| 用例 | 证明什么 |
| --- | --- |
| `test_batch_upserts_summary` | 4 条结果（pass/ pass/ fail/ skip）→ `total=4 passed=2 failed=1 blocked=0`，`skip` 只进 total |
| `test_reprocess_does_not_double_count` | 把已处理的消息重置为 pending 再跑一次 → **数字不变**（重算的幂等性） |
| `test_failure_sets_attempts_and_defers` | 失败后仍在 pending、`attempts=1`、`next_at` 在未来；退避窗口内扫不到；到点后能成功 |
| `test_dead_after_max_attempts_then_replay` | 达到上限置 dead；dead 不会被自动捡起；重放把 `attempts` 归零；重放后收敛 |
| `test_invalid_message_dies_immediately` | 未知 topic / payload 缺字段 → 当场 dead，**不浪费 5 次重试** |
| `test_multiple_workers_do_not_double_process` | 5 个 worker 并发消费 20 条：合计处理 20 条、每条 `attempts=1`、汇总 `total=20` |
| `test_two_biz_dates_produce_two_rows` | 跨日的执行结果落成两行汇总，数字各自正确 |
| `test_biz_date_filter_processes_only_that_day` | `--biz-date` 只处理那天（"生成某天日报"） |

其中 `test_multiple_workers_do_not_double_process` 是 `FOR UPDATE SKIP LOCKED` 的直接证据：
如果锁不对，会出现同一条消息被两个 worker 各处理一次（`attempts=2`），断言会立刻红。

---

## 4. 端到端冒烟（33 项）

`dev.cmd smoke` 在真实 uvicorn 上多跑了汇总这一段：

```text
[OK]   POST /v1/jobs/summary 200 且积压清零   done=1 pending_left=0
[OK]   汇总与明细差异 0（最终一致）            diff=0
[OK]   outbox 无死信                          dead=0
[OK]   GET /v1/admin/outbox 200（能看积压与死信） stats={'pending': 0, 'done': 85, 'dead': 0, 'summary_rows': 17}
```

---

## 5. 复现

```bat
dev.cmd worker            :: 处理积压（--drain 一遍就退出；-Watch 常驻）
dev.cmd bench             :: 四项实验，含 outbox 故障注入
dev.cmd test              :: 107 个用例
dev.cmd smoke             :: 33 项真实 HTTP 冒烟
```

单独跑：

```powershell
python bench/outbox_fault.py --messages 20
python scripts/outbox_worker.py --drain
python -m pytest tests/test_outbox_worker.py -q
```

---

## 6. 本项**没有**做的（第 3 周）

| 项 | 说明 |
| --- | --- |
| **业务规则型对账** `fail_without_defect` | 现在只有 `diff_summary()` 的数字对账；"执行失败但没提单"这条质量规则要对账服务来做 |
| `recon_discrepancies` 落库 + `POST /v1/jobs/recon` | 差异单要能持久化、能标记已解决 |
| keyset 50 万条深分页压测 | 接口形态已有（`GET /v1/defects`），缺数据量、`EXPLAIN`、P99 对比 |
| 批量汇总优化 | 现在是"每条消息一次按日聚合"。同一天几千条消息时应该同键只算一次（或换增量 + 定期全量对账）——这是规模问题，不影响当前正确性 |
| 逐请求 P99 | 目前只有整批墙钟与重试次数，不臆造延迟数字 |
