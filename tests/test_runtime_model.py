"""项目级模型覆盖（CodeBuddy）：迁移 → 存储/接口 → 命令行 → 候选目录。

不跑真实任务：真实 CLI 现在还可能被账号限流，命令层只验证参数是怎么拼的。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codingstorm.agents import make_runtime
from codingstorm.app import create_app
from codingstorm.config import CodeBuddyConfig, Config, RuntimeConfig
from codingstorm.db import _migrate
from codingstorm.model_catalog import FALLBACK_MODELS, _from_cli, available_models
from codingstorm.runner import Runner


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def _mk_project(client: TestClient, name: str = "demo", **extra) -> dict:
    body = {"name": name, **extra}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _model_of(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--model") + 1] if "--model" in cmd else None


# ---------------- 迁移 ----------------

def test_migration_adds_runtime_model_column(tmp_path: Path):
    """老库没有这一列时自动 ALTER，且不丢已有项目。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row   # Database 就是这么配的，_migrate 按列名取值
    conn.execute(
        "CREATE TABLE projects ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, repo_path TEXT NOT NULL,"
        " target_branch TEXT NOT NULL DEFAULT 'main', runtime TEXT,"
        " enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,"
        " window_override TEXT)"
    )
    conn.execute(
        "INSERT INTO projects (id, name, repo_path, target_branch, created_at)"
        " VALUES ('p1','demo','/tmp/demo','main','2026-01-01')"
    )
    conn.commit()

    assert "runtime_model" not in {
        row[1] for row in conn.execute("PRAGMA table_info(projects)")
    }
    _migrate(conn)
    conn.commit()
    assert "runtime_model" in {
        row[1] for row in conn.execute("PRAGMA table_info(projects)")
    }
    assert conn.execute("SELECT name FROM projects WHERE id='p1'").fetchone()[0] == "demo"
    # 重复迁移是幂等的（_migrate 每次启动都会跑）
    _migrate(conn)
    conn.close()


# ---------------- 接口 / 存储 ----------------

def test_model_roundtrip_via_api(client: TestClient):
    p = _mk_project(client, "plain")
    assert p["runtime_model"] is None

    q = _mk_project(
        client, "with-model", runtime="codebuddy", runtime_model="deepseek-v4-pro"
    )
    assert q["runtime"] == "codebuddy"
    assert q["runtime_model"] == "deepseek-v4-pro"

    # 改模型时不能把执行器一起清掉 —— 这正是单独开一个 PATCH 端点的原因
    r = client.patch(f"/api/projects/{q['id']}/model", json={"model": "glm-5.3"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runtime_model"] == "glm-5.3"
    assert body["runtime"] == "codebuddy"

    # 「跟随全局」两种写法：null 与空串
    assert client.patch(
        f"/api/projects/{q['id']}/model", json={"model": None}
    ).json()["runtime_model"] is None
    assert client.patch(
        f"/api/projects/{q['id']}/model", json={"model": ""}
    ).json()["runtime_model"] is None

    assert client.patch("/api/projects/nope/model", json={"model": "x"}).status_code == 404
    # 原有执行器端点不受影响
    assert client.patch(
        f"/api/projects/{q['id']}", json={"runtime": "pi"}
    ).json()["runtime"] == "pi"


def test_runtime_models_endpoint(client: TestClient, monkeypatch):
    # 别在单测里真去跑 codebuddy --help
    monkeypatch.setattr(
        "codingstorm.api.available_models", lambda cfg: (["m1", "m2"], "cli")
    )
    r = client.get("/api/runtime/models")
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == ["m1", "m2"]
    assert body["source"] == "cli"
    assert body["default_runtime"] == "claude"
    assert "global_model" in body


def test_runtime_models_reports_configured_global(client: TestClient, monkeypatch):
    cfg = Config(codebuddy=CodeBuddyConfig(model="deepseek-v4.1-flash"))
    monkeypatch.setattr("codingstorm.api.available_models", lambda _cfg: (["m"], "config"))
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        body = c.get("/api/runtime/models").json()
    assert body["global_model"] == "deepseek-v4.1-flash"


# ---------------- 命令层 ----------------

def test_codebuddy_command_uses_project_model(tmp_path: Path):
    cfg = Config(
        root=tmp_path / "root",
        runtime=RuntimeConfig(default="codebuddy"),
        codebuddy=CodeBuddyConfig(model="deepseek-v4.1-flash"),
    )
    rt = make_runtime(cfg, "codebuddy")
    kw = dict(session_id="s-1", context_dirs=[], resume=False)

    assert _model_of(rt.build_command("x", model="glm-5.3", **kw)) == "glm-5.3"
    assert _model_of(rt.build_command("x", model=None, **kw)) == "deepseek-v4.1-flash"

    cfg.codebuddy.model = None
    assert _model_of(rt.build_command("x", **kw)) is None
    assert _model_of(rt.build_command("x", model="glm-5.3", **kw)) == "glm-5.3"


def test_runner_forwards_model(tmp_path: Path):
    cfg = Config(
        root=tmp_path / "root",
        runtime=RuntimeConfig(default="codebuddy"),
        codebuddy=CodeBuddyConfig(model="deepseek-v4.1-flash"),
    )
    runner = Runner(cfg, store=None)  # type: ignore[arg-type]  只测命令拼装
    cmd = runner.build_command(
        "x", session_id="s", context_dir=None, model="glm-5.3"
    )
    assert _model_of(cmd) == "glm-5.3"


def test_other_runtimes_ignore_model(tmp_path: Path):
    """别把 CodeBuddy 的模型 id 塞给 Claude / Pi。"""
    cfg = Config(root=tmp_path / "root")
    for name in ("claude", "pi"):
        rt = make_runtime(cfg, name)
        kw = dict(session_id="s", context_dirs=[], resume=False)
        base = rt.build_command("x", **kw)
        overridden = rt.build_command("x", model="glm-5.3", **kw)
        assert base == overridden


# ---------------- 候选目录 ----------------

def test_catalog_sources(monkeypatch, tmp_path: Path):
    configured = Config(
        root=tmp_path, codebuddy=CodeBuddyConfig(models=["only-one", "only-one"])
    )
    assert available_models(configured) == (["only-one"], "config")

    plain = Config(root=tmp_path)
    monkeypatch.setattr("codingstorm.model_catalog._from_cli", lambda b: ("a", "b"))
    assert available_models(plain) == (["a", "b"], "cli")

    monkeypatch.setattr("codingstorm.model_catalog._from_cli", lambda b: ())
    assert available_models(plain) == (list(FALLBACK_MODELS), "builtin")


def test_cli_help_parsing(monkeypatch):
    class FakeProc:
        stdout = (
            b"  --model <model>     Model for the current session. "
            b"Currently supported: (fast-model, deepseek-v4.1-flash, glm-5.3)\n"
            b"  --port <number>    Port for HTTP server\n"
        )
        stderr = b""

    monkeypatch.setattr("codingstorm.model_catalog.resolve_executable", lambda b: b)
    monkeypatch.setattr(
        "codingstorm.model_catalog.subprocess.run", lambda *a, **k: FakeProc()
    )
    _from_cli.cache_clear()
    try:
        assert _from_cli("fake") == (
            "fast-model", "deepseek-v4.1-flash", "glm-5.3"
        )
    finally:
        _from_cli.cache_clear()


def test_cli_help_failure_returns_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("CLI 没装")

    monkeypatch.setattr("codingstorm.model_catalog.subprocess.run", boom)
    _from_cli.cache_clear()
    try:
        assert _from_cli("missing") == ()
    finally:
        _from_cli.cache_clear()
