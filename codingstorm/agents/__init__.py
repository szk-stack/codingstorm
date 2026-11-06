"""执行器注册表。"""

from __future__ import annotations

from codingstorm.agents.base import (
    MAX_PAYLOAD_BYTES,
    AgentRun,
    AgentRuntime,
    Handled,
    RunOutcome,
    describe_missing_result,
    truncate_payload,
)
from codingstorm.agents.claude import ClaudeRuntime
from codingstorm.agents.pi import PiRuntime
from codingstorm.config import Config
from codingstorm.config import RuntimeName

_RUNTIMES: dict[str, type[AgentRuntime]] = {
    ClaudeRuntime.name: ClaudeRuntime,
    PiRuntime.name: PiRuntime,
}


def make_runtime(config: Config, name: str | None = None) -> AgentRuntime:
    """按名字拿一个执行器。None = 全局默认。

    配置里写错的名字在 pydantic 那层就被挡住了；这里再兜一层，避免插件式扩展时
    崩在调度循环里。
    """
    key = name or config.runtime.default
    try:
        return _RUNTIMES[key](config)
    except KeyError as exc:
        raise ValueError(f"未知的执行器：{key}（可选：{', '.join(_RUNTIMES)}）") from exc


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "AgentRun",
    "AgentRuntime",
    "Handled",
    "RunOutcome",
    "RuntimeName",
    "describe_missing_result",
    "make_runtime",
    "truncate_payload",
]
