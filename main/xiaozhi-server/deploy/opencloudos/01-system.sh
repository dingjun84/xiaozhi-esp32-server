#!/usr/bin/env bash
# Run as root after copying the release into /home/appuser/xiaozhi-server.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo '请通过 sudo bash 运行本脚本'; exit 1; }
[[ -f /etc/os-release ]] || exit 1
. /etc/os-release
[[ ${ID:-} == opencloudos ]] || { echo '本脚本仅适用于 OpenCloudOS'; exit 1; }
case "$(uname -m)" in
  x86_64|aarch64) ;;
  *) echo '仅准备了 x86_64/aarch64 环境安装流程'; exit 1 ;;
esac
app_dir=/home/appuser/xiaozhi-server
[[ -f "$app_dir/app.py" && -f "$app_dir/data/.config.yaml" ]] || {
  echo "请先将发布包复制到 $app_dir（包括 data/.config.yaml）"; exit 1;
}
if ! id appuser >/dev/null 2>&1; then
  useradd -m -s /bin/bash appuser
  echo '已创建 appuser，未设置密码；管理员可用 sudo -iu appuser 切换。'
fi
[[ $(getent passwd appuser | cut -d: -f6) == /home/appuser ]] || {
  echo 'appuser 的主目录不是 /home/appuser，请先调整部署路径。'; exit 1;
}
dnf install -y ca-certificates curl unzip tar bzip2 gcc gcc-c++ make libgomp
app_group=$(id -gn appuser)
chown -R "appuser:$app_group" "$app_dir"
chmod 600 "$app_dir/data/.config.yaml"
echo '系统依赖已准备好。接下来 sudo -iu appuser，执行 02-install.sh。'
echo '已有 SSH 禁止登录规则会保留；本脚本不修改 SSH 和防火墙配置。'
