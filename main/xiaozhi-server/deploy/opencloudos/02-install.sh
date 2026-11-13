#!/usr/bin/env bash
# Run as appuser. Argument: externally reachable IPv4 address.
set -euo pipefail
[[ $(id -un) == appuser && $HOME == /home/appuser ]] || {
  echo '请先 sudo -iu appuser，再运行本脚本'; exit 1;
}
[[ $# -eq 1 ]] || { echo '用法：bash deploy/opencloudos/02-install.sh 服务器公网IPv4'; exit 1; }
public_ip=$1
[[ $public_ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo '参数必须是 IPv4 地址'; exit 1;
}
app_dir=/home/appuser/xiaozhi-server
cd "$app_dir"
[[ -f data/.config.yaml ]] || { echo '缺少 data/.config.yaml'; exit 1; }
if systemctl is-active --quiet xiaozhi-server; then
  echo '服务正在运行，请先由管理员 sudo systemctl stop xiaozhi-server，再安装或更新依赖。'
  exit 1
fi
case "$(uname -m)" in
  x86_64|aarch64) arch=$(uname -m) ;;
  *) echo '不支持此 CPU 架构'; exit 1 ;;
esac
forge_dir="$HOME/miniforge3"
env_dir="$HOME/.conda/envs/xiaozhi"
if [[ ! -x "$forge_dir/bin/conda" ]]; then
  [[ ! -e "$forge_dir" ]] || { echo 'miniforge3 目录已存在但安装不完整，请检查后重试'; exit 1; }
  download_dir=$(mktemp -d)
  trap 'rm -rf "$download_dir"' EXIT
  installer="Miniforge3-Linux-${arch}.sh"
  url="https://github.com/conda-forge/miniforge/releases/latest/download/$installer"
  curl -fL --retry 3 "$url" -o "$download_dir/$installer"
  curl -fL --retry 3 "$url.sha256" -o "$download_dir/checksum"
  expected=$(awk 'NR==1 {print $1}' "$download_dir/checksum")
  [[ $expected =~ ^[[:xdigit:]]{64}$ ]] || { echo '安装器校验文件格式异常'; exit 1; }
  printf '%s  %s\n' "$expected" "$download_dir/$installer" | sha256sum -c -
  bash "$download_dir/$installer" -b -p "$forge_dir"
fi
if [[ -x "$env_dir/bin/python" ]]; then
  "$env_dir/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 10), "已有环境不是 Python 3.10"'
  "$forge_dir/bin/conda" install -y -p "$env_dir" --override-channels -c conda-forge python=3.10 pip ffmpeg libopus
else
  "$forge_dir/bin/conda" create -y -p "$env_dir" --override-channels -c conda-forge python=3.10 pip ffmpeg libopus
fi
source "$forge_dir/etc/profile.d/conda.sh"
conda activate "$env_dir"
export LD_LIBRARY_PATH="$env_dir/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip check
# Edit only server networking/auth fields, keeping provider keys and other settings.
python - "$public_ip" <<'PY'
import ipaddress
import os
from pathlib import Path
import secrets
import shutil
import sys
from datetime import datetime
from ruamel.yaml import YAML

ip = ipaddress.IPv4Address(sys.argv[1])
if ip.is_unspecified or ip.is_multicast or ip.is_loopback or str(ip) == '255.255.255.255':
    raise SystemExit('请填写客户端能够访问的服务器 IPv4 地址')
p = Path('data/.config.yaml')
y = YAML()
y.preserve_quotes = True
c = y.load(p.read_text())
if not isinstance(c, dict):
    raise SystemExit('配置必须是 YAML 映射')
if (c.get('manager-api') or {}).get('url'):
    raise SystemExit('本流程适用于无智控台的单服务部署，检测到 manager-api，请单独配置。')
backup = p.with_name('.config.yaml.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
shutil.copy2(p, backup)
os.chmod(backup, 0o600)
s = c.setdefault('server', {})
s.update(ip='0.0.0.0', local_ip=str(ip), port=8000, http_port=8003)
# Explicit URLs avoid accidentally reusing local development addresses.
s['websocket'] = f'ws://{ip}:8000/xiaozhi/v1/'
s['vision_explain'] = f'http://{ip}:8003/mcp/vision/explain'
if not isinstance(s.get('auth_key'), str) or not s['auth_key'] or '你' in s['auth_key']:
    s['auth_key'] = secrets.token_hex(32)
a = s.setdefault('auth', {})
a.update(enabled=True, allowed_devices=[])
with p.open('w') as f:
    y.dump(c, f)
os.chmod(p, 0o600)
defaults = y.load(Path('config.yaml').read_text())
asr = c.get('selected_module', {}).get('ASR', defaults['selected_module']['ASR'])
if asr == 'FunASR' and not Path('models/SenseVoiceSmall/model.pt').exists():
    raise SystemExit('当前选择 FunASR，但缺少 models/SenseVoiceSmall/model.pt，请补齐后再启动。')
print('服务器地址已更新，Token 校验已开启，原配置已备份（权限 600）。')
PY
ffmpeg -version
python - <<'PY'
import ctypes
import torch
import torchaudio
import websockets
ctypes.CDLL('libopus.so.0')
print('基础检查通过：Python、FFmpeg、Opus、Torch、Torchaudio、WebSocket。')
PY
echo '依赖与配置准备完成。可执行 bash deploy/opencloudos/run-server.sh 前台试运行。'
echo '前台测试成功后按 Ctrl+C，返回管理员运行 03-service.sh。'
