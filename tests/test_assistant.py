"""对话式助手（sidecar）的探测、拉起与两个接口。

这里不启动真的助手进程：`probe()` 和 `subprocess.Popen` 都被替换掉，
只验证「决定的逻辑」——什么时候该报没在跑、环境变量怎么拼、失败信息长什么样。
真进程的端到端验证靠手动跑一次 start-assistant.bat（README 里有步骤）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codingstorm.app import create_app
from codingstorm.assistant import AssistantSupervisor
from codingstorm.config import Config


@pytest.fixture
def make_app(tmp_path: Path):
    def _make(**assistant_kwargs) -> tuple[TestClient, object]:
        cfg = Config(root=tmp_path / "root")
        cfg.ensure_dirs()
        for key, value in assistant_kwargs.items():
            setattr(cfg.assistant, key, value)
        app = create_app(cfg, start_scheduler=False)
        return TestClient(app, raise_server_exceptions=True), app

    return _make


def _fake_probe(monkeypatch, running: bool) -> None:
    """把端口探测换成常量，避免测试真的去连 127.0.0.1:8001。"""
    monkeypatch.setattr(AssistantSupervisor, "probe", lambda self: running)


# ---------------- 状态 ----------------

def test_info_reports_not_running(make_app, monkeypatch):
    _fake_probe(monkeypatch, False)
    client, _ = make_app(url="http://127.0.0.1:8123")
    with client:
        payload = client.get("/api/assistant/info").json()
    assert payload["enabled"] is True
    assert payload["running"] is False
    assert payload["url"] == "http://127.0.0.1:8123"
    # 没配 dir 时要说清楚为什么拉不起来，而不是丢一个空 message
    assert "dir" in payload["message"]
    assert payload["managed"] is False


def test_info_reports_running(make_app, monkeypatch):
    _fake_probe(monkeypatch, True)
    client, _ = make_app()
    with client:
        payload = client.get("/api/assistant/info").json()
    assert payload["running"] is True
    assert payload["message"] == ""


def test_info_disabled(make_app, monkeypatch):
    _fake_probe(monkeypatch, False)
    client, _ = make_app(enabled=False)
    with client:
        payload = client.get("/api/assistant/info").json()
    assert payload["enabled"] is False
    assert payload["running"] is False
    assert "关闭" in payload["message"]


# ---------------- 启动 ----------------

def test_start_refuses_when_disabled(make_app, monkeypatch):
    _fake_probe(monkeypatch, False)
    client, _ = make_app(enabled=False)
    with client:
        payload = client.post("/api/assistant/start").json()
    assert payload["ok"] is False
    assert "关闭" in payload["message"]


def test_start_refuses_without_dir(make_app, monkeypatch, tmp_path):
    _fake_probe(monkeypatch, False)
    client, app = make_app(dir=None)
    with client:
        ok, message = app.state.assistant.start()
    assert ok is False
    assert "dir" in message


def test_start_refuses_when_server_py_missing(make_app, monkeypatch, tmp_path):
    _fake_probe(monkeypatch, False)
    empty = tmp_path / "assistant"
    empty.mkdir()
    client, app = make_app(dir=empty)
    with client:
        ok, message = app.state.assistant.start()
    assert ok is False
    assert "server.py" in message


def test_start_is_noop_when_already_running(make_app, monkeypatch):
    _fake_probe(monkeypatch, True)

    def boom(*args, **kwargs):  # 已经在跑就不该再拉一个
        raise AssertionError("不应该启动第二个助手进程")

    monkeypatch.setattr("codingstorm.assistant.subprocess.Popen", boom)
    client, app = make_app()
    with client:
        ok, message = app.state.assistant.start()
    assert ok is True
    assert "已在运行" in message


def test_start_passes_env_and_waits_for_answer(make_app, monkeypatch, tmp_path):
    """拉起的瞬间探测还是 False，进程活下来后探测变 True —— 这中间要等。"""
    target = tmp_path / "assistant"
    target.mkdir()
    (target / "server.py").write_text("# 假助手\n", encoding="utf-8")

    answers = iter([False, False, True])
    monkeypatch.setattr(
        AssistantSupervisor, "probe", lambda self: next(answers, True)
    )
    monkeypatch.setattr("codingstorm.assistant.time.sleep", lambda _s: None)

    captured: dict = {}

    class FakeProc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr("codingstorm.assistant.subprocess.Popen", fake_popen)

    client, app = make_app(
        dir=target,
        url="http://127.0.0.1:8123",
        model="deepseek-flash",
        api_key="",
    )
    # 没填 assistant.api_key 时复用 [claude.env] 的 token
    app.state.config.claude.env = {"ANTHROPIC_AUTH_TOKEN": "sk-from-claude-env"}
    with client:
        ok, message = app.state.assistant.start()

    assert ok is True, message
    assert captured["cmd"][1:] == ["server.py"]
    assert captured["cwd"] == str(target)
    env = captured["env"]
    assert env["MYWHEEL_PORT"] == "8123"
    assert env["MYWHEEL_API_KEY"] == "sk-from-claude-env"
    assert env["MYWHEEL_MODEL"] == "deepseek-flash"
    # 数据目录故意不覆盖：手动跑 bat 和自动拉起要共用同一份对话历史
    assert "MYWHEEL_DATA_DIR" not in env or env["MYWHEEL_DATA_DIR"] == ""


def test_start_prefers_explicit_api_key(make_app, monkeypatch, tmp_path):
    target = tmp_path / "assistant"
    target.mkdir()
    (target / "server.py").write_text("# 假助手\n", encoding="utf-8")
    # 第一次探测（拉起前）必须是「没在跑」，否则 start() 直接短路返回
    answers = iter([False, True])
    monkeypatch.setattr(AssistantSupervisor, "probe", lambda self: next(answers, True))
    monkeypatch.setattr("codingstorm.assistant.time.sleep", lambda _s: None)

    captured: dict = {}

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(
        "codingstorm.assistant.subprocess.Popen",
        lambda cmd, **kw: (captured.update(kw) or FakeProc()),
    )

    client, app = make_app(dir=target, api_key="sk-explicit")
    app.state.config.claude.env = {"ANTHROPIC_AUTH_TOKEN": "sk-from-claude-env"}
    with client:
        app.state.assistant.start()

    assert captured["env"]["MYWHEEL_API_KEY"] == "sk-explicit"


def test_start_reports_login_when_process_dies(make_app, monkeypatch, tmp_path):
    """进程秒退（比如端口被占）时不能一直等到超时：poll() 一有结果就收工。"""
    target = tmp_path / "assistant"
    target.mkdir()
    (target / "server.py").write_text("# 假助手\n", encoding="utf-8")

    monkeypatch.setattr(AssistantSupervisor, "probe", lambda self: False)
    monkeypatch.setattr("codingstorm.assistant.time.sleep", lambda _s: None)

    class DeadProc:
        pid = 99

        def poll(self):
            return 1  # 已经退出

    monkeypatch.setattr(
        "codingstorm.assistant.subprocess.Popen", lambda cmd, **kw: DeadProc()
    )

    client, app = make_app(dir=target)
    with client:
        ok, message = app.state.assistant.start()

    assert ok is False
    assert "日志" in message
    assert app.state.assistant.snapshot()["running"] is False
