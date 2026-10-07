#!/usr/bin/env bash
# Backend tests against a throwaway PostgreSQL container (compose.test.yaml) that is removed again on exit.
# Usage: scripts/test_backend.sh [pytest args]   (default: backend/tests tests runner)
#        PYTHON=/path/to/python scripts/test_backend.sh
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROJECT=codex-hub-v1-test
PYTHON=${PYTHON:-$ROOT/.venv/bin/python}
compose() { docker compose -p "$PROJECT" -f "$ROOT/compose.test.yaml" "$@"; }

# A leftover from an interrupted run is never reused or silently replaced.
if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]; then
  echo "已存在 $PROJECT 的容器（上次未清理）。确认后执行：docker compose -p $PROJECT -f compose.test.yaml down -v" >&2
  exit 1
fi
cat <<EOF
将临时创建（结束自动删除）：
  容器 ${PROJECT}-db-1：postgres:16.14-bookworm linux/amd64，复用本机镜像、不拉取
  网络 ${PROJECT}_default（新建，不加入已有网络）；无卷，数据在 tmpfs
  端口 127.0.0.1:<Docker 分配的空闲端口>，不占用固定端口；资源上限 1 CPU / 512MB
EOF

HUB_TEST_DB_PASSWORD=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')
export HUB_TEST_DB_PASSWORD
trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
trap 'exit 130' INT TERM
compose up -d --wait
PORT=$(compose port db 5432 | sed 's/.*://')

[ $# -gt 0 ] || set -- backend/tests tests runner
cd "$ROOT"
DATABASE_URL="postgresql+psycopg://hub_test:$HUB_TEST_DB_PASSWORD@127.0.0.1:$PORT/agent_hub_test" \
  PYTHONPATH=backend "$PYTHON" -m pytest "$@" -q
