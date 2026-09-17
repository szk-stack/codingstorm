#!/bin/bash
# PreToolUse 拦截脚本示例。
#
# 装配方式（settings.json）：
#   {"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
#     {"type":"command","command":"<此脚本的绝对路径>"}]}]}}
#
# 协议：从 stdin 收 JSON，退出码 2 = 阻断，stderr 内容会回喂给模型。
#
# Phase 0 已验证：在 --permission-mode bypassPermissions 下此 hook 依然会触发。

LOG="${GUARD_LOG:-$HOME/codingstorm/spike/out/guard.log}"
mkdir -p "$(dirname "$LOG")"

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("tool_name",""))' 2>/dev/null)
CMD=$(printf '%s' "$INPUT"  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)

printf 'tool=%s cmd=%s\n' "$TOOL" "$CMD" >> "$LOG"

block() {
  echo "BLOCKED: $1" >&2
  exit 2
}

case "$CMD" in
  *"git push"*)
    block "git push 在此环境被禁止" ;;
  *"git remote add"*|*"git remote set-url"*)
    block "修改 remote 在此环境被禁止" ;;
esac

# 网络出口（按需启用；会同时挡掉正常的依赖安装）
# case "$CMD" in
#   *curl*|*wget*|*"npm install"*|*"pip install"*)
#     block "网络出口在此环境被禁止" ;;
# esac

exit 0
