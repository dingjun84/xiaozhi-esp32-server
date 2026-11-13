#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/appsvc/assistant/xiaozhi-server}"
cd "$APP_DIR"
APP_DIR=$(pwd -P)
mkdir -p tmp
PID_FILE="$APP_DIR/xiaozhi-server.pid"

exec 9>"$APP_DIR/xiaozhi-server.pid.lock"
flock -w 10 9 || { echo '其他启停操作正在运行，请稍后再试。' >&2; exit 1; }

# 看门狗保留运行，但暂停自动拉起；手动 start.sh 后恢复。
touch tmp/xiaozhi-server.stopped
if [[ ! -f "$PID_FILE" ]]; then
  echo '没有 PID 文件，已暂停看门狗自动拉起。'
  exit 0
fi
pid=$(cat "$PID_FILE")
if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]]; then
  echo 'PID 文件无效，请检查 xiaozhi-server.pid。' >&2
  exit 1
fi

is_running() {
  kill -0 "$pid" 2>/dev/null &&
    [[ $(ps -o stat= -p "$pid") != *Z* ]]
}

if ! is_running; then
  rm -f "$PID_FILE"
  echo '服务已停止，已暂停看门狗自动拉起。'
  exit 0
fi

# 防止旧 PID 被其他进程复用时误杀。此检查针对 Linux / OpenCloudOS。
if [[ $(readlink "/proc/$pid/cwd") != "$APP_DIR" ]] ||
   ! tr '\0' '\n' < "/proc/$pid/cmdline" | grep -qx 'app.py'; then
  echo "PID $pid 与当前服务不匹配，未发送停止信号，请人工检查。" >&2
  exit 1
fi

kill -TERM "$pid"
for ((i = 0; i < 30; i++)); do
  if ! is_running; then
    rm -f "$PID_FILE"
    echo "服务已停止，PID：$pid；看门狗自动拉起已暂停。"
    exit 0
  fi
  sleep 1
done

echo "进程 $pid 在 30 秒内未退出，未强制终止，请检查日志。" >&2
exit 1
