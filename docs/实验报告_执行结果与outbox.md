# 实验报告 · 提交执行结果与 outbox

> 目标：技术方案 §4.4 —— **"事务：写 executions + 写 outbox，同一事务原子"**，
> 以及 §9 坑 #4 —— **"`executed_at` 是 TIMESTAMPTZ、日报按自然日切，时区口径不统一每天差几条"**。
>
> 本报告只记录实际跑出来的输出。worker 汇总、故障注入、dead 重放属于第 3 周，文末单列。

---

## 0. 环境

| 项 | 值 |
| --- | --- |
| OS / Python | Windows · CPython 3.13.12 |
| PostgreSQL | 16.9（`127.0.0.1:5433`） |
| 业务时区 | `TDP_TZ` 默认 `Asia/Shanghai`（本机 PG 会话时区恰好也是，但**代码不依赖它**） |
| 连接池 | `pool_size=20` + `max_overflow=20` |

---

## 1. 一次提交写四张表

```text
① execution_queue: claimed/running → done    （条件 UPDATE，防重复提交/防顶替）
② executions:       写结果（含补录时间 executed_at）
③ outbox:           topic='exec_result_recorded'   ← 与 ② 同一事务
④ 失败且带缺陷时:    defects + defect_links + 「(new)→open」审计
```

**为什么 ③ 必须和 ② 同一事务**：
第 3 周的 worker 靠 outbox 驱动 `daily_exec_summary`。若先提交 executions 再单独写 outbox，
中间崩了就是"明细有、汇总永远没有"——对账会报 `missing_summary`，但谁也补不回来
（明细已落库，没人知道它该不该被汇总）。同一事务里要么两条都在、要么都不在，
worker 才能把"最终一致"收敛住。

**为什么不在这个接口里直接 upsert 汇总表**：
汇总按 (日期, 项目, 版本) 聚合，放进同一事务会拉长事务、放大行锁竞争。
用 outbox 换"最终一致"，代价是汇总短暂延迟——这正是 §4.4 的取舍点。

---

## 2. 实验：20 并发提交同一条任务

**脚本**：`bench/execution_submit.py`　**命令**：`python bench/execution_submit.py --concurrency 20`

### 原始输出

```text
[A] 20 个**不同**幂等键并发提交同一条任务
    结果分布: {'recorded': 1, 'already_submitted': 19}
    落库：executions=1 outbox=1 task=done v2
    耗时(整批墙钟): 199 ms
    [OK] 恰好 1 次落库，明细与 outbox 各 1 条

[B] 同 1 个**相同**幂等键并发提交 20 次
    结果分布: {'recorded': 1, 'replayed': 19}
    重放返回的不同 execution_id 个数: 1（要求 1）
    落库：executions=1 outbox=1
    耗时(整批墙钟): 32 ms
    [OK] 幂等生效：1 次真实写入 + 19 次重放

[C] outbox 消息样本（第 3 周 worker 的输入）
    topic=exec_result_recorded status=pending attempts=0
    payload={'execution_id': 46, 'queue_id': 718, 'project_id': 777, 'version_id': 778,
             'case_id': 1062, 'tester_id': 1173, 'result': 'fail',
             'biz_date': '2026-10-18', 'executed_at': '2026-10-18T13:04:52.289677+00:00'}
    [OK] payload 带 biz_date（对账按本地自然日归集）
```

### 结论

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 成功落库数 | 恰好 1 | **1**（`recorded=1`） |
| 其余请求 | 全部 409 | **19 条 `already_submitted`** |
| `executions` 行数 | 1 | **1** |
| `outbox` 行数 | 1 | **1**（多一条 = 汇总多算一次） |
| 同 key 并发 | 同一个 `execution_id` | **1 次真实写入 + 19 次重放，id 集合大小 = 1** |
| 任务终态 | `done`，version +1 | `done`，v2（领取 +1、完成 +1） |

> 注意 A 组 20 个线程有两套不同 body（`result` 交替 pass/fail），
> 但都是不同幂等键，所以只考抢占；B 组强制 body 完全相同，考幂等。

---

## 3. 业务日期口径（§9 坑 #4）

这一条不是"顺手做的"，是**必须现在做**：等第 3 周对账写完再回来改口径，
`daily_exec_summary` 里已经有一批按 UTC 日归集的数据，只能重刷。

### 规则只有一个入口

`app/domain/biz_time.py` 提供两个**等价**的实现，服务端和 SQL 侧共用：

```python
to_biz_date(moment)              # Python：ensure_aware(moment).astimezone(TZ).date()
biz_date_sql("executed_at")      # SQL：(executed_at AT TIME ZONE 'Asia/Shanghai')::date
```

边界实测（`tests/test_biz_time.py`）：

| 时刻 | UTC+8 的本地日 | 断言 |
| --- | --- | --- |
| `2025-01-01T15:59:59Z` | 2025-01-01 | ✅ |
| `2025-01-01T16:00:00Z` | **2025-01-02** | ✅ 恰好跨日 |
| 无时区 `2025-01-01T16:30` | 2025-01-02 | ✅ naive 按 UTC 解释 |

业务侧验证（`tests/test_execution_flow.py::test_biz_date_follows_local_day_boundary`）：
两个任务分别用 `executed_at=2025-01-01T15:30Z` 和 `16:30Z` 提交，落库后

```text
executions.executed_at  原样保留（不因换算偏移）
outbox.payload.biz_date 2025-01-01 / 2025-01-02   ← 差一天就说明口径错了
```

### 为什么不用 `datetime.now().date()`

那是**进程本地时区**：多副本部署时，一台机器 UTC、一台 CST，同一个 `executed_at`
会算出两个业务日。用显式时区（`TDP_TZ`）就和机器在哪无关。
Windows 上缺 `tzdata` 时 `ZoneInfo` 会失败，这里对常见时区退化成固定偏移并告警，
其余情况**启动就报错**——宁可起不来，也不要静默把日期算错一天。

---

## 4. 原子性的反向验证

正向断言"写成功了"很容易，难的是证明**失败时什么都没留下**。三处反向断言：

| 场景 | 断言 |
| --- | --- |
| 未领取就提交（409 `not_claimed`） | `executions` 0 行、`outbox` 0 行、任务仍 `pending` |
| 别人代提交（409 `not_claim_owner`） | `executions` 0 行、`outbox` 0 行、任务仍 `claimed` |
| 非法 `result`（400） | `executions` 0 行、任务仍 `claimed` |

对应测试：`tests/test_execution_flow.py` 里三个用例的尾部断言。

---

## 5. 测试与冒烟

```text
$ python -m pytest
94 passed
```

本轮新增 22 个用例：

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_biz_time.py` | 6 | 时区边界、naive→UTC、半开区间、SQL 与 Python 等价性 |
| `tests/test_execution_flow.py` | 13 | 四表同事务、无缺陷的失败、带缺陷+`defect_links`、幂等重放、body_mismatch、not_claimed / not_claim_owner / already_submitted、10 线程并发、biz_date 边界、naive 时间、未来时间拒绝 |
| `tests/test_claim_flow.py`（新增 5 条） | 18 | `enqueue`：建用例+排队、重复排队 409 带回 task_id、同 key 重放、车型不匹配、版本不存在 |
| `tests/test_api.py`（新增 3 条） | 25 | HTTP：排队、提交失败带缺陷（缺陷可追溯到 execution）、未领取提交 409、非法 result 422 |

端到端冒烟从 24 项扩到 **29 项**，新增的 5 项是"自己排队 → 领取 → 提交 → 追溯"整条链：

```text
[OK]   排队一条新任务 201（用例不存在则先建）
[OK]   提交失败结果 201，带 execution_id / defect_no / biz_date
[OK]   同 key 重放：返回同一个 execution_id（不重复落库）
[OK]   缺陷能追溯到来源执行记录（execution_id 对得上）
[OK]   任务已转 done
```

> 冒烟改成"自己排队再造一条任务"之后，反复跑不会把演示任务池耗空
> （之前的版本会消费种子任务，跑 10 次就没得领了）。

---

## 6. 复现

```bat
dev.cmd bench      :: 档期竞争(1000) + 领取竞争(20) + 提交竞争(20)
dev.cmd test       :: 94 个用例
dev.cmd smoke      :: 起临时服务跑 29 项真实 HTTP 冒烟
```

单独跑：

```powershell
python bench/execution_submit.py --concurrency 20
python -m pytest tests/test_execution_flow.py tests/test_biz_time.py -q
```

---

## 7. 本项**没有**做的（第 3 周）

| 项 | 说明 |
| --- | --- |
| outbox worker | 现在消息只写不消费，`outbox.status` 一直是 `pending`（这正是"worker 未上线"的诚实状态） |
| `daily_exec_summary` upsert | worker 的职责；口径已由 `biz_date` 定好 |
| 故障注入 / 指数退避 / dead 重放 | §4.4 的验收实验，需要 worker 才能做 |
| `skip` 的汇总口径 | 汇总表只有 `passed/failed/blocked` 三列，`skip` 只能进 `total`。第 3 周对账要把 `total - (passed+failed+blocked) = skipped` 写进校验，否则总数对不上 |
| 逐请求 P99 | 目前只有整批墙钟，不臆造延迟数字 |
