-- 由《测试数据管理平台_技术方案》§2 的 DDL 生成，已在 PG 16.9 实跑通过（16 表 / 37 索引）
-- ═══════════════ 主数据 ═══════════════
CREATE TABLE projects (
    id         BIGSERIAL PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,              -- 如 U8-CITY-NOA-2025Q3
    name       TEXT NOT NULL,
    model      TEXT NOT NULL,                     -- 车型
    stage      TEXT NOT NULL DEFAULT 'testing'    -- testing|releasing|released|archived
);

CREATE TABLE vehicles (
    id          BIGSERIAL PRIMARY KEY,
    vin         TEXT NOT NULL UNIQUE,
    model       TEXT NOT NULL,
    plate_no    TEXT,
    status      TEXT NOT NULL DEFAULT 'available', -- available|in_test|maintenance|retired
    location    TEXT NOT NULL DEFAULT ''           -- 试验场/门店
);

CREATE TABLE versions (
    id         BIGSERIAL PRIMARY KEY,
    model      TEXT NOT NULL,
    ver        TEXT NOT NULL,                      -- 软件版本号
    released   BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (model, ver)
);

CREATE TABLE testers (
    id          BIGSERIAL PRIMARY KEY,
    employee_no TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    team        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE test_cases (
    id          BIGSERIAL PRIMARY KEY,
    case_no     TEXT NOT NULL UNIQUE,              -- TC-NOA-0123
    module      TEXT NOT NULL,                     -- 领航/泊车/HMI…
    title       TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'P2',        -- P0..P3
    precondition TEXT NOT NULL DEFAULT '',
    steps_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_cases_module_priority ON test_cases (module, priority);

-- ═══════════════ ① 车辆档期（本项目核心：并发占用）═══════════════
-- 一行 = 一台车的一个半天。**占用的本质是把 status 从 free 原子改成 booked**，
-- 就像扣库存；但因为有唯一约束 (vehicle_id, slot_date, half)，
-- "同一台车同一半天只能有一个有效占用"由 DB 保证。
CREATE TABLE vehicle_slots (
    id         BIGSERIAL PRIMARY KEY,
    vehicle_id BIGINT NOT NULL REFERENCES vehicles(id),
    slot_date  DATE NOT NULL,
    half       SMALLINT NOT NULL CHECK (half IN (0, 1)),   -- 0=上午 1=下午
    status     TEXT NOT NULL DEFAULT 'free',               -- free|booked|maintenance
    booking_id BIGINT,                                     -- 占用人（预约单）
    version    BIGINT NOT NULL DEFAULT 0,                  -- 乐观锁版本
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vehicle_id, slot_date, half)
);
CREATE INDEX ix_slots_free ON vehicle_slots (slot_date, half, status);

CREATE TABLE bookings (
    id          BIGSERIAL PRIMARY KEY,
    booking_no  TEXT NOT NULL UNIQUE,
    project_id  BIGINT NOT NULL REFERENCES projects(id),
    vehicle_id  BIGINT NOT NULL REFERENCES vehicles(id),
    testers_id  BIGINT NOT NULL REFERENCES testers(id),
    slot_date   DATE NOT NULL,
    half        SMALLINT NOT NULL CHECK (half IN (0, 1)),
    status      TEXT NOT NULL DEFAULT 'active',    -- active|cancelled|done
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 取消后可复用同一档期：只对有效预约做唯一（部分索引）
CREATE UNIQUE INDEX ux_booking_active
    ON bookings (vehicle_id, slot_date, half) WHERE status = 'active';

-- ═══════════════ ② 执行任务池（抢占式领取）═══════════════
CREATE TABLE execution_queue (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id),
    version_id BIGINT NOT NULL REFERENCES versions(id),
    case_id    BIGINT NOT NULL REFERENCES test_cases(id),
    status     TEXT NOT NULL DEFAULT 'pending',    -- pending|claimed|running|done|cancelled
    claimed_by BIGINT REFERENCES testers(id),
    claimed_at TIMESTAMPTZ,
    version    BIGINT NOT NULL DEFAULT 0,          -- 乐观锁：领取时校验
    UNIQUE (project_id, version_id, case_id)
);
CREATE INDEX ix_queue_pending ON execution_queue (project_id, status);

CREATE TABLE executions (
    id          BIGSERIAL PRIMARY KEY,
    queue_id    BIGINT NOT NULL REFERENCES execution_queue(id),
    vehicle_id  BIGINT REFERENCES vehicles(id),
    tester_id   BIGINT NOT NULL REFERENCES testers(id),
    result      TEXT NOT NULL,                     -- pass|fail|blocked|skip
    note        TEXT NOT NULL DEFAULT '',
    executed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exec_summary ON executions (executed_at, result);

-- ═══════════════ ③ 问题单（状态机 + 审计）═══════════════
CREATE TABLE defects (
    id            BIGSERIAL PRIMARY KEY,
    defect_no     TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    severity      TEXT NOT NULL,                   -- S1..S4
    status        TEXT NOT NULL DEFAULT 'open',
    project_id    BIGINT NOT NULL REFERENCES projects(id),
    execution_id  BIGINT REFERENCES executions(id),-- 来源执行记录（可为空=人工提报）
    owner_id      BIGINT REFERENCES testers(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 深分页必须靠它（keyset）
CREATE INDEX ix_defects_keyset ON defects (created_at DESC, id DESC);
CREATE INDEX ix_defects_project_status ON defects (project_id, status, created_at DESC);

CREATE TABLE defect_transitions (
    id         BIGSERIAL PRIMARY KEY,
    defect_id  BIGINT NOT NULL REFERENCES defects(id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同一缺陷在多车型/多版本复现 → 关联多条执行记录（合并去重的依据）
CREATE TABLE defect_links (
    id           BIGSERIAL PRIMARY KEY,
    defect_id    BIGINT NOT NULL REFERENCES defects(id),
    execution_id BIGINT NOT NULL REFERENCES executions(id),
    UNIQUE (defect_id, execution_id)
);

-- ═══════════════ ④ 日报汇总（对账的一方）═══════════════
CREATE TABLE daily_exec_summary (
    id          BIGSERIAL PRIMARY KEY,
    biz_date    DATE NOT NULL,
    project_id  BIGINT NOT NULL REFERENCES projects(id),
    version_id  BIGINT NOT NULL REFERENCES versions(id),
    total       INTEGER NOT NULL DEFAULT 0,
    passed      INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (biz_date, project_id, version_id)
);

-- ═══════════════ ⑤ 幂等 ═══════════════
CREATE TABLE idempotency_keys (
    id          BIGSERIAL PRIMARY KEY,
    scope       TEXT NOT NULL,          -- claim_task / submit_result / create_booking
    key         TEXT NOT NULL,
    body_hash   TEXT NOT NULL,          -- 同 key 换 body → 422
    status_code INTEGER NOT NULL,
    response    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, key)                 -- ← 幂等唯一防线
);

-- ═══════════════ ⑥ 本地消息表（跨项目同步/汇总异步化）═══════════════
CREATE TABLE outbox (
    id         BIGSERIAL PRIMARY KEY,
    topic      TEXT NOT NULL,           -- exec_result_recorded / slot_released
    payload    JSONB NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending|done|dead
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_outbox_due ON outbox (status, next_at);

-- ═══════════════ ⑦ 对账差异 ═══════════════
CREATE TABLE recon_discrepancies (
    id         BIGSERIAL PRIMARY KEY,
    biz_date   DATE NOT NULL,
    project_id BIGINT NOT NULL,
    kind       TEXT NOT NULL,
    expected   INTEGER NOT NULL,
    actual     INTEGER NOT NULL,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolved   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (biz_date, project_id, kind)
);
