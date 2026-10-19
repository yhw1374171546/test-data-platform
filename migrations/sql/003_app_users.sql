-- 003：认证用户表（JWT 账号体系）
--
-- 为什么单独一张表而不是复用 testers：
-- `testers` 是**业务实体**（谁去执行用例），`app_users` 是**访问凭证**（谁能调接口）。
-- 两者生命周期完全不同：测试员离职要把账号停用，但他的历史执行记录必须留着；
-- 反过来，一个管理员可能根本不执行用例。混在一张表里，早晚会出现
-- "停用账号把历史数据一起搅了"这种事。
CREATE TABLE IF NOT EXISTS app_users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                 -- pbkdf2_sha256$轮数$盐$哈希，绝不存明文
    role          TEXT NOT NULL DEFAULT 'viewer'
                  CHECK (role IN ('admin', 'tester', 'viewer')),
    active        BOOLEAN NOT NULL DEFAULT TRUE, -- 停用账号后不能再登录
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_app_users_active ON app_users (active) WHERE active;
