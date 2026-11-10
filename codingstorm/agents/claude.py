"""Claude Code 执行器。

命令行与事件处理的依据全部来自 Phase 0 实测（见 docs/phase0-report.md）：

- `-p` + `--output-format stream-json` **必须**配 `--verbose`，否则直接报错退出。
- **`--disallowed-tools AskUserQuestion`**：不禁掉的话无人值守时它会挂在那里等回答。
- **`--session-id` 显式指定 UUID**，不用 `--continue`（后者按 cwd 找「最近的会话」，
  多任务共享目录时会串到别人的会话上）。
- `result` 事件是唯一权威判据，退出码粒度太粗。
- `thinking_tokens` 占事件量 99.6%，一条都不能落库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from codingstorm.agents.base import (
    MAX_PAYLOAD_BYTES,
    AgentRun,
    AgentRuntime,
    Handled,
    RunOutcome,
    describe_missing_result,
    resolve_executable,
    truncate_payload,
)
from codingstorm.config import Config

# 这些错误重试也没用，见到就立刻停
FATAL_HTTP_STATUS = frozenset({401, 403})


class ClaudeRuntime(AgentRuntime):
    name = "claude"

    def build_command(
        self,
        prompt: str,
        *,
        session_id: str,
        context_dirs: Sequence[Path],
        resume: bool,
        model: str | None = None,
    ) -> list[str]:
        # Claude Code 的模型由 [claude.env] / ~/.claude/settings.json 决定，
        # 项目级下拉只对 CodeBuddy 生效，这里刻意不用 model。
        cfg = self.config
        cmd = [
            resolve_executable(cfg.claude.binary),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            # 不传 --verbose 时 stream-json 会直接报错退出（Phase 0 实测）
            "--verbose",
        ]
        if resume:
            # 实测 --resume 追加到同一个会话文件、id 不变
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]
        if cfg.task.max_turns > 0:
            cmd += ["--max-turns", str(cfg.task.max_turns)]
        cmd += [
            "--permission-mode",
            cfg.claude.permission_mode,
            # 无人值守时必须禁掉：否则它会停下来等一个永远不会来的回答
            "--disallowed-tools",
            "AskUserQuestion",
        ]
        # Phase 8：严格模式还要授权运行目录（state.json 与阶段产物都在那儿）。
        for d in context_dirs:
            cmd += ["--add-dir", str(d)]
        if cfg.claude.settings_file is not None:
            cmd += ["--settings", str(cfg.claude.settings_file)]
        return cmd

    def new_run(self, outcome: RunOutcome) -> AgentRun:
        return ClaudeRun(self.config, outcome)

    def build_oneshot_command(
        self, prompt: str, *, session_id: str, cwd: Path
    ) -> list[str]:
        cfg = self.config.claude
        cmd = [
            resolve_executable(cfg.binary),
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
        if cfg.settings_file is not None:
            cmd += ["--settings", str(cfg.settings_file)]
        return cmd


class ClaudeRun(AgentRun):
    def handle(self, event: dict[str, Any]) -> Handled:
        etype = event.get("type")
        subtype = event.get("subtype")

        if etype == "system" and subtype == "init":
            self.outcome.session_id = event.get("session_id") or self.outcome.session_id
            self.outcome.model = event.get("model")
            return Handled(events=[("init", {
                "session_id": self.outcome.session_id,
                "model": self.outcome.model,
                "cwd": event.get("cwd"),
                "tools": event.get("tools"),
            })])

        if etype == "system" and subtype == "api_retry":
            # 不可恢复的认证类错误：退避重试 10 次要等约 3 分钟，直接停
            if event.get("error_status") in FATAL_HTTP_STATUS:
                return Handled(fatal=(
                    f"认证失败（HTTP {event.get('error_status')}: {event.get('error')}），"
                    "重试无意义，已终止"
                ))
            return Handled()

        if etype == "result":
            self._absorb_result(event, self.outcome)
            return Handled(events=[("result", {
                "subtype": self.outcome.result_subtype,
                "is_error": self.outcome.is_error,
                "num_turns": self.outcome.num_turns,
                "duration_ms": self.outcome.duration_ms,
                "result": self.outcome.result_text,
                "usage": {
                    "input_tokens": self.outcome.input_tokens,
                    "output_tokens": self.outcome.output_tokens,
                    "cache_creation_input_tokens": self.outcome.cache_creation_tokens,
                    "cache_read_input_tokens": self.outcome.cache_read_tokens,
                },
                "permission_denials": self.outcome.permission_denials,
            })])

        if etype == "assistant":
            message = event.get("message") or {}
            tool_uses = [
                {"id": b.get("id"), "name": b.get("name"), "input": truncate_payload(b.get("input"))}
                for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            text = "\n".join(
                b.get("text", "")
                for b in (message.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if not text and not tool_uses:
                # 纯 thinking 的块不带信息，落了只是噪音
                return Handled()
            return Handled(events=[("assistant", {
                "text": text[:MAX_PAYLOAD_BYTES] or None,
                "tool_uses": tool_uses or None,
            })])

        if etype == "user":
            events = []
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(("tool_result", {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": block.get("is_error", False),
                        "content": truncate_payload(block.get("content")),
                    }))
            return Handled(events=events)

        # thinking_tokens / stream_event 等一律不落库：实测前者占 99.6%，
        # 逐条转发会淹掉队列、把 result 这类关键事件挤丢。
        return Handled()

    def finish(self, *, exit_code: int | None, stderr_text: str) -> None:
        if self.outcome.saw_result:
            return
        self.outcome.is_error = True
        if not self.outcome.error_text:
            self.outcome.error_text = describe_missing_result(
                exit_code, stderr_text, event="result"
            )

    def recover(self, events: Sequence[dict[str, Any]]) -> None:
        if self.outcome.saw_result:
            return
        for event in events:
            if event.get("type") == "result":
                self._absorb_result(event, self.outcome)

    def progress(self, event: dict[str, Any]) -> int | None:
        if event.get("type") == "system" and event.get("subtype") == "thinking_tokens":
            return int(event.get("estimated_tokens") or 0)
        return None

    @staticmethod
    def _absorb_result(event: dict[str, Any], outcome: RunOutcome) -> None:
        """result 是唯一权威判据，退出码粒度太粗。"""
        usage = event.get("usage") or {}
        outcome.saw_result = True
        outcome.result_subtype = event.get("subtype")
        outcome.is_error = bool(event.get("is_error"))
        outcome.result_text = event.get("result")
        outcome.num_turns = event.get("num_turns")
        outcome.duration_ms = event.get("duration_ms")
        outcome.duration_api_ms = event.get("duration_api_ms")
        outcome.total_cost_usd = event.get("total_cost_usd")
        outcome.input_tokens = usage.get("input_tokens") or 0
        outcome.output_tokens = usage.get("output_tokens") or 0
        outcome.cache_creation_tokens = usage.get("cache_creation_input_tokens") or 0
        outcome.cache_read_tokens = usage.get("cache_read_input_tokens") or 0
        outcome.permission_denials = event.get("permission_denials") or []
        if event.get("session_id"):
            outcome.session_id = event["session_id"]
