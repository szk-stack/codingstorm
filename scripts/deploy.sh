#!/bin/bash
# 部署到执行机并重启服务。
#
#   ./scripts/deploy.sh [ssh别名]      # 默认 tencent
#
# 前置条件（只需做一次）：
#   ssh <别名> 'cd ~/codingstorm && python3 -m venv .venv && \
#     .venv/bin/pip install fastapi uvicorn websockets -i https://pypi.tuna.tsinghua.edu.cn/simple'
#   以及 ~/codingstorm/codingstorm.toml（参考 codingstorm.example.toml）
set -euo pipefail

HOST="${1:-tencent}"
REMOTE_ROOT='$HOME/codingstorm'
PORT=8788

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> 上传代码到 $HOST"
tar czf - codingstorm codingstorm.example.toml prices.example.toml \
  | ssh "$HOST" "rm -rf $REMOTE_ROOT/app/codingstorm && mkdir -p $REMOTE_ROOT/app && cd $REMOTE_ROOT/app && tar xzf -"

# 重启。这一段必须幂等且确定 —— 之前只 pkill + sleep 1，旧进程还在优雅退出
# （等运行中的任务收尾）时就启动了新的，结果**旧版本一直占着端口在服务**，
# 改了代码却看不到效果，排查了很久。
echo "==> 停止旧进程"
# 注意 `pgrep -f "codingstorm.app"` 这种宽泛模式会把**执行检查的 shell 自身**也算进去
# （它的命令行里含有这个字符串），数出来永远是多的。所以匹配更具体的完整路径。
ssh "$HOST" bash -s <<REMOTE
set -u
PIDS=\$(pgrep -f "venv/bin/python -m codingstorm.app" || true)
if [ -n "\$PIDS" ]; then
  kill \$PIDS 2>/dev/null || true
  for i in \$(seq 1 30); do
    pgrep -f "venv/bin/python -m codingstorm.app" >/dev/null || break
    sleep 1
  done
  LEFT=\$(pgrep -f "venv/bin/python -m codingstorm.app" || true)
  if [ -n "\$LEFT" ]; then
    echo "    优雅退出超时，强制终止: \$LEFT"
    kill -9 \$LEFT 2>/dev/null || true
    sleep 1
  fi
fi
if pgrep -f "venv/bin/python -m codingstorm.app" >/dev/null; then
  echo "    ✗ 仍有进程无法终止，中止部署"
  exit 1
fi
if ss -lntH 2>/dev/null | grep -q ":$PORT "; then
  echo "    ✗ 端口 $PORT 仍被占用，中止部署"
  exit 1
fi
echo "    已清空，端口已释放"
REMOTE

echo "==> 启动服务"
ssh "$HOST" bash -s <<REMOTE
set -e
cd \$HOME/codingstorm/app
nohup env PYTHONPATH=\$HOME/codingstorm/app \$HOME/codingstorm/.venv/bin/python -m codingstorm.app \\
  --config \$HOME/codingstorm/codingstorm.toml > \$HOME/codingstorm/app.log 2>&1 &
echo \$! > \$HOME/codingstorm/app.pid
sleep 4
if ! curl -sS -m 5 -o /dev/null http://127.0.0.1:$PORT/api/health; then
  echo "    ✗ 健康检查失败，最后几行日志："
  tail -8 \$HOME/codingstorm/app.log
  exit 1
fi
COUNT=\$(pgrep -f "venv/bin/python -m codingstorm.app" | wc -l)
if [ "\$COUNT" != "1" ]; then
  echo "    ✗ 期望恰好 1 个进程，实际 \$COUNT 个"
  exit 1
fi
echo "    服务已就绪（pid \$(cat \$HOME/codingstorm/app.pid)，端口 $PORT）"
grep -a "已装配边界拦截钩子\|未配置价目表" \$HOME/codingstorm/app.log | tail -2 | sed 's/^/    /'
REMOTE

echo "==> 完成。日志：ssh $HOST 'tail -f ~/codingstorm/app.log'"
