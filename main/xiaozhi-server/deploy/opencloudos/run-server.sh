#!/usr/bin/env bash
set -euo pipefail
[[ $(id -un) == appuser ]] || { echo '请以 appuser 身份运行'; exit 1; }
source /home/appuser/miniforge3/etc/profile.d/conda.sh
conda activate /home/appuser/.conda/envs/xiaozhi
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1
cd /home/appuser/xiaozhi-server
exec "$CONDA_PREFIX/bin/python" -u app.py
