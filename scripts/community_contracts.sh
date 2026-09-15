#!/usr/bin/env bash
set -euo pipefail
# Executed only in the temporary CI Compose environment.
export DATABASE_URL="mysql+pymysql://root:${MYSQL_ROOT_PASSWORD}@127.0.0.1:3306/growth_log?charset=utf8mb4"
# Some inherited tests create temporary runners beside their sources.
# Copy only source directories into this disposable container's scratch area.
scratch=$(mktemp -d /tmp/growthlog-community-contracts.XXXXXX)
cp -R /workspace/backend /workspace/frontend "$scratch/"
cd "$scratch"
python backend/scripts/test_label_list_isolation.py
python backend/scripts/test_r1130_query_planner.py
python backend/scripts/test_r1112_notion_contracts.py
python backend/scripts/test_r110_todo_ai_sources.py
python backend/scripts/test_todo_priority.py
python backend/scripts/test_todo_completion_note.py
python backend/scripts/test_r7_today_plan.py
python backend/scripts/community_notifications.py
python -m compileall -q /app/app /app/scripts
