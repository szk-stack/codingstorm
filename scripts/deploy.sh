#!/bin/bash
# 部署到执行机并重启服务。
#
#   ./scripts/deploy.sh [ssh别名]      # 默认 tencent
#
# 前置条件（只需做一次）：
#   ssh <别名> 'cd ~/codingstorm && python3 -m venv .venv && \
#     .venv/bin/pip install fastapi uvicorn -i https://pypi.tuna.tsinghua.edu.cn/simple'
#   以及 ~/codingstorm/codingstorm.toml（参考 codingstorm.example.toml）
set -euo pipefail

HOST="${1:-tencent}"
REMOTE_ROOT='$HOME/codingstorm'

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> 上传代码到 $HOST"
tar czf - codingstorm codingstorm.example.toml \
  | ssh "$HOST" "rm -rf $REMOTE_ROOT/app/codingstorm && mkdir -p $REMOTE_ROOT/app && cd $REMOTE_ROOT/app && tar xzf -"

echo "==> 重启服务"
ssh "$HOST" bash -s <<REMOTE
set -e
pkill -f 'codingstorm.app' 2>/dev/null || true
sleep 1
cd \$HOME/codingstorm/app
nohup env PYTHONPATH=\$HOME/codingstorm/app \$HOME/codingstorm/.venv/bin/python -m codingstorm.app \\
  --config \$HOME/codingstorm/codingstorm.toml > \$HOME/codingstorm/app.log 2>&1 &
echo \$! > \$HOME/codingstorm/app.pid
sleep 3
tail -5 \$HOME/codingstorm/app.log
REMOTE

echo "==> 完成。日志：ssh $HOST 'tail -f ~/codingstorm/app.log'"
