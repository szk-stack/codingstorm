"""CodeBuddy Code CLI 执行器（`codebuddy` / `cbc`）。

CodeBuddy 的 headless 模式是 **Claude Code 兼容**的：

- `-p` + `--output-format stream-json`，事件流是 init → assistant → result；
- `--max-turns`、`--permission-mode`、`--settings`、`--add-dir` 都有；
- `result` 同样带 `subtype` / `is_error` / `result` / `usage`。

所以这里只做三件和 Claude 不同的事，事件解析直接复用 `ClaudeRun`：

1. **新会话用 `--session-id <uuid>`**（2.181.0 支持，和 Claude 一致）；
2. 续跑用 `--resume <id>`；
3. 非交互写文件/跑命令要加 `--dangerously-skip-permissions`（`-y`），
   工具黑名单参数是 `--disallowedTools`（驼峰）。

另外固定传 `--port 0`：CLI 即使 headless 也会起一个本地 HTTP 服务，默认会
「复用稳定端口」。如果平台是从某个 IDE/宿主终端里启动的，宿主注入的
`CODEBUDDY_SESSION_ID` 会让任务进程挑中宿主已在监听的那个端口，直接报
`EADDRINUSE ... Failed to start server`。显式要一个系统分配端口就没这个耦合。

官方文档：https://www.codebuddy.cn/docs/cli/cli-reference
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from codingstorm.agents.base import (
    AgentRun,
    RunOutcome,
    resolve_executable,
)
from codingstorm.agents.claude import ClaudeRun, ClaudeRuntime


class CodeBuddyRuntime(ClaudeRuntime):
    name = "codebuddy"

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str,
        context_dirs: Sequence[Path],
        resume: bool,
    ) -> list[str]:
        cfg = self.config.codebuddy
        cmd = [
            resolve_executable(cfg.binary),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            # 不复用宿主会话的稳定端口；见文件头说明
            "--port",
            "0",
        ]
        if resume:
            cmd += ["--resume", session_id]
        else:
            # 2.181.0 支持 --session-id，和 Claude 一样把平台生成的 id 写死，
            # 重试/恢复时能找到同一个会话
            cmd += ["--session-id", session_id]

        if self.config.task.max_turns > 0:
            cmd += ["--max-turns", str(self.config.task.max_turns)]
        cmd += [
            "--permission-mode",
            cfg.permission_mode,
        ]
        if cfg.permission_mode == "bypassPermissions":
            # headless 下不加这个，写文件/跑命令会被拦下来等确认
            cmd += ["--dangerously-skip-permissions"]
        cmd += [
            # 无人值守不能停在提问上：Claude 的工具叫 AskUserQuestion，
            # CodeBuddy 2.181 的 init 里是 AskUserForStructuredInput，两个都禁
            "--disallowedTools",
            "AskUserQuestion,AskUserForStructuredInput",
        ]
        for d in context_dirs:
            cmd += ["--add-dir", str(d)]
        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.settings_file is not None:
            cmd += ["--settings", str(cfg.settings_file)]
        cmd += list(cfg.extra_args)
        return cmd

    def build_oneshot_command(
        self, prompt: str, *, session_id: str, cwd: Path
    ) -> list[str]:
        cfg = self.config.codebuddy
        cmd = [
            resolve_executable(cfg.binary),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--port",
            "0",
            "--max-turns",
            "1",
            # 沉淀只出文本：禁用所有工具 + 不落会话文件
            "--tools",
            "",
            "--no-session-persistence",
            "--permission-mode",
            cfg.permission_mode,
        ]
        if cfg.permission_mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]
        cmd += ["--disallowedTools", "AskUserQuestion,AskUserForStructuredInput"]
        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.settings_file is not None:
            cmd += ["--settings", str(cfg.settings_file)]
        cmd += list(cfg.extra_args)
        return cmd

    def new_run(self, outcome: RunOutcome) -> AgentRun:
        return CodeBuddyRun(self.config, outcome)


class CodeBuddyRun(ClaudeRun):
    """事件结构和 Claude Code 同构，直接复用解析器。

    唯一差异：失败时错误文本在 `result.errors[]` 里，不是 `result.result`。
    不映射的话，任务详情只会显示 `执行失败（subtype=error_during_execution）`，
    看不到真正原因（比如「Authentication required」）。
    """

    @staticmethod
    def _absorb_result(event, outcome) -> None:
        ClaudeRun._absorb_result(event, outcome)
        errors = event.get("errors") or []
        if outcome.is_error and errors:
            text = "; ".join(str(e) for e in errors)[:2000]
            if not outcome.result_text:
                outcome.result_text = text
            if not outcome.error_text:
                outcome.error_text = text
