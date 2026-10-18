# 测试数据管理平台

整車测试场景的数据管理后端：**车辆档期占用**、**执行任务领取**、**问题单状态机**、**日报汇总与对账**。

技术重点：**并发下的资源占用一致性**（同一台车同一半天不能被两个项目占用；同一条任务不能被两人领取）。

## 快速开始

```powershell
python -m pip install -r requirements.txt
# 建库（本机绿色版 PG，端口 5433）
& C:\Users\13741\dsh-devtools\pgsql\bin\createdb.exe -h 127.0.0.1 -p 5433 -U postgres tdp
# 建表
& C:\Users\13741\dsh-devtools\pgsql\bin\psql.exe -h 127.0.0.1 -p 5433 -U postgres -d tdp -f migrations/versions/001_baseline.sql
```

## 文档

- 技术方案（16 张表 / 6 个技术点 / A/B 实验设计）：见 `docs/`

## 路线图

| 周 | 交付 | 验收 |
| --- | --- | --- |
| 1 | 表 + 状态机 + 预约主链路（条件更新 + 幂等） | 1000 并发抢同一档期 → 重复占用 0、成功恰好 1 |
| 2 | 任务领取（乐观锁抢占）+ 缓存一致性 A/B | 20 并发领同一任务 → 恰好 1 人；缓存不一致 0 |
| 3 | outbox 汇总 + 日终对账 + keyset 分页 | 故障注入后最终一致；对账 100% 检出 |
