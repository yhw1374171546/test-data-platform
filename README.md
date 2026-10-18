# 测试数据管理平台

整车测试场景的数据管理后端：**车辆档期占用**、**执行任务领取**、**问题单状态机**、**日报汇总与对账**。

技术重点：**并发下的资源占用一致性**（同一台车同一半天不能被两个项目占用；同一条任务不能被两人领取）。

> 定位：个人项目。起源是真实工作痛点——测试车辆 / 软件版本 / 用例 / 问题单四个维度靠人工表格拼，
> 于是把重复劳动做成了系统。业务细节是真的，技术实现也是真的（每个技术点都有能跑出数的实验）。

---

## 进度

### 第 1 周（已完成）

| # | 交付 | 状态 | 验收数据 |
| --- | --- | --- | --- |
| 1 | 16 张表 + 索引（`migrations/versions/001_baseline.sql`） | ✅ | 16 表 / 37 索引，PG 16 实跑 |
| 2 | 预约主链路：条件更新 + 物理幂等 + 取消释放 | ✅ | **1000 并发抢同一档期 → `created=1`、`slot_taken=999`、重复占用 0** |
| 3 | 问题单状态机（显式转移表 + 审计 + 幂等） | ✅ | **64 个 (from,to) 组合全遍历 → 合法 15 条全过、非法全 409、审计条数 == 合法转移数** |
| 4 | API 层（预约 / 问题单 / 健康检查 / 指标 / 统一错误体） | ✅ | 端到端冒烟全通过 |
| 5 | `dev.ps1` / `dev.cmd` 一键脚本 | ✅ | `check / init / test / smoke / bench / api / docs` 七个动作 |

报告：[`docs/实验报告_第1周.md`](docs/实验报告_第1周.md)

### 第 2 周（进行中）

| # | 交付 | 状态 | 验收数据 |
| --- | --- | --- | --- |
| 1 | **任务领取**：排队 + 乐观锁抢占 + 释放 + 幂等 + 4 种 409 | ✅ | **20 并发 → `claimed=1` / `already_claimed=19`；同 key → 1 claimed + 19 replayed；旧版本号重领 409 `version_conflict`** |
| 2 | **提交执行结果**：executions + outbox 同事务、失败带出问题单、业务日期口径 | ✅ | **20 并发 → `recorded=1` / `already_submitted=19`，executions 与 outbox 各 1 条；同 key 重放同一个 `execution_id`** |
| 3 | **outbox worker + 日报汇总**：重算式 upsert、指数退避、死信 + 人工重放 | ✅ | **注入故障 20 条全部 dead（60 次重试）→ 恢复不重放仍有 20 条未汇总 → 重放后差异 0** |
| 4 | **日终对账**：三方对齐（明细 / 汇总 / 失败是否提单）+ 差异单自动解决 | ✅ | **注入 5 种差错 → 检出 6/6 条、误报 0；修好后重跑自动 resolved** |
| 5 | **缓存一致性**：A 同步删 / B 延迟双删 / C 消息驱动 + **负对照** + 批量消费 | ✅ | **负对照脏读 99.65%（证明能测出）；A 1.72% / B 4.30% / C 10.12%（C 的窗口=消费延迟，有界且不丢）；TTL 兜底后全部收敛** |
| 6 | **占用方案 P99** + **50 万条 keyset 深分页** + 性能索引迁移 | ✅ | **A/B/C 正确性一致（恰好 1 人成功）；P99 A 269ms / B 275ms / C 25ms（DB 语句 603→9）；分页第 25000 页 OFFSET 148ms vs keyset 1.2ms（128x）；带 project 过滤时浅页 113ms→1.5ms，靠复合索引** |

报告：[`docs/实验报告_任务领取.md`](docs/实验报告_任务领取.md) · [`docs/实验报告_执行结果与outbox.md`](docs/实验报告_执行结果与outbox.md) · [`docs/实验报告_outbox汇总与故障注入.md`](docs/实验报告_outbox汇总与故障注入.md) · [`docs/实验报告_日终对账.md`](docs/实验报告_日终对账.md) · [`docs/实验报告_缓存一致性.md`](docs/实验报告_缓存一致性.md) · [`docs/实验报告_P99与深分页.md`](docs/实验报告_P99与深分页.md)

**当前测试规模：150 个**（纯函数 37 + 落库/集成 79 + HTTP 契约 34），`dev.cmd` 端到端冒烟 **40 项**。

---

## 一键开始

```bat
dev.cmd
```

一条命令做完：环境体检 → 建库建表 + 演示数据 → 跑测试 → 起临时服务做真实 HTTP 冒烟（跑完自动关）。

| 命令 | 作用 |
| --- | --- |
| `dev.cmd` | 默认：`check` + `init` + `test` + `smoke` |
| `dev.cmd check` | 只体检：Python / 依赖 / PG 连通性 / 表结构 / pytest |
| `dev.cmd init` | 建库建表 + 演示主数据（幂等，可反复跑） |
| `dev.cmd test` | `pytest`（143 个用例，真库 + Redis） |
| `dev.cmd smoke` | 起临时服务，跑 40 项真实 HTTP 冒烟（含缓存/汇总/对账），自动关闭并清理本次占用的档期与任务 |
| `dev.cmd worker` | 跑 outbox worker 处理积压（默认处理完就退出；`-Watch` 常驻轮询） |
| `dev.cmd recon` | 跑日终对账（明细 vs 汇总 vs 失败是否提单），差异单打印成表 |
| `dev.cmd bench` | 八项实验：档期竞争（1000）+ 领取竞争（20）+ 提交竞争（20）+ outbox 故障注入 + 对账注入 + 缓存一致性 + 占用三方案 P99 + 深分页 |
| `dev.cmd api -Port 8000 -Reload` | 启动服务 → <http://127.0.0.1:8000/docs> |
| `dev.cmd docs` | 打印地址与可直接复制的示例请求 |

> `dev.cmd` 是 `dev.ps1` 的包装器：Windows 默认 `ExecutionPolicy=Restricted` 会拒绝执行 `.ps1`，
> 包装器用 `-ExecutionPolicy Bypass` **只对本进程生效**，不改用户的系统安全设置。

### 手动开始（不想用脚本）

```powershell
python -m pip install -r requirements-dev.txt

# 本机绿色版 PG（端口 5433）
& C:\Users\13741\dsh-devtools\pgsql\bin\pg_ctl.exe -D C:\Users\13741\dsh-devtools\pgdata -o "-p 5433" -l pg.log start

# 本机绿色版 Redis（端口 6380；缓存相关功能要用）
& C:\Users\13741\dsh-devtools\redis\redis-server.exe C:\Users\13741\dsh-devtools\redis\redis-6380.conf

python scripts/init_db.py --seed-demo     # 建库 + 建表 + 演示数据
python -m pytest                          # 131 个用例
python -m uvicorn app.main:app --port 8000 --reload
python scripts/outbox_worker.py --drain    # 把执行结果汇总成日报
python scripts/recon.py                    # 日终对账（--strict 时有差异退出码 1）
python scripts/api_smoke.py --base-url http://127.0.0.1:8000
```

---

## 接口

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/bookings` | 预约车辆档期（**核心并发点**） | 必需 |
| POST | `/v1/bookings/{no}/cancel` | 取消并释放档期（可重复调用） | 必需 |
| GET | `/v1/vehicles/{vin}/slots?date=` | 查某天档期（`only_free=true` 只看可约） | — |
| GET | `/v1/vehicles/{vin}` | **车辆详情（带缓存）**，响应头 `X-Cache: hit/miss/bypass` | — |
| PATCH | `/v1/vehicles/{vin}` | 更新车辆并失效缓存（设绝对值，**天然幂等，不要求幂等键**） | — |
| GET | `/v1/test-cases/{case_no}` | 用例详情（带缓存；标题被导入更新时自动失效） | — |
| POST | `/v1/defects` | 人工提报问题单 | 必需 |
| POST | `/v1/defects/{no}/transition` | 状态转移（非法 409，重复 `already=true`） | 必需 |
| GET | `/v1/defects` | 问题单列表（keyset 分页） | — |
| GET | `/v1/defects/{no}` | 详情 + 完整审计轨迹 | — |
| GET | `/v1/defects/status-machine` | 状态机全貌（转移表 + 8×8 矩阵） | — |
| GET | `/v1/queue` | 任务池列表（keyset 按 id 翻页） | — |
| POST | `/v1/queue` | 排队一条用例（用例不存在则先建；版本必须匹配项目车型） | 必需 |
| POST | `/v1/queue/claim` | **领取执行任务**（抢占：恰好一人成功，其余 409） | 必需 |
| POST | `/v1/queue/{task_id}/release` | 释放任务回待领取（仅领取人本人） | 必需 |
| POST | `/v1/executions` | **提交执行结果**（executions + outbox 同事务；失败可带出问题单） | 必需 |
| POST | `/v1/jobs/summary` | **处理 outbox 并生成日报汇总**（可只处理某一天） | 必需 |
| POST | `/v1/jobs/recon` | **日终对账**：明细 / 汇总 / 失败是否提单三方对齐 | 必需 |
| GET | `/v1/admin/outbox` | outbox 消息与积压统计（`status=pending\|done\|dead`） | — |
| POST | `/v1/admin/outbox/replay` | **重放死信**（`attempts` 归零；不传 ids 则全部） | 必需 |
| GET | `/v1/admin/recon/discrepancies` | 对账差异单（默认只看未解决的） | — |
| GET | `/healthz` `/readyz` `/metrics` | 存活 / 就绪 / Prometheus | — |

### 约定

- **写接口必须带 `Idempotency-Key`**：同 key 同 body → 返回首次响应（`replay=true`）；
  同 key 换 body → `422`，且不产生任何副作用；缺头 → `400`。
  **例外**：`PATCH /v1/vehicles/{vin}` 设的是绝对值，同一个 body 调 N 次结果一样，
  天然幂等 —— 再叠一个幂等键只是多一张表记录。需要幂等键的是"有副作用"的写。
- **幂等指纹只算请求体**：`executed_at` 缺省时服务端会补 `now()`，但补出来的值不参与哈希——
  否则同一个请求每次重试都被判成"换了 body"。
- **错误体统一** `{error, detail, trace_id}`，`trace_id` 同时回写到响应头 `X-Request-ID`。
- 状态码：`400` 参数/缺头、`404` 对象不存在、`409` 业务冲突、`422` 校验失败或同 key 换 body、
  `503` DB 不可用。409 的具体 `error` 取值：
  `slot_not_available` / `booking_not_cancellable` / `illegal_transition` / `state_conflict` /
  `already_claimed` / `version_conflict` / `not_claim_owner` / `not_releasable` /
  `task_exists` / `model_mismatch` / `not_claimed` / `already_submitted` / `not_submittable`。
- **业务日期**：`executed_at` 是带时区的时间戳，归属日按 `TDP_TZ`（默认 `Asia/Shanghai`）
  的本地自然日算，统一走 `app/domain/biz_time.py`。提交结果的响应里会回 `biz_date`。
- **缓存**：`TDP_CACHE_STRATEGY` 决定失效策略
  （`off` / `none` 负对照 / `delete` A / `double_delete` B / `outbox` C，默认 `delete`），
  `TDP_CACHE_TTL` 默认 300s。读接口用响应头 `X-Cache` 告诉你命中没有 —— 缓存最容易出的问题是
  "以为命中了其实没有"（key 拼错、TTL 0、Redis 挂了降级），响应头让这件事一眼可见。

```powershell
# 预约
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/bookings' `
  -Headers @{'Idempotency-Key'='demo-1'} -ContentType 'application/json' `
  -Body '{"project_id":1,"vehicle_id":1,"testers_id":1,"slot_date":"2030-01-01","half":0}'

# 非法转移：open -> closed
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/defects/<单号>/transition' `
  -Headers @{'Idempotency-Key'='d-2'} -ContentType 'application/json' `
  -Body '{"to_status":"closed","actor":"qa"}'
# → 409 {"error":"illegal_transition","detail":{"from":"open","to":"closed","allowed":["rejected","triaged"],...}}

# 领取任务（换 tester_id 再领 → 409 already_claimed；带旧 expect_version → 409 version_conflict）
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/queue/claim' `
  -Headers @{'Idempotency-Key'='claim-1'} -ContentType 'application/json' `
  -Body '{"task_id":1,"tester_id":1}'

# 处理积压 → 生成日报（幂等；也可以交给 worker 进程常驻轮询）
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/jobs/summary' `
  -Headers @{'Idempotency-Key'='sum-1'} -ContentType 'application/json' `
  -Body '{"limit":500}'
# → {"processed":1,"done":1,"failed":0,"dead":0,"pending_left":0,"dead_total":0,"diff_count":0}

# 看死信 + 重放
Invoke-RestMethod 'http://127.0.0.1:8000/v1/admin/outbox?status=dead'
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/admin/outbox/replay' `
  -Headers @{'Idempotency-Key'='replay-1'} -ContentType 'application/json' -Body '{}'

# 日终对账（三方对齐）→ 差异单
Invoke-RestMethod -Method Post 'http://127.0.0.1:8000/v1/jobs/recon' `
  -Headers @{'Idempotency-Key'='recon-1'} -ContentType 'application/json' `
  -Body '{"biz_date":"2030-01-01","project_id":1}'
# → {"total":0,"by_kind":{},...}；有差异时 items 里带 expected/actual/detail
Invoke-RestMethod 'http://127.0.0.1:8000/v1/admin/recon/discrepancies?resolved=false'
```

---

## 目录结构

```
test-data-platform/
├── app/
│   ├── api/          bookings.py / defects.py / queue.py / executions.py
│   │                 jobs.py / admin.py / catalog.py
│   │                 deps.py / errors.py / schemas.py / context.py
│   ├── domain/       defect_state.py（状态机）· queue_state.py（队列规则）
│   │                 slot_policy.py（半天粒度）· biz_time.py（业务日期口径）
│   │                 recon_rules.py（对账规则：四类差异 + 质量规则）
│   ├── service/      booking.py（占用+取消）· defect.py（转移+审计+列表）
│   │                 claim.py（排队+抢占+释放）· execution.py（提交结果+outbox）
│   │                 outbox.py（worker：重算汇总/退避/死信/批量失效）
│   │                 recon.py（三方对账 + 差异单自动解决）
│   │                 catalog.py（车辆/用例详情：缓存读 + 会失效的写）
│   ├── infra/        db.py · idempotency.py · metrics.py
│   │                 cache.py（三策略 + 负对照）· redis_lock.py（预占：C 方案的漏斗）
│   └── main.py       中间件（trace_id + 指标）→ 路由 → 运维端点
├── migrations/versions/001_baseline.sql · 002_perf_indexes.sql（幂等增补索引）
├── scripts/          init_db.py（建库建表+演示数据）· api_smoke.py（真实 HTTP 冒烟）
│                     outbox_worker.py（常驻 worker）· recon.py（对账命令行）
├── tests/            test_defect_state.py · test_defect_flow.py · test_queue_state.py
│                     test_claim_flow.py · test_execution_flow.py · test_biz_time.py
│                     test_outbox_worker.py · test_recon_rules.py · test_recon_flow.py
│                     test_cache_catalog.py · test_booking_schemes.py · test_api.py
│                     conftest.py
├── bench/            slot_contention.py（档期竞争）· claim_race.py（领取竞争）
│                     execution_submit.py（提交竞争）· outbox_fault.py（故障注入）
│                     recon_inject.py（对账注入）· cache_consistency.py（缓存一致性）
│                     slot_schemes.py（占用三方案 P99）· pagination.py（深分页）
├── docs/             技术方案.md · 实验报告_第1周.md · 实验报告_任务领取.md
│                     实验报告_执行结果与outbox.md · 实验报告_outbox汇总与故障注入.md
│                     实验报告_日终对账.md · 实验报告_缓存一致性.md
│                     实验报告_P99与深分页.md
├── dev.ps1 / dev.cmd / requirements*.txt / pytest.ini
```

---

## 设计要点（面试会问，代码里有注释）

1. **占用靠一条条件 UPDATE + DB 唯一约束**，不靠应用层"先查再改"：
   `UPDATE ... WHERE status='free'` 用 affected rows 判定，1000 并发只成功 1 次。
   先查再改在并发下必然超占——这是"库存/档期不超卖"的唯一正解。
2. **幂等靠 DB 唯一约束 + 同事务**，不靠进程内字典（多副本立刻失效）：
   先占键 → 干业务 → 写回首次响应，全部在一个事务里；业务失败连键一起回滚，重试仍可再抢。
3. **状态机是纯函数 + 显式转移表**，所以能被 64 组合穷举验证；落库时用
   `UPDATE ... WHERE id=? AND status=?` 做并发守卫，`affected=0` 时区分
   "并发重复提交（already）"和"状态被别人改走（409）"。
4. **`already=true` 不写审计**：刷新按钮不该污染审计表，审计条数必须等于真实流转次数。
5. **领取靠 `WHERE status='pending'` 的谓词重求值**：PG 对被阻塞的 UPDATE 会重新判断谓词，
   后到的事务醒来时 `affected=0`——这就是"恰好一人领到"，不需要分布式锁。
6. **`version` 防的是 ABA，不是重复点击**：重复点击归幂等键管；`version` 管的是
   "领取→释放→再领取"之后，拿着旧版本号的陈旧请求不能假装自己领到了。
7. **executions 和 outbox 必须同一事务**：先提交明细再补 outbox，中间崩了就是
   "明细有、汇总永远没有"，对账只能报差异却补不回来。同一事务 → 要么都在要么都不在。
8. **业务日期只有一个换算入口**：`executions.executed_at` 是 TIMESTAMPTZ，日报按本地自然日切。
   统一走 `biz_time.to_biz_date()` / `biz_date_sql()`，不散落 `::date`
   —— 否则容器里默认 UTC，每天 00:00~08:00 的结果会算到前一天。
9. **汇总用「重算」不用「累加」**：outbox 是 at-least-once，`total = total + 1` 在重放时就多算。
   按 (日, 项目, 版本) 从 executions 重新聚合并整体 upsert，处理 1 次和 100 次结果一样，
   而且**能自愈**（同键只要有一条消息被处理，整天就被算对）。
10. **死信必须有人看**：`dead > 0` 是一个"不会自愈"的状态，指标 `tdp_outbox_dead` +
    `GET /v1/admin/outbox?status=dead` + `POST /v1/admin/outbox/replay` 三件套缺一不可。
    实验里第二幕就是证据：**故障恢复不重放，差异依然存在**。
11. **对账的口径必须和汇总完全一致**：明细侧用 `biz_date_sql()`（和 worker 写汇总同一个
    纯函数的 SQL 版）。两边口径不同，就会自己制造"每天差几条"的假差异。
12. **质量规则也是对账**：`fail_without_defect`（失败但没提单）检查的不是数据一致性，
    而是流程有没有被遵守——数字全对也可能有质量事故被漏掉。这类检查纯数字对账永远发现不了。
13. **差异单是"活"的**：`UNIQUE (日, 项目, kind)` + 每次对账重算，重复跑不堆历史噪音，
    问题修好后 `resolved` 自动变 true（**作用域要包含"没有差异"的组合**，否则永远解不开）。
14. **删缓存必须在事务提交之后，且必须能重试**：A/B 的删除是"尽力而为"（进程在
    commit 之后 delete 之前崩掉就永久丢），C 把失效写成 outbox 消息 —— 失败会重试、
    超限进死信告警。实测 A 最好但**停写后仍有脏值残留**（丢删除），C 次数最多但**残留 0**。
15. **缓存失效消息必须批量消费**：一条消息一个事务时，消费只有 ~100 条/秒，
    而写入 ~460 次/秒，积压一路涨、C 退化成"从不失效"。批量（一次事务取一批、删一批）
    是这类幂等消息的标配，不是优化项。
16. **三种占用方案共用一套业务代码**，只换"怎么抢"（`create_booking(scheme=...)`）：
    A 条件更新 / B `FOR UPDATE` / C Redis 预占。正确性始终由唯一约束兜底，所以能放心换手段。
    实测单档期场景 A≈B（B 多一次来回没买到东西），C 快一个数量级（DB 语句 603→9）——
    因为 C 让**输家一次 DB 都不打**。
17. **Redis 预占只是漏斗，不是真相**：预占成功后仍要写库，失败/取消必须把 key 放掉。
    取消时用 `force_release`（不看 holder），否则会出现"取消了却约不回来"。
18. **keyset 的收益来自索引同时覆盖「过滤 + 排序」**：只把 OFFSET 改写成
    `(created_at, id) < (?, ?)` 而索引只有 `(created_at DESC, id DESC)`，
    带 `project_id` 过滤时浅页反而更慢（113ms）—— PG 只能扫描+过滤+排序。
    补 `(project_id, created_at DESC, id DESC)` 后回到 1.5ms 且不随深度增长。
19. **endpoint 全部是同步 `def`**：DB 是阻塞的，写成 `async def` 会卡死事件循环。
20. **`/metrics` 的 path 用路由模板**，不是真实 URL，否则每个 id 都变成一条时间序列。

---

## 踩坑记录（真踩过，写下来省下次）

| # | 坑 | 根因 / 解法 |
| --- | --- | --- |
| 1 | `dev.ps1` 一运行就满屏 `Unexpected token` | Windows PowerShell 5.1 按 **GBK** 读无 BOM 的 `.ps1`，中文把引号吃掉了。**必须存成 UTF-8 with BOM**（`dev.cmd` 已照顾编码，编辑脚本时别让编辑器把 BOM 去掉） |
| 2 | `dev.cmd` 里的中文注释被当成命令执行 | `cmd.exe` 按 OEM 页读 `.cmd`，所以 `.cmd` 只放 ASCII；中文一律放 `.ps1` |
| 3 | 预约失败却把幂等键留在了库里 | 失败路径用 `return` 而不是 `raise`，`session_scope` 照常 commit。**失败必须抛异常**才算回滚 |
| 4 | 20 并发推同一目标写了 20 条审计 | 先读状态再无条件 UPDATE。改成 `WHERE status=:frm` 条件更新，`affected=0` 走 already 分支 |
| 5 | 非法转移后改对参数重试却拿到 422 | 幂等键在非法请求里被提交了（body_hash 不同）。非法/未知状态必须在 `claim` 之前或随异常回滚 |
| 6 | `bench` 跑完库里多出 `BENCH-*` 项目/车辆 | 压测按"每轮独立造数据"设计，方便多轮对比；这些数据留着不影响业务表，需要干净库可重建 |
| 7 | 中文 JSON 在控制台变乱码 | 只影响显示：脚本里设 `PYTHONUTF8=1` + `chcp 65001` 即可，接口返回的字节是对的 |
| 8 | 用"不同测试员 + 同 `Idempotency-Key`"测抢占，结果全 422 | `body_hash` 包含 `tester_id`，换人就是"同 key 换 body"。**抢占要用不同的 key 测**，幂等要用同一个人同一个 key 测，两者是两套机制 |
| 9 | 同 key 重试提交结果，第二次被判 `422 body_mismatch` | 幂等指纹里放了服务端补的 `executed_at=now()`，每次哈希都不同。**指纹只能算请求体**，不能算派生值 |
| 10 | 冒烟脚本里预约接口突然 422 `Extra inputs are not permitted` | 把共享的 `ids` 字典整体铺进请求体，多带了 `version_id`。请求模型 `extra="forbid"` 挡住了——这正是它的价值，别为了省事关掉 |
| 11 | 压测里同 key 组混进 `body_mismatch` | 同 key 组的线程各自交替 `result=pass/fail`，命中"同 key 换 body"。**同 key 组必须发完全相同的 body** |
| 12 | `replay_dead` 报 `syntax error at or near "LIMIT"` | **PG 的 `UPDATE` 不支持 `LIMIT`**（MySQL 才行）。改成"子查询选出 id → `WHERE id IN (...)`" |
| 13 | worker 处理测试数据时全部进死信 | 测试清理删了 projects，但 outbox 消息还带着那个 `project_id` → `daily_exec_summary` 外键失败。两处修：**测试清理要连 outbox/汇总一起删**；worker 遇到"项目/版本已不存在"直接判消息作废（dead，不重试） |
| 14 | `outbox` 查询报 `column "created_at" does not exist` | 表里没有这个列（见 baseline DDL）。`next_at` 插入时就是 `now()`，用它看积压时长即可，别自己"补"一个不存在的字段 |
| 15 | 测试里用 2030 年当业务日，被自己的校验拦住 | `submit_execution` 拒绝"超过一天以后的未来"（防止时钟歪/写错）。测试要用**过去的日期**；这正说明校验生效了 |
| 16 | 差异修好后，差异单永远是 unresolved | `_resolve_stale` 只遍历了"本次**有**差异的组合"，问题一修好就没差异了，那段代码压根不执行。作用域必须换成"本次**检查过**的组合"（含无差异的） |
| 17 | 跨日注入时，预期 `missing_summary` 却报成 `summary_mismatch` | 次日**本来就有汇总**（原来有 1 条执行），跨日后变成 2 条 → 数字对不上，不是"没有汇总"。要制造"少算"，次日必须原本没有数据 |
| 18 | `dev.cmd recon` 全量跑报出 30+ 条差异 | 压测会**故意注入差错**（`BENCH-*` 项目），对账如实报出来是正确行为。只想看干净范围就加 `--biz-date` / `--project-id` |
| 19 | 冒烟里"对账无差异"是假通过 | 两个原因叠在一起：① 拿了**档期日**去过滤（执行归属的其实是**提交当天**）；② `pairs_checked` 当时返回的是"有差异的组合数"，`0` 被误读成"没问题"。现在拆成 `pairs_checked`（真的核对了几组）和 `pairs_with_diff`（其中几组有问题），冒烟也断言 `pairs_checked >= 1` |
| 20 | 缓存一致性实验里四组都测出 40%~99% 脏读 | 脏读判定用了**全局最大序号**，而读回来的是**某台车**的值 —— 拿全局最大比单车序号，正常读也判脏。改成按车记录、按车比较 |
| 21 | 压测里读延迟 p50 几百毫秒 | ① 读线程每次抢全局锁 → convoy；② 冷启动时所有读一起回源，把连接池堵死。改成**读侧无锁快照 + 线程本地统计 + 跑前预热缓存** |
| 22 | Redis 报 `Too many connections`，但 maxclients=10000 | **redis-py 8 客户端连接池默认上限是 100**，报错长得像服务端拒绝。显式 `max_connections=256` |
| 23 | 连 Redis 5 报 `unknown command HELLO` | redis-py 8 默认用 RESP3 握手（`HELLO 3`），Redis 5 不认。显式 `protocol=2` |
| 24 | 延迟双删的第二次删除静默失效 | `threading.Timer(args=((key,),))` 多包了一层元组 → `delete((key,))`，redis-py 报 `Invalid input of type: 'tuple'`。正确写法 `args=(key,)` |
| 25 | outbox 策略的失效"看起来没生效" | 上一轮实验留下上万条 `cache_invalidate` 消息，消费者 FIFO 整轮都在消费**旧**消息。实验前要清队列；测试里也要清积压，否则 `process_batch(limit=N)` 的配额被吃掉 |
| 26 | `dev.ps1` 报 `Unexpected token '??'` | `??` 是 PowerShell 7 语法，Windows PowerShell 5.1 不认。改写 `if/else`（脚本要兼容 5.1） |
| 27 | 分页 bench 里 keyset 和 OFFSET 返回的不是同一页 | keyset 的游标必须是**上一页最后一条**（第 offset-1 条），我取成了当页第一条，于是返回下一整页。翻页"看起来正常"，只有逐条对比才暴露 |
| 28 | P99 实验测出 C 方案"0 次 DB 事务" | 用了 `pg_stat_database.xact_commit` 差值，那张视图是异步刷新的。改成给 SQLAlchemy 挂 `before_cursor_execute` 事件精确计语句数（C 是 9 条，不是 0） |
| 29 | 带 `project_id` 时 keyset 浅页要 113ms | 基线索引 `(created_at DESC, id DESC)` 覆盖不了过滤条件，PG 只能扫描+排序。补复合索引 `(project_id, created_at DESC, id DESC)` 后 1.5ms。**keyset 不是改 SQL 就快** |
| 30 | `bench/pagination.py` 造完 50 万条后 `LIMIT 1 OFFSET 199980` 返回 None | 那一轮只造了 20 万条，`DEPTHS` 里的第 25000 页超出范围。加"`offset + 页大小 > 总行数` 就跳过"的守卫，游标为 None 时按"第 1 页"处理 |

---

## 路线图

| 周 | 交付 | 验收 |
| --- | --- | --- |
| 1 ✅ | 表 + 预约主链路（条件更新 + 幂等 + 取消释放）+ 问题单状态机 + API + 一键脚本 | 1000 并发 → 重复占用 0、成功恰好 1；64 组合矩阵全过 |
| 2 ✅ | ~~任务领取~~ + ~~提交执行结果 / outbox~~ + ~~outbox worker + 日报汇总 + 故障注入~~ + ~~日终对账~~ + ~~缓存一致性 A/B/C + 负对照~~ + ~~占用三方案 P99~~ | 全部主线验收已出数：20 并发恰好 1 人 ✅；提交恰好 1 次 ✅；故障后汇总收敛 ✅；对账 6/6 ✅；缓存负对照 + 三方案 ✅；P99 三方对比 ✅ |
| 3 ✅ | ~~outbox worker~~ + ~~日终对账（含 `fail_without_defect`）~~ + ~~keyset 50 万条压测~~ + 性能索引迁移 | 第 25000 页 keyset 1.2ms vs OFFSET 148ms（128x）；带过滤时靠复合索引从 113ms → 1.5ms |

**技术方案 §四 的 7 个技术点已全部完成**（4.1 占用 / 4.2 领取幂等 / 4.3 缓存 / 4.4 异步汇总 /
4.5 状态机 / 4.6 对账 / 4.7 深分页）。剩下的都是**工程面**：

| 欠账 | 影响 | 建议做法 |
| --- | --- | --- |
| Alembic 化 | 现在靠 `001_baseline.sql` + 幂等 `002_*.sql`，能跑但不记录版本 | `alembic init` 后把 baseline 标成初始 revision，002 起走 autogenerate |
| CI | 没有流水线，靠本地 `dev.cmd` | GitHub Actions：起 PG/Redis service → `dev.cmd test` + `dev.cmd smoke` |
| Dockerfile / compose | 部署还是手工起进程 | 多阶段构建 + compose（pg/redis/app/worker 四个服务） |
| 幂等键 TTL | `idempotency_keys` 只增不删 | 定时任务按 `created_at` 删（索引已在 002 里备好） |
| 429 限流 | 只有并发竞争，没有速率限制 | 网关层令牌桶，或按 `X-Request-ID` 维度 |
| 认证 | 接口全开放 | 网关鉴权 + 内部服务用 mTLS |
