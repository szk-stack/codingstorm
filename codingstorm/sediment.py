"""任务结束后的自动沉淀。

产出写进项目的 `journal.md`。**不能只记「改了什么」** —— 要记
「为什么改、何时可删」，否则文件会只增不减：实测指令类文件约 8 个月增长 226%，
而且规则的「可删除危险度」随年龄*下降*（老规则留着不是因为有用，
是因为没人能证明它没用）。三个字段把「删不删」从开放判断变成封闭谓词。

这是一次独立、轻量的模型调用，不走 runner —— 免得它的输出混进任务的执行流里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codingstorm.config import Config
from codingstorm.context import ContextStore
from codingstorm.models import TaskOut

log = logging.getLogger("codingstorm.sediment")

PROMPT_TEMPLATE = """\
你在为「{project}」维护变更记录。下面是一次刚完成的任务和它产生的改动。

## 任务
{title}
{body}

## 改动摘要
{stat}

## 改动内容
```diff
{diff}
```

写一条变更记录，直接输出 markdown，格式**严格**如下：

## <一句话说明这次改了什么>（{date}）
<!--
source: 为什么要改 —— 观察到的问题、需求来源，或触发的失败
applicability: 什么情况下这条记录相关（改到哪部分代码、做哪类任务时该看它）
expiry: 什么条件下这条记录可以删除（比如某模块重构后、某约束不再成立时）
-->
<一到三句话：具体做了什么、有什么后续需要注意的>

只输出这条记录本身，不要任何额外说明或前后缀。若这次改动不值得记录（比如只是格式化），
只输出一个词：SKIP
"""


@dataclass
class SedimentResult:
    text: str | None
    skipped: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model: str | None = None
    duration_ms: int | None = None
    error: str | None = None


def build_prompt(
    project_name: str, task: TaskOut, diff: str, stat: str, *, max_diff_bytes: int
) -> str:
    raw = diff.encode("utf-8")
    if len(raw) > max_diff_bytes:
        diff = raw[:max_diff_bytes].decode("utf-8", "ignore") + "\n…（diff 过长已截断）"
    return PROMPT_TEMPLATE.format(
        project=project_name,
        title=task.title,
        body=task.body or "（无）",
        stat=stat.strip() or "（无改动）",
        diff=diff.strip() or "（无改动）",
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
    )


async def run_sediment(
    config: Config,
    workspace_path: Path,
    prompt: str,
    *,
    session_id: str,
) -> SedimentResult:
    """跑一次只出文本、不用工具的调用。"""
    cmd = [
        config.claude.binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        session_id,
        # 只出文本，不给它任何动手机会
        "--max-turns",
        "1",
        "--disallowed-tools",
        "AskUserQuestion",
    ]
    if config.claude.settings_file is not None:
        cmd += ["--settings", str(config.claude.settings_file)]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workspace_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        return SedimentResult(text=None, error="沉淀调用超时")

    result = SedimentResult(text=None)
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            usage = event.get("usage") or {}
            result.input_tokens = usage.get("input_tokens") or 0
            result.output_tokens = usage.get("output_tokens") or 0
            result.cache_read_tokens = usage.get("cache_read_input_tokens") or 0
            result.cache_creation_tokens = usage.get("cache_creation_input_tokens") or 0
            result.duration_ms = event.get("duration_ms")
            if event.get("is_error"):
                result.error = str(event.get("result") or event.get("subtype"))
            else:
                result.text = (event.get("result") or "").strip() or None
        elif event.get("type") == "system" and event.get("subtype") == "init":
            result.model = event.get("model")

    if result.text is None and result.error is None:
        detail = err.decode("utf-8", "replace").strip()[:300]
        result.error = f"未取得结果（退出码 {proc.returncode}）{': ' + detail if detail else ''}"
    return result


async def sediment_task(
    config: Config,
    contexts: ContextStore,
    project_name: str,
    task: TaskOut,
    workspace_path: Path,
    *,
    diff: str,
    stat: str,
    session_id: str,
) -> SedimentResult:
    """生成并追加一条变更记录。**幂等**：同一任务只追加一次。"""
    if not config.context.sediment:
        return SedimentResult(text=None, skipped=True)

    if contexts.journal_has_task(project_name, task.id):
        log.info("任务 %s 已有变更记录，跳过沉淀", task.id)
        return SedimentResult(text=None, skipped=True)

    prompt = build_prompt(
        project_name, task, diff, stat, max_diff_bytes=config.context.sediment_max_diff_bytes
    )
    result = await run_sediment(config, workspace_path, prompt, session_id=session_id)

    if result.error or not result.text:
        log.warning("任务 %s 沉淀失败: %s", task.id, result.error)
        return result

    if result.text.strip().upper().startswith("SKIP"):
        log.info("任务 %s 的改动无需记录", task.id)
        result.skipped = True
        return result

    entry = result.text.strip() + f"\n\n<!-- codingstorm-task: {task.id} -->\n"
    contexts.append_journal(project_name, entry)
    log.info("任务 %s 的变更记录已写入 journal", task.id)
    return result
