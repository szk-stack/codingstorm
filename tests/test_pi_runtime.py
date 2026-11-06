"""Pi 执行器测试。不真的拉起 pi —— 只验证命令行构造与事件翻译。

本机没装 pi，所以这里全部按官方 wire reference（https://pi.dev/docs/latest/json）
构造事件。真机验收清单见 docs/pi-agent-integration.md。
"""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from codingstorm.agents import make_runtime
from codingstorm.config import Config, PiConfig, RuntimeConfig
from codingstorm.db import Database
from codingstorm.guard import PI_EXTENSION_SOURCE, GuardInstaller
from codingstorm.models import ProjectCreate, TaskCreate
from codingstorm.runner import RunOutcome, Runner
from codingstorm.store import Store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path: Path):
    cfg = Config(root=tmp_path / "root", runtime=RuntimeConfig(default="pi"))
    cfg.ensure_dirs()
    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    runner = Runner(cfg, store)
    try:
        yield cfg, store, runner
    finally:
        db.close()


async def _mk_task(store: Store) -> str:
    p = await store.create_project(ProjectCreate(name="demo"))
    t = await store.create_task(p.id, TaskCreate(title="A"))
    return t.id


def _line(event: dict) -> bytes:
    return json.dumps(event).encode()


async def _feed(runner: Runner, store: Store, events: list[dict]):
    task_id = await _mk_task(store)
    outcome = RunOutcome()
    for event in events:
        await runner._handle_line(_line(event), task_id, outcome)
    await store.db.flush_events()
    rows = await store.list_events(task_id)
    return task_id, outcome, rows


def _assistant(
    text: str = "",
    *,
    stop: str = "stop",
    model: str = "deepseek-v4-flash",
    usage: dict | None = None,
    tools: list[dict] | None = None,
) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in tools or []:
        content.append({
            "type": "toolCall",
            "id": tool["id"],
            "name": tool["name"],
            "arguments": tool.get("arguments", {}),
        })
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": usage or {
                "input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 4,
                "totalTokens": 19, "cost": {"total": 0},
            },
            "stopReason": stop,
        },
    }


# ---------- 命令行 ----------

def test_build_command_uses_json_mode(env):
    _, _, runner = env
    cmd = runner.build_command("do it", session_id="s-1", context_dir=None)
    assert cmd[0] == "pi"
    assert cmd[1:3] == ["--mode", "json"]
    assert "--session-id" in cmd and "s-1" in cmd
    # 无人值守时不弹信任框，也不加载仓库里的 .pi 扩展
    assert "--no-approve" in cmd
    # prompt 放在 `--` 之后，以 `-` 开头的内容不会被当成开关
    assert cmd[-2:] == ["--", "do it"]
    # Pi 按启动用户权限读文件，不需要 Claude 的 --add-dir
    assert "--add-dir" not in cmd


def test_build_command_passes_model_and_thinking(env):
    cfg, store, _ = env
    cfg.pi = PiConfig(model="deepseek/deepseek-chat", provider="deepseek", thinking="high")
    runner = Runner(cfg, store)
    cmd = runner.build_command("x", session_id="s", context_dir=None)
    assert cmd[cmd.index("--model") + 1] == "deepseek/deepseek-chat"
    assert cmd[cmd.index("--provider") + 1] == "deepseek"
    assert cmd[cmd.index("--thinking") + 1] == "high"


def test_build_command_includes_guard_extension_when_present(env):
    cfg, _, runner = env
    assert "-e" not in runner.build_command("x", session_id="s", context_dir=None)

    guard = cfg.root / "guard" / "pi-guard.ts"
    guard.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text("// stub\n", encoding="utf-8")
    cmd = runner.build_command("x", session_id="s", context_dir=None)
    assert "-e" in cmd and str(guard) in cmd


def test_project_trust_modes(env):
    cfg, store, _ = env
    cfg.pi = PiConfig(project_trust="approve")
    runner = Runner(cfg, store)
    assert "--approve" in runner.build_command("x", session_id="s", context_dir=None)

    cfg.pi = PiConfig(project_trust="inherit")
    runner = Runner(cfg, store)
    cmd = runner.build_command("x", session_id="s", context_dir=None)
    assert "--approve" not in cmd and "--no-approve" not in cmd


def test_provider_requires_model():
    with pytest.raises(ValidationError):
        PiConfig(provider="deepseek")


def test_config_load_pi_section(tmp_path: Path):
    path = tmp_path / "codingstorm.toml"
    path.write_text(
        '[runtime]\ndefault = "pi"\n\n[pi]\nmodel = "deepseek/deepseek-chat"\n'
        'thinking = ""\nsession_dir = "~/pi-sessions"\n',
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.runtime.default == "pi"
    assert cfg.pi.thinking is None
    assert cfg.pi.session_dir == Path.home() / "pi-sessions"


# ---------- 事件翻译 ----------

def test_session_header_sets_session_id_and_emits_init(env):
    _, store, runner = env

    async def main():
        task_id, outcome, rows = await _feed(runner, store, [
            {"type": "session", "version": 3, "id": "uuid-1", "cwd": "/w"},
        ])
        assert outcome.session_id == "uuid-1"
        assert [r["type"] for r in rows] == ["init"]
        assert json.loads(rows[0]["payload"])["cwd"] == "/w"

    run(main())


def test_assistant_message_maps_text_tools_and_usage(env):
    _, store, runner = env

    async def main():
        _, outcome, rows = await _feed(runner, store, [
            {"type": "session", "id": "s1"},
            _assistant("hello", stop="toolUse", tools=[
                {"id": "c1", "name": "bash", "arguments": {"command": "ls"}},
            ]),
        ])
        assert outcome.model == "deepseek-v4-flash"
        assert (outcome.input_tokens, outcome.output_tokens) == (10, 2)
        assert (outcome.cache_read_tokens, outcome.cache_creation_tokens) == (3, 4)

        assistant = [json.loads(r["payload"]) for r in rows if r["type"] == "assistant"]
        assert assistant[0]["text"] == "hello"
        # Pi 用 arguments，UI 只认 input
        assert assistant[0]["tool_uses"][0]["input"] == {"command": "ls"}

    run(main())


def test_tool_execution_end_maps_tool_result(env):
    _, store, runner = env

    async def main():
        _, _, rows = await _feed(runner, store, [
            {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
             "isError": False,
             "result": {"content": [{"type": "text", "text": "file.txt"}], "details": {}}},
        ])
        payload = json.loads(rows[0]["payload"])
        assert payload["tool_use_id"] == "c1"
        assert payload["content"] == "file.txt"
        assert payload["is_error"] is False

    run(main())


def test_settled_aggregates_usage_and_succeeds(env):
    _, store, runner = env

    async def main():
        _, outcome, rows = await _feed(runner, store, [
            {"type": "session", "id": "s1"},
            _assistant("first", usage={"input": 10, "output": 2, "cacheRead": 3, "cacheWrite": 4}),
            {"type": "turn_end", "message": {}, "toolResults": []},
            _assistant("done", usage={"input": 5, "output": 1, "cacheRead": 0, "cacheWrite": 0}),
            {"type": "turn_end", "message": {}, "toolResults": []},
            {"type": "agent_settled", "aborted": False},
        ])
        assert outcome.saw_result and outcome.ok
        assert outcome.result_text == "done"
        assert outcome.num_turns == 2
        assert (outcome.input_tokens, outcome.output_tokens) == (15, 3)

        result = json.loads([r for r in rows if r["type"] == "result"][0]["payload"])
        assert result["subtype"] == "success"
        assert result["usage"]["input_tokens"] == 15

    run(main())


def test_error_stop_reason_marks_failure(env):
    _, store, runner = env

    async def main():
        bad = _assistant("", stop="error")
        bad["message"]["errorMessage"] = "upstream exploded"
        _, outcome, _ = await _feed(runner, store, [
            bad,
            {"type": "agent_settled", "aborted": False},
        ])
        assert outcome.saw_result and not outcome.ok
        assert outcome.result_subtype == "error_during_execution"
        assert outcome.error_text == "upstream exploded"

    run(main())


def test_aborted_marks_failure(env):
    _, store, runner = env

    async def main():
        _, outcome, _ = await _feed(runner, store, [
            _assistant("partial"),
            {"type": "agent_settled", "aborted": True},
        ])
        assert outcome.saw_result and not outcome.ok
        assert outcome.result_subtype == "aborted"

    run(main())


def test_turn_limit_is_enforced(env):
    """Pi 没有 --max-turns，不自己数的话死循环能烧一整晚。"""
    cfg, store, runner = env
    cfg.task.max_turns = 1

    async def main():
        _, outcome, _ = await _feed(runner, store, [
            {"type": "turn_start"},
            {"type": "turn_end", "message": {}, "toolResults": []},
            # 第二轮一开始就该拦，别等它跑完
            {"type": "turn_start"},
        ])
        assert outcome.error_text is not None
        assert "max_turns" in outcome.error_text

    run(main())


def test_agent_end_without_settled_is_accepted(env):
    """流被截断时 agent_end(willRetry=false) 已经声明不会再有自动续跑。"""
    _, store, runner = env

    async def main():
        task_id, outcome, _ = await _feed(runner, store, [
            _assistant("done"),
            {"type": "agent_end", "messages": [], "willRetry": False},
        ])
        runner._run_for(task_id, outcome).finish(exit_code=0, stderr_text="")
        assert outcome.saw_result and outcome.ok
        assert outcome.result_text == "done"

    run(main())


def test_missing_settled_reports_stderr(env):
    _, store, runner = env

    async def main():
        task_id, outcome, _ = await _feed(runner, store, [_assistant("partial")])
        runner._run_for(task_id, outcome).finish(
            exit_code=1, stderr_text="invalid api key"
        )
        assert not outcome.saw_result
        assert not outcome.ok
        assert "agent_settled" in outcome.error_text
        assert "invalid api key" in outcome.error_text

    run(main())


def test_recover_from_log_tail(env, tmp_path: Path):
    _, _, runner = env
    log = tmp_path / "task.ndjson"
    log.write_bytes(
        b"\n".join([
            _line({"type": "session", "id": "s-recovered"}),
            _line(_assistant("recovered", usage={"input": 7, "output": 3})),
            _line({"type": "agent_settled", "aborted": False}),
        ])
    )
    outcome = RunOutcome()
    run(runner._fill_from_log_tail(log, outcome))
    assert outcome.saw_result and outcome.ok
    assert outcome.session_id == "s-recovered"
    assert outcome.result_text == "recovered"
    assert outcome.input_tokens == 7


def test_auth_retry_stops_early(env):
    _, store, runner = env

    async def main():
        _, outcome, _ = await _feed(runner, store, [
            {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
             "delayMs": 2000, "errorMessage": "401 Unauthorized"},
        ])
        assert outcome.error_text is not None
        assert "认证失败" in outcome.error_text

    run(main())


# ---------- 沉淀 ----------

def test_parse_pi_sediment_aggregates_usage():
    from codingstorm.sediment import _parse_pi

    stdout = "\n".join([
        json.dumps({"type": "session", "id": "s"}),
        json.dumps(_assistant("## 记录", usage={"input": 100, "output": 20,
                                                "cacheRead": 5, "cacheWrite": 1})),
        json.dumps({"type": "agent_settled", "aborted": False}),
    ])
    result = _parse_pi(stdout, "", 0, 1234)
    assert result.text == "## 记录"
    assert (result.input_tokens, result.output_tokens) == (100, 20)
    assert (result.cache_read_tokens, result.cache_creation_tokens) == (5, 1)
    assert result.duration_ms == 1234
    assert result.error is None


def test_parse_pi_sediment_error_stop():
    from codingstorm.sediment import _parse_pi

    bad = _assistant("", stop="error")
    bad["message"]["errorMessage"] = "boom"
    result = _parse_pi(json.dumps(bad) + "\n", "", 0, 1)
    assert result.text is None
    assert result.error == "boom"


# ---------- 配置 / 数据层 ----------

def test_runtime_factory_rejects_unknown_name():
    with pytest.raises(ValueError):
        make_runtime(Config(), "nope")


def test_project_runtime_roundtrip_and_attempt_runtime(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)

    async def main():
        project = await store.create_project(ProjectCreate(name="p", runtime="pi"))
        assert project.runtime == "pi"
        task = await store.create_task(project.id, TaskCreate(title="A"))
        await store.start_attempt(task.id, 1, runtime="pi")
        attempts = await store.list_attempts(task.id)
        assert attempts[0].runtime == "pi"

    try:
        run(main())
    finally:
        db.close()


def test_guard_installer_writes_pi_extension(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    target = GuardInstaller(cfg).install_pi_guard()
    assert target is not None and target.exists()
    text = target.read_text(encoding="utf-8")
    assert "tool_call" in text and "spawnSync" in text


def test_guard_disabled_writes_no_pi_extension(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    cfg.claude.guard_enabled = False
    assert GuardInstaller(cfg).install_pi_guard() is None


def test_shipped_pi_extension_exists():
    assert PI_EXTENSION_SOURCE.exists(), "发行包里必须带上 Pi 的 guard 扩展"
