#!/usr/bin/env bash
# 服务器上的一次性部署 / 更新脚本（Ubuntu / Debian 系，已装 Docker）。
#
#   ./deploy/deploy.sh            # 首次或更新（会重新构建镜像）
#   ./deploy/deploy.sh --check    # 只跑上线自检
#
# 它做的事，就是"每次上线都要做、但人一定会漏"的那几步：
#   1. 检查 .env 是否存在、必填项有没有填；
#   2. 构建 + 起服务（migrate 会先把库迁到最新并灌演示数据）；
#   3. 等 /readyz 真正就绪，而不是 up -d 完就宣布成功；
#   4. 跑一遍 prod_check.py，有 FAIL 就把它标红。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
COMPOSE="docker compose -f docker-compose.prod.yml"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()  { printf '\033[32m  [OK] %s\033[0m\n' "$*"; }
bad() { printf '\033[31m  [FAIL] %s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--check" ]]; then
    exec python scripts/prod_check.py --base-url "$BASE_URL"
fi

log "1/4 检查 .env"
if [[ ! -f .env ]]; then
    bad "没有 .env。先执行：cp .env.example .env，然后填 TDP_JWT_SECRET / POSTGRES_PASSWORD"
    exit 1
fi
for key in TDP_JWT_SECRET POSTGRES_PASSWORD; do
    value="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true)"
    if [[ -z "${value}" || "${value}" == 换成* ]]; then
        bad "$key 还没填（当前：${value:-空}）"
        exit 1
    fi
done
ok ".env 必填项已填"

log "2/4 构建并启动"
$COMPOSE up -d --build
ok "容器已启动"

log "3/4 等待就绪"
for i in $(seq 1 60); do
    if curl -fsS "$BASE_URL/readyz" 2>/dev/null | grep -q '"db": *"ok"'; then
        ok "/readyz 就绪（${i}s）"
        break
    fi
    if [[ $i -eq 60 ]]; then
        bad "90 秒还没就绪，看日志：$COMPOSE logs --tail=80 app"
        exit 1
    fi
    sleep 1.5
done

log "4/4 上线自检"
if python scripts/prod_check.py --base-url "$BASE_URL"; then
    ok "可以对外开放了"
    echo
    echo "  界面：  $BASE_URL/ui/"
    echo "  接口文档：$BASE_URL/docs"
    echo "  只读演示账号：见 .env 里的 TDP_DEMO_USER"
else
    bad "自检有 FAIL，修完再开放（常见：JWT 密钥太弱 / 默认 admin 口令没改 / 前端没打进镜像）"
    exit 1
fi
