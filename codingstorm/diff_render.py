"""把 unified diff 渲染成 HTML。

**在服务端渲染**，不依赖任何 CDN —— 执行机在国内，前端依赖外部 CDN 不可靠。

⚠️ diff 内容来自被调度的仓库，是**不可信输入**。所有输出都经过 `html.escape`。
"""

from __future__ import annotations

import html
import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def render_diff(diff_text: str) -> str:
    """返回一段 HTML。调用方负责把它放进页面。"""
    if not diff_text.strip():
        return '<div class="diff-empty">没有改动</div>'

    out: list[str] = ['<div class="diff">']
    in_table = False
    old_no = new_no = 0
    truncated = False

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            out.append("</table></div>")
            in_table = False

    for raw in diff_text.splitlines():
        if raw.startswith("[diff 已截断"):
            truncated = True
            break

        if raw.startswith("diff --git "):
            close_table()
            path = _extract_path(raw)
            out.append(f'<div class="diff-file"><div class="diff-file-head">{html.escape(path)}</div>')
            continue

        if raw.startswith(("index ", "old mode", "new mode", "similarity index",
                           "dissimilarity index", "rename from", "rename to",
                           "new file mode", "deleted file mode")):
            continue

        if raw.startswith("--- ") or raw.startswith("+++ "):
            continue

        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            out.append(f'<div class="diff-note">{html.escape(raw)}</div>')
            continue

        m = HUNK_RE.match(raw)
        if m:
            close_table()
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            out.append('<div class="diff-hunk">')
            out.append(f'<div class="diff-hunk-head">{html.escape(raw)}</div>')
            out.append('<table class="diff-table">')
            in_table = True
            continue

        if not in_table:
            # 进不了表格的行（比如 "\ No newline at end of file"）
            out.append(f'<div class="diff-note">{html.escape(raw)}</div>')
            continue

        if raw.startswith("\\"):
            out.append(
                f'<tr class="note"><td class="ln"></td><td class="ln"></td>'
                f'<td class="code">{html.escape(raw)}</td></tr>'
            )
            continue

        if raw.startswith("+"):
            out.append(_row("add", "", new_no, raw[1:]))
            new_no += 1
        elif raw.startswith("-"):
            out.append(_row("del", old_no, "", raw[1:]))
            old_no += 1
        else:
            body = raw[1:] if raw.startswith(" ") else raw
            out.append(_row("ctx", old_no, new_no, body))
            old_no += 1
            new_no += 1

    close_table()
    if truncated:
        out.append('<div class="diff-note">（diff 过长已截断）</div>')
    out.append("</div>")
    return "".join(out)


def _row(kind: str, old_no: int | str, new_no: int | str, body: str) -> str:
    return (
        f'<tr class="{kind}">'
        f'<td class="ln">{old_no}</td>'
        f'<td class="ln">{new_no}</td>'
        f'<td class="code">{html.escape(body) or "&nbsp;"}</td>'
        f"</tr>"
    )


def _extract_path(line: str) -> str:
    """从 `diff --git a/x b/x` 里取一个可读路径。"""
    parts = line.split(" ", 3)
    if len(parts) >= 4:
        return parts[3].split(" b/")[-1]
    return line
