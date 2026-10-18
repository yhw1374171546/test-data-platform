-- 性能索引（幂等，可重复执行）
-- 001_baseline.sql 是一次性建表，这个文件是"后续增补的索引"。
-- 全部用 IF NOT EXISTS，所以 `python scripts/init_db.py` 每次都跑一遍是安全的。
--
-- ═══════════════ 1. 深分页：索引必须匹配「过滤 + 排序」═══════════════
-- 基线里的 ix_defects_keyset (created_at DESC, id DESC) 只覆盖排序。
-- 而 API 的列表接口几乎总是带 project_id 过滤：
--     WHERE project_id = :pid AND (created_at, id) < (:t, :id)
--     ORDER BY created_at DESC, id DESC LIMIT 20
-- 只有排序索引时，PG 只能"扫项目下的行 → 过滤 → 排序"，
-- 实测 50 万条、第 1 页就要 100ms+（见 bench/pagination.py 的 before 组）。
-- 复合索引把过滤和排序一次覆盖，keyset 才真正变成"索引定位 20 行"。
CREATE INDEX IF NOT EXISTS ix_defects_project_keyset
    ON defects (project_id, created_at DESC, id DESC);

-- ═══════════════ 2. 对账质量规则：按 execution_id 反查问题单 ═══════════════
-- recon 的 `fail_without_defect` 要用 NOT EXISTS 反查两处：
--     defect_links.execution_id / defects.execution_id
-- 这两个列原来都没有索引，十几万条失败执行时会退化成全表扫。
CREATE INDEX IF NOT EXISTS ix_defect_links_execution
    ON defect_links (execution_id);
CREATE INDEX IF NOT EXISTS ix_defects_execution
    ON defects (execution_id) WHERE execution_id IS NOT NULL;

-- ═══════════════ 3. 幂等键清理（按时间删历史）═══════════════
-- idempotency_keys 只增不删会一直涨；清理任务按 created_at 删，需要这个索引。
CREATE INDEX IF NOT EXISTS ix_idempotency_created
    ON idempotency_keys (created_at);
