#!/usr/bin/env python3
"""PreToolUse 拦截钩子。

装配方式（settings.json）：

    {"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[
      {"type":"command","command":"python3 /path/to/guard.py"}]}]}}

协议：从 stdin 收 JSON；**退出码 2 = 阻断**，stderr 内容会回喂给模型。

Phase 0 实测（docs/phase0-report.md §3）：在 `--permission-mode bypassPermissions`
下此 hook 依然会触发，且模型会如实汇报被拦截、不会尝试绕过。所以边界不需要靠
`--permission-prompt-tool` 和 MCP server。

规则来自同目录的 `guard.json`；缺失时用保守默认值。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

DEFAULT_RULES = {
    "block_push": True,
    "block_remote_change": True,
    "block_network": False,
    "extra_deny": [],
    "allow": [],
    "audit_log": None,
}

PUSH_RE = re.compile(
    # 要能匹配 `git push`、`git -C /repo push`、`git --no-pager push`、`/usr/bin/git push`
    r"\bgit\b(?:\s+-C\s+\S+|\s+--\S+|\s+-[^\sC]\S*)*\s+push\b"
)
REMOTE_RE = re.compile(r"\bgit\b(?:\s+-C\s+\S+|\s+--\S+|\s+-[^\sC]\S*)*\s+remote\s+(add|set-url|remove|rename)\b")
NETWORK_RE = re.compile(
    r"\b(curl|wget|nc|netcat|telnet|ssh|scp|rsync)\b"
    r"|\b(npm|pnpm|yarn)\s+(install|add|i)\b"
    r"|\bpip[23]?\s+install\b"
    r"|\buv\s+(pip\s+)?(install|add)\b"
    r"|\bgo\s+get\b"
    r"|\bcargo\s+(install|add)\b"
)


def force_utf8() -> None:
    """强制 UTF-8 输出。

    这条 stderr 是要回喂给模型的，按系统 locale 编码（Windows 上是 GBK）写出去
    会变成乱码 —— 测试里就是因为这个把 stderr 整个丢掉了。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def load_rules(here: Path) -> dict:
    rules = dict(DEFAULT_RULES)
    cfg = here / "guard.json"
    if cfg.exists():
        try:
            rules.update(json.loads(cfg.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if not rules.get("audit_log"):
        rules["audit_log"] = str(here / "audit.log")
    return rules


def audit(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    force_utf8()
    here = Path(__file__).resolve().parent
    rules = load_rules(here)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0  # 读不懂就放行，不能因为解析失败把正常流程卡死

    tool = payload.get("tool_name", "")

    if tool in ("WebFetch", "WebSearch"):
        # 网络类工具没有 command，直接按规则判定，不能拿去匹配命令正则
        audit(rules["audit_log"], {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "command": str((payload.get("tool_input") or {}).get("url")
                           or (payload.get("tool_input") or {}).get("query") or ""),
            "session_id": payload.get("session_id"),
        })
        if rules.get("block_network"):
            print(
                "BLOCKED: 网络访问被 codingstorm 的边界规则拦下了（block_network=true）。",
                file=sys.stderr,
            )
            return 2
        return 0

    command = str((payload.get("tool_input") or {}).get("command") or "") if tool == "Bash" else ""

    audit(rules["audit_log"], {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": tool,
        "command": command[:500],
        "session_id": payload.get("session_id"),
    })

    if not command:
        return 0

    for pattern in rules.get("allow") or []:
        if re.search(pattern, command):
            return 0

    deny = list(rules.get("extra_deny") or [])
    if rules.get("block_push"):
        deny.append(PUSH_RE.pattern)
    if rules.get("block_remote_change"):
        deny.append(REMOTE_RE.pattern)
    if rules.get("block_network"):
        deny.append(NETWORK_RE.pattern)

    for pattern in deny:
        try:
            if re.search(pattern, command):
                print(
                    f"BLOCKED: 这条命令被 codingstorm 的边界规则拦下了"
                    f"（匹配 /{pattern}/）。如果确实需要执行，请在平台上调整规则。",
                    file=sys.stderr,
                )
                return 2
        except re.error:
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
