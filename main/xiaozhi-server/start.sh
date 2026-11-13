#!/usr/bin/env bash
set -euo pipefail

# 默认使用服务器部署路径；可通过 APP_DIR 环境变量覆盖。
APP_DIR="${APP_DIR:-/home/appsvc/assistant/xiaozhi-server}"
cd "$APP_DIR"

[[ -f app.py && -f .venv/bin/activate && -x .venv/bin/python ]] || {
  echo "缺少 app.py 或 .venv 虚拟环境，请检查目录：$APP_DIR" >&2
  exit 1
}

mkdir -p logs tmp
LOG_FILE="$APP_DIR/logs/nohup.log"
PID_FILE="$APP_DIR/xiaozhi-server.pid"

# OpenCloudOS 自带的 flock 防止多个启动脚本同时运行。
exec 9>"$APP_DIR/xiaozhi-server.pid.lock"
flock -w 10 9 || { echo '其他启停操作正在运行，请稍后再试。' >&2; exit 1; }

if [[ "${1:-}" == --watchdog ]]; then
  # 必须在锁内检查，避免与手动停止同时发生。
  [[ ! -f tmp/xiaozhi-server.stopped ]] || exit 0
else
  rm -f tmp/xiaozhi-server.stopped
fi

if [[ -f "$PID_FILE" ]]; then
  pid=$(cat "$PID_FILE")
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "PID $pid 仍在运行，请先确认并停止原进程。"
    exit 1
  fi
fi

source .venv/bin/activate
export PYTHONUNBUFFERED=1
nohup "$APP_DIR/.venv/bin/python" -u app.py >>"$LOG_FILE" 2>&1 </dev/null 9>&- &
pid=$!
printf '%s\n' "$pid" > "$PID_FILE"

sleep 2
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "启动失败，请查看日志：$LOG_FILE" >&2
  tail -n 50 "$LOG_FILE" >&2
  exit 1
fi

echo "进程已启动，PID：$pid（服务就绪情况请查看日志）"
echo "查看日志：tail -f $LOG_FILE"
echo "停止服务：bash $APP_DIR/stop.sh"
