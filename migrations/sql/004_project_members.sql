-- 004：项目成员表（ABAC 的数据来源）
--
-- 为什么权限边界是"项目"：这个系统里所有业务数据都挂在 project 下
-- （档期、任务、执行结果、问题单、日报），项目也是现实组织里的协作边界。
--
-- 为什么不复用 app_users.role：角色回答"能做什么动作"（读/写/管理），
-- 成员关系回答"能碰哪些数据"。两个维度正交：
-- 同样是 tester，A 只在 U8 项目里、B 只在另一个车型项目里。
CREATE TABLE IF NOT EXISTS project_members (
    id         BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    username   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, username)
);

-- 反查"某人属于哪些项目"是列表接口的必经路径
CREATE INDEX IF NOT EXISTS ix_project_members_user
    ON project_members (username);
