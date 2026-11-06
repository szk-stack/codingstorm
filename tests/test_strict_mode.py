"""Phase 8 8a 集成测试：API 令牌校验 + 调度器挂起/续跑。

不真跑 Claude Code —— ScriptedRunner 在每次执行时把 Agent 该写的 state.json
和阶段产物写到磁盘上，用来验证平台侧的判定逻辑。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codingstorm.agent_state import (
    DELIVERY_ARTIFACTS,
    DELIVERY_STAGE,
    GATE_ARTIFACTS,
    GATE_DEV_TOKEN,
    GATE_STAGE,
    collect_artifacts,
    new_state,
    read_state,
    record_gate,
    state_path,
    write_state,
)
from codingstorm.app import create_app
from codingstorm.config import AgentsConfig, Config, ContextConfig
from codingstorm.db import Database
from codingstorm.models import ProjectCreate, TaskCreate, TaskStatus
from codingstorm.runner import RunOutcome
from codingstorm.scheduler import Scheduler
from codingstorm.store import Store
from codingstorm.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _write_gate(run_path: Path, task_id: str) -> dict:
    """模拟执行 A：诊断 + 方案，停在人工门。"""
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "diagnosis.md").write_text("# 根因\n复现与证据\n", encoding="utf-8")
    (run_path / "idea.md").write_text("# 方案\n准备改什么\n", encoding="utf-8")
    st = new_state(task_id, "bugfix")
    st["stage"] = GATE_STAGE
    st["waiting_token"] = GATE_DEV_TOKEN
    st["artifacts"] = collect_artifacts(run_path, GATE_ARTIFACTS)
    write_state(state_path(run_path), st)
    return st


def _write_delivery(run_path: Path, task_id: str) -> None:
    """模拟执行 B：实现 + 净室评审 + 验证 + 提测包。"""
    st = read_state(state_path(run_path), task_id=task_id)
    st["stage"] = DELIVERY_STAGE
    st["waiting_token"] = None
    for name in DELIVERY_ARTIFACTS:
        (run_path / name).write_text("ok\n", encoding="utf-8")
    st["artifacts"].update(collect_artifacts(run_path, DELIVERY_ARTIFACTS))
    write_state(state_path(run_path), st)


def _sql(cfg: Config, sql: str, params: tuple = ()) -> None:
    con = sqlite3.connect(cfg.db_path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


@pytest.fixture
def api_rig(tmp_path: Path):
    cfg = Config(root=tmp_path / "root", agents=AgentsConfig(enabled=True))
    cfg.ensure_dirs()
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as c:
        p = c.post("/api/projects", json={"name": "demo"}).json()
        yield c, cfg, p


def _create_task(client: TestClient, project_id: str, **body) -> dict:
    r = client.post(
        f"/api/projects/{project_id}/tasks", json={"title": "修复一个 bug", **body}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _gate_ready(c: TestClient, cfg: Config, p: dict) -> tuple[dict, Path]:
    """造一条已经停在人工门的 strict 任务，返回 (任务, 运行目录)。"""
    t = _create_task(c, p["id"], mode="strict")
    run_path = cfg.runs_dir / "demo" / t["id"]
    _write_gate(run_path, t["id"])
    _sql(
        cfg,
        "UPDATE tasks SET status=?, mode=?, agent_id=?, stage=?, state_path=? WHERE id=?",
        (
            str(TaskStatus.AWAITING_DECISION), "strict", "bugfix", GATE_STAGE,
            str(state_path(run_path)), t["id"],
        ),
    )
    return t, run_path


def test_strict_is_rejected_when_agents_disabled(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")  # enabled 默认 False
    cfg.ensure_dirs()
    with TestClient(create_app(cfg, start_scheduler=False)) as c:
        p = c.post("/api/projects", json={"name": "demo"}).json()
        r = c.post(
            f"/api/projects/{p['id']}/tasks",
            json={"title": "x", "mode": "strict"},
        )
        assert r.status_code == 400
        assert "未启用" in r.json()["detail"]


def test_plain_is_the_default_and_kind_stays_a_label(api_rig):
    c, _, p = api_rig
    t = _create_task(c, p["id"], kind="bug")
    assert t["mode"] == "plain"
    assert t["agent_id"] is None


def test_strict_kinds_can_auto_enable(tmp_path: Path):
    cfg = Config(
        root=tmp_path / "root",
        agents=AgentsConfig(enabled=True, strict_kinds=["bug"]),
    )
    cfg.ensure_dirs()
    with TestClient(create_app(cfg, start_scheduler=False)) as c:
        p = c.post("/api/projects", json={"name": "demo"}).json()
        bug = _create_task(c, p["id"], kind="bug")
        other = _create_task(c, p["id"], kind="task")
        assert bug["mode"] == "strict"
        assert bug["agent_id"] == "bugfix"
        assert other["mode"] == "plain"


def test_stage_endpoint(api_rig):
    c, cfg, p = api_rig
    plain = _create_task(c, p["id"])
    assert c.get(f"/api/tasks/{plain['id']}/stage").status_code == 409

    t, run_path = _gate_ready(c, cfg, p)
    r = c.get(f"/api/tasks/{t['id']}/stage")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stage"] == GATE_STAGE
    assert d["waiting_token"] == GATE_DEV_TOKEN
    assert d["authorized"] is False
    assert [a["name"] for a in d["artifacts"]] == list(GATE_ARTIFACTS)
    assert d["state_path"] == str(state_path(run_path))


def test_gate_rejects_everything_but_the_exact_token(api_rig):
    c, cfg, p = api_rig
    t, _ = _gate_ready(c, cfg, p)
    for text in ["继续", "可以", "按这个做", GATE_DEV_TOKEN + " ", " " + GATE_DEV_TOKEN, "进入开发"]:
        r = c.post(f"/api/tasks/{t['id']}/messages", json={"text": text})
        assert r.status_code == 409, text
        assert "精确令牌" in r.json()["detail"]
    assert c.get(f"/api/tasks/{t['id']}").json()["status"] == "awaiting_decision"


def test_gate_accepts_exact_token_and_records_authorization(api_rig):
    c, cfg, p = api_rig
    t, run_path = _gate_ready(c, cfg, p)
    r = c.post(f"/api/tasks/{t['id']}/messages", json={"text": GATE_DEV_TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"

    st = read_state(state_path(run_path), task_id=t["id"])
    assert st["history"][-1]["token"] == GATE_DEV_TOKEN
    assert st["history"][-1]["approved_idea_sha"].startswith("sha256:")
    view = c.get(f"/api/tasks/{t['id']}/stage").json()
    assert view["authorized"] is True


def test_gate_detects_tampered_idea(api_rig):
    c, cfg, p = api_rig
    t, run_path = _gate_ready(c, cfg, p)
    (run_path / "idea.md").write_text("偷偷换了个方案\n", encoding="utf-8")
    r = c.post(f"/api/tasks/{t['id']}/messages", json={"text": GATE_DEV_TOKEN})
    assert r.status_code == 409
    assert "哈希" in r.json()["detail"]


def test_discard_allows_awaiting_decision(api_rig):
    c, cfg, p = api_rig
    t = _create_task(c, p["id"], mode="strict")
    _sql(cfg, "UPDATE tasks SET status=? WHERE id=?", (str(TaskStatus.AWAITING_DECISION), t["id"]))
    r = c.post(f"/api/tasks/{t['id']}/discard")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "discarded"


# ---------------------------------------------------------------------------
# 调度器层
# ---------------------------------------------------------------------------


class ScriptedRunner:
    """按脚本写阶段产物，模拟 Agent 的两次执行。"""

    def __init__(self, *, dirty_gate: bool = False, bad_state: bool = False):
        self.calls: list[dict] = []
        self.phase = 0
        self.dirty_gate = dirty_gate
        self.bad_state = bad_state

    async def run(self, task_id: str, project_name: str, **kw) -> RunOutcome:
        self.calls.append(kw)
        dirs = kw.get("context_dirs") or []
        if len(dirs) > 1:
            run_path = Path(dirs[1])
            if self.bad_state:
                state_path(run_path).write_text("{broken", encoding="utf-8")
            elif self.phase == 0:
                _write_gate(run_path, task_id)
                if self.dirty_gate:
                    (Path(kw["cwd"]) / "premature.txt").write_text("oops", encoding="utf-8")
                self.phase = 1
            else:
                _write_delivery(run_path, task_id)
                (Path(kw["cwd"]) / "fixed.txt").write_text("fixed", encoding="utf-8")
        return RunOutcome(saw_result=True, is_error=False, session_id=kw.get("session_id"))

    async def kill(self, task_id: str) -> None:
        pass


@pytest.fixture
def env(tmp_path: Path, make_repo):
    cfg = Config(
        root=tmp_path / "root",
        agents=AgentsConfig(enabled=True),
        context=ContextConfig(sediment=False),  # 避免测试里真去拉一次模型
    )
    cfg.ensure_dirs()
    repo = make_repo()
    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    runner = ScriptedRunner()
    sched = Scheduler(cfg, store, runner, WorkspaceManager(cfg))
    try:
        yield cfg, store, runner, sched, repo
    finally:
        db.close()


async def _mk(store: Store, repo: Path, **kw) -> None:
    p = await store.create_project(
        ProjectCreate(name="demo", repo_path=str(repo), target_branch="main")
    )
    await store.create_task(p.id, TaskCreate(title="fix bug", **kw))


async def _drain(sched: Scheduler) -> None:
    tasks = list(sched.running.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_strict_task_hangs_at_gate_and_releases_slot(env):
    _, store, runner, sched, repo = env

    async def main():
        await _mk(store, repo, mode="strict")
        await sched._tick()
        await _drain(sched)

        t = (await store.list_tasks())[0]
        assert t.status == TaskStatus.AWAITING_DECISION
        assert t.stage == GATE_STAGE
        assert t.state_path
        assert t.commit_sha is None          # 挂起时不提交、不沉淀
        assert not sched.running             # 并发位已释放
        assert len(runner.calls) == 1

        run_path = Path(t.state_path).parent
        assert (run_path / "diagnosis.md").is_file()
        # 阶段产物在 worktree 之外，git add -A 碰不到它们
        assert not (Path(t.worktree_path) / "state.json").exists()
        assert not (Path(t.worktree_path) / "diagnosis.md").exists()

    asyncio.run(main())


def test_token_resumes_same_session_and_delivers(env):
    _, store, runner, sched, repo = env

    async def main():
        await _mk(store, repo, mode="strict")
        await sched._tick()
        await _drain(sched)
        t = (await store.list_tasks())[0]

        # 等价于 API 放行：校验令牌、记录授权、追加消息、重新入队
        run_path = Path(t.state_path).parent
        st = read_state(state_path(run_path), task_id=t.id)
        record_gate(st, token=GATE_DEV_TOKEN, approved_idea_sha=st["artifacts"]["idea.md"])
        write_state(state_path(run_path), st)
        await store.add_message(t.id, GATE_DEV_TOKEN)
        await store.requeue(t.id)

        await sched._tick()
        await _drain(sched)
        t2 = (await store.list_tasks())[0]

        assert t2.status == TaskStatus.AWAITING_REVIEW
        assert len(runner.calls) == 2
        assert runner.calls[1]["resume"] is True
        assert "人工授权已由平台校验" in runner.calls[1]["prompt"]
        assert GATE_DEV_TOKEN in runner.calls[1]["prompt"]
        # 续跑接的是执行 A 的会话，不是新会话
        assert runner.calls[1]["session_id"] == runner.calls[0]["session_id"]
        assert not (Path(t2.worktree_path) / "state.json").exists()

    asyncio.run(main())


def test_plain_mode_is_untouched_even_when_agents_enabled(env):
    _, store, runner, sched, repo = env

    async def main():
        await _mk(store, repo)  # 默认 plain
        await sched._tick()
        await _drain(sched)
        t = (await store.list_tasks())[0]
        assert t.status == TaskStatus.AWAITING_REVIEW
        assert t.state_path is None
        assert runner.calls[0]["context_dirs"] is None

    asyncio.run(main())


def test_code_change_before_gate_fails_the_task(env):
    _, store, runner, sched, repo = env
    runner.dirty_gate = True

    async def main():
        await _mk(store, repo, mode="strict")
        await sched._tick()
        await _drain(sched)
        t = (await store.list_tasks())[0]
        assert t.status == TaskStatus.FAILED
        assert "人工门之前不得修改产品代码" in (t.error_text or "")

    asyncio.run(main())


def test_broken_state_file_fails_the_task(env):
    _, store, runner, sched, repo = env
    runner.bad_state = True

    async def main():
        await _mk(store, repo, mode="strict")
        await sched._tick()
        await _drain(sched)
        t = (await store.list_tasks())[0]
        assert t.status == TaskStatus.FAILED
        assert "状态错误" in (t.error_text or "")

    asyncio.run(main())
