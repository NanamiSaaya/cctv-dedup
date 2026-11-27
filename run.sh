#!/usr/bin/env bash
# cron 每 6 小时调用。测源直连，push 走 mihomo 代理。
set -u
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PATH=/usr/local/bin:/usr/bin:/bin

# 测源必须直连：清掉全局代理（publish.py 内部只给 git 单独加代理）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

LOG=./run.log
{
  echo "===== $(date '+%F %T') start ($PY) ====="
  "$PY" publish.py 2>&1
  echo "===== $(date '+%F %T') end rc=$? ====="
} >> "$LOG" 2>&1

tail -n 600 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
