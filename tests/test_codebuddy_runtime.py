"""CodeBuddy CLI 执行器测试。

本机没装 codebuddy，命令构造按官方 headless 文档
（https://www.codebuddy.cn/docs/cli/cli-reference）断言；事件流与 Claude Code
同构，解析器复用 ClaudeRun，用构造事件验证。
"""

import json
from pathlib import Path

import pytest

from codingstorm.agents import make_runtime
from codingstorm.agents.base import RunOutcome
from codingstorm.agents.codebuddy import CodeBuddyRun, CodeBuddyRuntime
from codingstorm.config import Config, CodeBuddyConfig, RuntimeConfig
from codingstorm.db import Database
from codingstorm.guard import GuardInstaller
from codingstorm.models import ProjectCreate
from codingstorm.runner import Runner
from codingstorm.store import Store


@pytest.fixture
def env(tmp_path: Path):
    cfg = Config(root=tmp_path / "root", runtime=RuntimeConfig(default="codebuddy"))
    cfg.ensure_dirs()
    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    runner = Runner(cfg, store)
    try:
        yield cfg, store, runner
    finally:
        db.close()


def test_factory_returns_codebuddy_runtime():
    runtime = make_runtime(Config(), "codebuddy")
    assert isinstance(runtime, CodeBuddyRuntime)
    assert runtime.name == "codebuddy"


def test_new_session_uses_session_id(env):
    _, _, runner = env
    cmd = runner.build_command("do it", session_id="s-1", context_dir=None)
    assert cmd[0] == "codebuddy"
    assert cmd[1:3] == ["-p", "do it"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    # 2.181.0 支持 --session-id，和 Claude 一样固定平台生成的 id
    assert cmd[cmd.index("--session-id") + 1] == "s-1"
    assert "--resume" not in cmd
    assert cmd[cmd.index("--max-turns") + 1] == str(runner.config.task.max_turns)
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == (
        "AskUserQuestion,AskUserForStructuredInput"
    )
    # 宿主终端注入的 CODEBUDDY_SESSION_ID 会让 headless 进程去抢宿主已占用的
    # 稳定端口；显式 --port 0 强制系统分配，避免 EADDRINUSE
    assert cmd[cmd.index("--port") + 1] == "0"


def test_resume_uses_resume_flag(env):
    _, _, runner = env
    cmd = runner.build_command("next", session_id="abc-123", context_dir=None, resume=True)
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd


def test_settings_and_add_dir_are_passed(env, tmp_path: Path):
    cfg, store, _ = env
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    cfg.codebuddy = CodeBuddyConfig(settings_file=settings, model="gpt-5")
    runner = Runner(cfg, store)
    cmd = runner.build_command(
        "x", session_id="s", context_dir=None, context_dirs=[tmp_path / "ctx"]
    )
    assert cmd[cmd.index("--settings") + 1] == str(settings)
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "ctx")
    assert cmd[cmd.index("--model") + 1] == "gpt-5"


def test_oneshot_command_is_single_turn(env):
    _, _, runner = env
    cmd = runner.runtime.build_oneshot_command("record", session_id="s", cwd=Path("."))
    assert cmd[0] == "codebuddy"
    assert cmd[1] == "-p"
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert "--resume" not in cmd
    assert cmd[cmd.index("--port") + 1] == "0"


def test_event_stream_reuses_claude_parser():
    outcome = RunOutcome()
    run = CodeBuddyRun(Config(), outcome)
    events = [
        {"type": "system", "subtype": "init", "session_id": "cb-1",
         "model": "gpt-5", "cwd": "/w", "tools": ["Bash"]},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 1, "duration_ms": 12, "result": "done",
         "usage": {"input_tokens": 10, "output_tokens": 2,
                   "cache_read_input_tokens": 3, "cache_creation_input_tokens": 0}},
    ]
    emitted = []
    for event in events:
        emitted.extend(run.handle(event).events)
    run.finish(exit_code=0, stderr_text="")
    assert outcome.ok and outcome.session_id == "cb-1"
    assert outcome.result_text == "done"
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 2)
    assert [e[0] for e in emitted] == ["init", "assistant", "result"]


def test_error_result_maps_errors_to_reason():
    """CodeBuddy 把错误放在 errors[]，要映射成可读的失败原因。"""
    outcome = RunOutcome()
    run = CodeBuddyRun(Config(), outcome)
    events = [
        {"type": "system", "subtype": "init", "session_id": "cb-1"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True,
         "num_turns": 2, "duration_ms": 10, "result": None,
         "errors": ["Authentication required. Please use /login command to sign in"],
         "usage": {"input_tokens": 0, "output_tokens": 0}},
    ]
    emitted = []
    for event in events:
        emitted.extend(run.handle(event).events)
    run.finish(exit_code=0, stderr_text="")
    assert not outcome.ok
    assert "Authentication required" in (outcome.error_text or "")
    assert "Authentication required" in (outcome.result_text or "")
    result_payload = [p for t, p in emitted if t == "result"][0]
    assert "Authentication required" in (result_payload.get("result") or "")


def test_codebuddy_guard_settings_has_hooks_but_no_claude_env(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude.env = {"ANTHROPIC_AUTH_TOKEN": "should-not-leak"}
    path = GuardInstaller(cfg).install_codebuddy_guard()
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"]
    assert "env" not in data, "CodeBuddy 不能继承 Claude 的 provider 配置"


def test_sediment_parser_reads_codebuddy_errors():
    from codingstorm.sediment import _parse_claude

    event = {
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "errors": ["Authentication required. Please use /login"],
    }
    result = _parse_claude(json.dumps(event), "", 0, 1)
    assert result.text is None
    assert "Authentication required" in (result.error or "")


def test_project_create_accepts_codebuddy():
    assert ProjectCreate(name="demo", runtime="codebuddy").runtime == "codebuddy"
