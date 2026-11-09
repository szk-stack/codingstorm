"""0 = 不限制轮数 / 墙钟的语义。"""

from codingstorm.agents.base import RunOutcome
from codingstorm.agents.claude import ClaudeRuntime
from codingstorm.agents.codebuddy import CodeBuddyRuntime
from codingstorm.agents.pi import PiRun
from codingstorm.config import Config, TaskConfig


def _unlimited() -> Config:
    return Config(task=TaskConfig(max_turns=0, wall_clock_timeout_s=0))


def test_config_allows_zero_limits():
    cfg = _unlimited()
    assert cfg.task.max_turns == 0
    assert cfg.task.wall_clock_timeout_s == 0


def test_claude_omits_max_turns_when_zero():
    cmd = ClaudeRuntime(_unlimited()).build_command(
        "x", session_id="s", context_dirs=[], resume=False
    )
    assert "--max-turns" not in cmd


def test_codebuddy_omits_max_turns_when_zero():
    cmd = CodeBuddyRuntime(_unlimited()).build_command(
        "x", session_id="s", context_dirs=[], resume=False
    )
    assert "--max-turns" not in cmd


def test_pi_does_not_fatal_when_unlimited():
    run = PiRun(_unlimited(), RunOutcome())
    assert run.handle({"type": "turn_start"}).fatal is None
    run.handle({"type": "turn_end", "message": {}, "toolResults": []})
    assert run.handle({"type": "turn_start"}).fatal is None
