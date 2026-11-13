#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo '请通过 sudo bash 运行本脚本'; exit 1; }
app_dir=/home/appuser/xiaozhi-server
[[ -x /home/appuser/.conda/envs/xiaozhi/bin/python && -f "$app_dir/deploy/opencloudos/run-server.sh" ]] || {
  echo '请先完成 01-system.sh 和 02-install.sh'; exit 1;
}
service_file=/etc/systemd/system/xiaozhi-server.service
if [[ -f "$service_file" ]]; then
  cp -p "$service_file" "$service_file.backup-$(date +%Y%m%d-%H%M%S)"
fi
cat > "$service_file" <<'UNIT'
[Unit]
Description=Xiaozhi Voice Server
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=appuser
WorkingDirectory=/home/appuser/xiaozhi-server
Environment=HOME=/home/appuser
ExecStart=/bin/bash /home/appuser/xiaozhi-server/deploy/opencloudos/run-server.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
systemd-analyze verify "$service_file"
systemctl daemon-reload
systemctl enable xiaozhi-server
systemctl restart xiaozhi-server
systemctl --no-pager --full status xiaozhi-server
echo '已发出启动请求；还需检查监听端口、OTA 和实际语音对话。'
echo '日志：sudo journalctl -u xiaozhi-server -n 100 --no-pager'
