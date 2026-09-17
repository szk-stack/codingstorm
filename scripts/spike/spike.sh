#!/bin/bash
# Phase 0 验证脚本。在跑 Claude Code 的执行机上运行。
# 结论见 docs/phase0-report.md。
set -u

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SPIKE_DIR/out"
mkdir -p "$OUT"

# 所有 claude 调用都带 </dev/null —— 否则会继承调用者的 stdin（Phase 0 踩过）
CLAUDE_BASE=(claude -p --output-format stream-json --verbose)

hr() { printf '\n########## %s ##########\n' "$1"; }

t_smoke() {
  hr "smoke：基本连通性 + 事件序列"
  timeout 120 "${CLAUDE_BASE[@]}" "Reply with exactly: OK" \
    </dev/null > "$OUT/smoke.ndjson" 2> "$OUT/smoke.err"
  echo "退出码=$?  行数=$(wc -l < "$OUT/smoke.ndjson")  stderr行数=$(wc -l < "$OUT/smoke.err")"
  python3 "$SPIKE_DIR/analyze.py" "$OUT/smoke.ndjson" --summary
}

t_verbose() {
  hr "verbose：不传 --verbose 会怎样"
  timeout 90 claude -p "Say OK" --output-format stream-json \
    </dev/null > "$OUT/noverbose.ndjson" 2> "$OUT/noverbose.err"
  echo "退出码=$?  stdout行数=$(wc -l < "$OUT/noverbose.ndjson")"
  echo "stderr: $(head -c 300 "$OUT/noverbose.err")"
}

t_add_dir() {
  hr "add-dir：能否读工作目录之外的文件"
  mkdir -p "$OUT/ctx"
  echo "SECRET_WORD=platypus" > "$OUT/ctx/context.md"
  timeout 120 "${CLAUDE_BASE[@]}" \
    --permission-mode bypassPermissions --max-turns 6 \
    --add-dir "$OUT/ctx" \
    "Read the file $OUT/ctx/context.md and reply with only the value after the equals sign." \
    </dev/null > "$OUT/adddir.ndjson" 2> "$OUT/adddir.err"
  echo "退出码=$?"
  python3 "$SPIKE_DIR/analyze.py" "$OUT/adddir.ndjson" --result
}

t_max_turns() {
  hr "max-turns：用尽时的行为"
  timeout 120 "${CLAUDE_BASE[@]}" \
    --permission-mode bypassPermissions --max-turns 2 \
    "Run the bash command 'echo tick' ten separate times, one at a time." \
    </dev/null > "$OUT/maxturns.ndjson" 2> "$OUT/maxturns.err"
  echo "退出码=$?"
  python3 "$SPIKE_DIR/analyze.py" "$OUT/maxturns.ndjson" --result
}

t_lines() {
  hr "lines：行长分布（评估 64 KiB PIPE 风险）"
  python3 -c "
with open('$OUT/big.txt','w') as f:
    for i in range(20000):
        f.write(f'line {i:06d} ' + 'x'*8 + '\n')
"
  echo "big.txt = $(stat -c%s "$OUT/big.txt") 字节"
  timeout 180 "${CLAUDE_BASE[@]}" \
    --permission-mode bypassPermissions --max-turns 6 \
    "Run exactly this shell command, then report the character count of its output: python3 -c \"print('A'*120000)\"" \
    </dev/null > "$OUT/lines.ndjson" 2> "$OUT/lines.err"
  for f in "$OUT"/*.ndjson; do
    [ -f "$f" ] || continue
    awk -v n="$(basename "$f")" '
      { if (length>m) m=length; if (length>65536) c++ }
      END { printf "  %-24s 最长行=%-8d 超64KiB的行=%d\n", n, m, c+0 }
    ' "$f"
  done
}

t_hook() {
  hr "hook：bypassPermissions 下 PreToolUse 是否触发"
  H="$OUT/hooktest"
  rm -rf "$H"; mkdir -p "$H/.claude"
  git init -q --bare "$OUT/fake-remote.git"

  ( cd "$H"
    git init -q -b main
    git config user.email spike@localhost
    git config user.name spike
    echo "hook test" > readme.md
    git add readme.md && git commit -q -m init
    git remote add origin "$OUT/fake-remote.git" )

  cat > "$H/.claude/settings.json" <<EOF
{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
  {"type":"command","command":"$SPIKE_DIR/guard.sh"}]}]}}
EOF

  export GUARD_LOG="$OUT/guard.log"
  rm -f "$GUARD_LOG"

  ( cd "$H" && timeout 150 "${CLAUDE_BASE[@]}" \
      --permission-mode bypassPermissions --max-turns 6 \
      "Run this exact shell command and report its output verbatim: git push origin main" \
      </dev/null > "$OUT/hook.ndjson" 2> "$OUT/hook.err" )
  echo "退出码=$?"

  if [ -f "$GUARD_LOG" ]; then
    echo "hook 触发 $(wc -l < "$GUARD_LOG") 次；含 git push 的记录："
    grep -c 'git push' "$GUARD_LOG" | sed 's/^/  /'
  else
    echo "hook 未触发"
  fi
  echo "远端 refs（为空 = 成功阻断）:"
  git -C "$OUT/fake-remote.git" for-each-ref | sed 's/^/  /'
  python3 "$SPIKE_DIR/analyze.py" "$OUT/hook.ndjson" --tool-results | head -6
}

case "${1:-all}" in
  smoke)     t_smoke ;;
  verbose)   t_verbose ;;
  add-dir)   t_add_dir ;;
  max-turns) t_max_turns ;;
  lines)     t_lines ;;
  hook)      t_hook ;;
  all)       t_smoke; t_verbose; t_add_dir; t_max_turns; t_lines; t_hook ;;
  *)         echo "未知参数：$1"; sed -n '2,20p' "$SPIKE_DIR/README.md"; exit 1 ;;
esac

echo
echo "产物在 $OUT"
