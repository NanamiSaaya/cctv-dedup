#!/usr/bin/env bash
# cron 每 6 小时调用。
#   测源：直连（代理会绕一圈，分片停顿测不准）
#   push：走 mihomo 代理（直连 GitHub 被 reset）
set -u
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PATH=/usr/local/bin:/usr/bin:/bin
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

LOG=./run.log
{
  echo "===== $(date '+%F %T') start ====="

  # 关键：先刷新候选池。
  # 不刷新的话 pick.py 会退回去读上一轮的成品 cctv.m3u，
  # 候选越挑越窄，几轮后源就枯竭了。
  echo '--- 刷新候选池 ---'
  GIT_PROXY=http://127.0.0.1:7890 "$PY" dedupe.py 2>&1 | tail -3

  "$PY" publish.py 2>&1
  echo "===== $(date '+%F %T') end rc=$? ====="
} >> "$LOG" 2>&1

tail -n 800 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
