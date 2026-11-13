#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/appsvc/assistant/xiaozhi-server}"
export APP_DIR
cd "$APP_DIR"
INTERVAL="${WATCHDOG_INTERVAL:-10}"
[[ "$INTERVAL" =~ ^[1-9][0-9]*$ ]] || { echo 'WATCHDOG_INTERVAL 必须为正整数。' >&2; exit 1; }
mkdir -p logs tmp

# 锁由后台看门狗持有，退出后自动释放。
exec 8>"$APP_DIR/xiaozhi-watchdog.pid.lock"
flock -n 8 || { echo '看门狗已在运行。'; exit 0; }

if [[ "${1:-}" != --run ]]; then
  nohup bash "$APP_DIR/watchdog.sh" --run >>"$APP_DIR/logs/watchdog.log" 2>&1 </dev/null &
  echo "看门狗已提交后台启动，PID：$!；检查间隔：${INTERVAL}秒。"
  echo "日志：tail -f $APP_DIR/logs/watchdog.log"
  exit 0
fi

echo "[$(date '+%F %T')] 看门狗启动，检查间隔：${INTERVAL}秒。"
while true; do
  if [[ ! -f tmp/xiaozhi-server.stopped ]]; then
    pid=''
    [[ ! -f xiaozhi-server.pid ]] || pid=$(cat xiaozhi-server.pid)
    if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] || ! kill -0 "$pid" 2>/dev/null; then
      echo "[$(date '+%F %T')] 服务进程不存在，尝试启动。"
      # 不能让服务继承看门狗锁。
      bash "$APP_DIR/start.sh" --watchdog 8>&- ||
        echo "[$(date '+%F %T')] 启动失败，将在 ${INTERVAL} 秒后重试。"
    fi
  fi
  sleep "$INTERVAL" 8>&-
done
