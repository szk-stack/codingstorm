"""调度器测试。用假 runner，不真跑 Claude Code。"""

import asyncio
from pathlib import Path

import pytest

from codingstorm.config import Config
from codingstorm.db import Database
from codingstorm.models import ProjectCreate, TaskCreate, TaskStatus
from codingstorm.runner import RunOutcome
from codingstorm.scheduler import Scheduler
from codingstorm.store import Store
from codingstorm.workspace import WorkspaceManager


def run(coro):
    return asyncio.run(coro)


class FakeRunner:
    """记录被调用情况，按预设返回。"""

    def __init__(self, outcome_fn=None):
        self.calls: list[dict] = []
        self.outcome_fn = outcome_fn or (lambda **_: RunOutcome(saw_result=True, is_error=False))
        self.killed: list[str] = []

    async def run(self, task_id, project_name, **kwargs) -> RunOutcome:
        self.calls.append({"task_id": task_id, "project": project_name, **kwargs})
        out = self.outcome_fn(task_id=task_id)
        if isinstance(out, Exception):
            raise out
        return out

    async def kill(self, task_id: str) -> None:
        self.killed.append(task_id)


@pytest.fixture
def env(tmp_path: Path, make_repo):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    repo = make_repo()

    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    runner = FakeRunner()
    sched = Scheduler(cfg, store, runner, WorkspaceManager(cfg))
    try:
        yield cfg, store, runner, sched, repo
    finally:
        db.close()


async def _mk(store: Store, repo: Path, name: str = "demo", **kw) -> str:
    p = await store.create_project(
        ProjectCreate(name=name, repo_path=str(repo), target_branch="main")
    )
    await store.create_task(p.id, TaskCreate(title="A", **kw))
    return p.id


async def _drain(sched: Scheduler) -> None:
    if sched.running:
        await asyncio.gather(*list(sched.running.values()), return_exceptions=True)


def test_success_moves_to_awaiting_review(env):
    _, store, runner, sched, repo = env

    async def main():
        await _mk(store, repo)
        await sched._tick()
        await _drain(sched)

        task = (await store.list_tasks())[0]
        assert task.status == TaskStatus.AWAITING_REVIEW
        assert len(runner.calls) == 1

    run(main())


def test_failure_moves_to_failed_with_reason(env):
    _, store, runner, sched, repo = env
    runner.outcome_fn = lambda **_: RunOutcome(
        saw_result=True, is_error=True, result_subtype="error_max_turns"
    )

    async def main():
        await _mk(store, repo)
        await sched._tick()
        await _drain(sched)

        task = (await store.list_tasks())[0]
        assert task.status == TaskStatus.FAILED
        assert "error_max_turns" in (task.error_text or "")

    run(main())


def test_exception_in_runner_marks_failed(env):
    _, store, runner, sched, repo = env
    runner.outcome_fn = lambda **_: RuntimeError("炸了")

    async def main():
        await _mk(store, repo)
        await sched._tick()
        await _drain(sched)

        task = (await store.list_tasks())[0]
        assert task.status == TaskStatus.FAILED
        assert "炸了" in (task.error_text or "")

    run(main())


def test_per_project_serial(env):
    """同项目即使有多条排队、且全局并发还有余量，也只能跑一条。"""
    cfg, store, runner, sched, repo = env
    cfg.scheduler.max_concurrent = 8

    async def main():
        project = await store.create_project(
            ProjectCreate(name="demo", repo_path=str(repo), target_branch="main")
        )
        for i in range(4):
            await store.create_task(project.id, TaskCreate(title=f"T{i}"))

        await sched._tick()
        assert len(sched.running) == 1, "同项目只能同时跑一条"

        await sched._tick()
        assert len(sched.running) == 1, "第二条不该被领走"

        await _drain(sched)

    run(main())


def test_cross_project_parallel_up_to_limit(env):
    cfg, store, runner, sched, repo = env
    cfg.scheduler.max_concurrent = 2

    async def main():
        for i in range(4):
            p = await store.create_project(
                ProjectCreate(name=f"p{i}", repo_path=str(repo), target_branch="main")
            )
            await store.create_task(p.id, TaskCreate(title="x"))

        await sched._tick()
        await sched._tick()
        assert len(sched.running) == 2

        await sched._tick()
        assert len(sched.running) == 2, "达到全局上限后不该再领"

    run(main())


def test_recover_marks_running_as_interrupted(env):
    _, store, _, sched, repo = env

    async def main():
        await _mk(store, repo)
        claimed = await store.claim_next()
        assert claimed is not None

        recovered = await sched.recover()
        assert recovered == [claimed["id"]]

        task = await store.get_task(claimed["id"])
        assert task.status == TaskStatus.INTERRUPTED
        assert "重启" in (task.error_text or "")

    run(main())


def test_recover_is_safe_when_pid_missing(env):
    """没有 pid 记录（比如崩在记录 pid 之前）也要能正常恢复。"""
    _, store, _, sched, repo = env

    async def main():
        await _mk(store, repo)
        await store.claim_next()
        recovered = await sched.recover()
        assert len(recovered) == 1

    run(main())


def test_attempt_records_tokens(env):
    _, store, runner, sched, repo = env
    runner.outcome_fn = lambda **_: RunOutcome(
        saw_result=True,
        is_error=False,
        input_tokens=26198,
        output_tokens=3423,
        cache_read_tokens=78080,
        num_turns=4,
        model="deepseek-v4-flash",
    )

    async def main():
        await _mk(store, repo)
        await sched._tick()
        await _drain(sched)

        task = (await store.list_tasks())[0]
        attempts = await store.list_attempts(task.id)
        assert len(attempts) == 1
        assert attempts[0].input_tokens == 26198
        assert attempts[0].cache_read_tokens == 78080
        assert attempts[0].model == "deepseek-v4-flash"

    run(main())


def test_retry_creates_second_attempt_row(env):
    """重试不能覆盖上一条 attempt —— 失败那次往往最贵。"""
    _, store, runner, sched, repo = env

    async def main():
        await _mk(store, repo)
        await sched._tick()
        await _drain(sched)

        task = (await store.list_tasks())[0]
        await store.requeue(task.id)
        await sched._tick()
        await _drain(sched)

        attempts = await store.list_attempts(task.id)
        assert [a.attempt_no for a in attempts] == [1, 2]

    run(main())


def test_stop_marks_running_interrupted(env):
    _, store, runner, sched, repo = env

    async def slow(task_id, project_name, **kw):
        await asyncio.sleep(30)
        return RunOutcome(saw_result=True)

    async def main():
        await _mk(store, repo)
        runner.run = slow  # type: ignore[method-assign]
        await sched._tick()
        await asyncio.sleep(0.05)

        await sched.stop(grace_s=0.5)

        task = (await store.list_tasks())[0]
        assert task.status == TaskStatus.INTERRUPTED
        assert runner.killed, "正在跑的任务应当被终止"

    run(main())
