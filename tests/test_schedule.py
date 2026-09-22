"""定时任务与执行窗口。

时间用真实时钟构造（相对当前时刻算窗口和 next_run_at），不冻结时间 ——
要测的本来就是「现在允不允许跑」，冻结了反而测不到真实路径。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from codingstorm.config import Config
from codingstorm.db import Database
from codingstorm.models import (
    ProjectCreate,
    ScheduleCreate,
    ScheduleRuleIn,
    TaskCreate,
    TaskStatus,
)
from codingstorm.runner import RunOutcome
from codingstorm.scheduler import Scheduler
from codingstorm.store import Store
from codingstorm.timing import iso_utc, parse_rule, parse_windows
from codingstorm.workspace import WorkspaceManager

TZ = ZoneInfo("Asia/Shanghai")


def run(coro):
    return asyncio.run(coro)


class FakeRunner:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, task_id, project_name, **kwargs) -> RunOutcome:
        self.calls.append({"task_id": task_id, "project": project_name, **kwargs})
        return RunOutcome(saw_result=True, is_error=False, session_id=kwargs.get("session_id"))

    async def kill(self, task_id: str) -> None:
        pass


def open_window() -> list[list[str]]:
    """一段此刻一定开着的窗口：一小时前开始，一小时后再关。"""
    now = datetime.now(TZ)
    return [[(now - timedelta(hours=1)).strftime("%H:%M"), (now + timedelta(hours=1)).strftime("%H:%M")]]


def closed_window() -> list[list[str]]:
    """一段此刻一定关着的窗口：一小时后才开。"""
    now = datetime.now(TZ)
    return [[(now + timedelta(hours=1)).strftime("%H:%M"), (now + timedelta(hours=2)).strftime("%H:%M")]]


@pytest.fixture
def build(tmp_path: Path, make_repo):
    """按需造环境 —— 窗口配置得在构造 Scheduler 之前给。"""

    dbs: list[Database] = []
    counter = {"n": 0}

    def _build(*, windows=None, enabled=None, grace=600):
        counter["n"] += 1
        tag = counter["n"]
        cfg = Config(root=tmp_path / f"root{tag}")
        cfg.ensure_dirs()
        # 不传 windows 就是「不启用窗口」，否则默认的 00:30–08:30 会随测试运行的钟点
        # 时灵时不灵 —— 那种测试比没有还糟
        cfg.scheduler.window.enabled = (windows is not None) if enabled is None else enabled
        cfg.scheduler.window.timezone = "Asia/Shanghai"
        cfg.scheduler.misfire_grace_s = grace
        if windows is not None:
            cfg.scheduler.window.windows = windows

        db = Database(tmp_path / f"test{tag}.db")
        db.start()
        dbs.append(db)
        store = Store(db)
        runner = FakeRunner()
        sched = Scheduler(cfg, store, runner, WorkspaceManager(cfg))
        return cfg, store, runner, sched

    yield _build
    for db in dbs:
        db.close()


async def _project(store: Store, repo: Path) -> str:
    p = await store.create_project(
        ProjectCreate(name="demo", repo_path=str(repo), target_branch="main")
    )
    return p.id


async def _drain(sched: Scheduler) -> None:
    if sched.running:
        await asyncio.gather(*list(sched.running.values()), return_exceptions=True)


async def _add_schedule(
    store: Store,
    project_id: str,
    rule_spec: dict,
    *,
    due_in_s: float = 0,
    title: str = "定时任务",
):
    """建一条定时任务，下一次触发时刻设成「due_in_s 秒之后」（负数表示已过期）。"""
    rule = parse_rule(rule_spec)
    next_at = datetime.now(UTC) + timedelta(seconds=due_in_s)
    return await store.create_schedule(
        project_id,
        ScheduleCreate(title=title, rule=ScheduleRuleIn(**rule_spec)),
        rule_json=rule.as_json(),
        next_run_at=iso_utc(next_at),
    )


# ---------- 执行窗口 ----------


def test_窗口关着时不领任务(build, make_repo):
    _, store, runner, sched = build(windows=closed_window())

    async def main():
        await store.create_task(await _project(store, make_repo()), TaskCreate(title="白天提交的"))
        await sched._tick()
        await _drain(sched)

        # 任务一条都没跑，还老实待在队列里
        assert runner.calls == []
        task = (await store.list_tasks())[0]
        assert task.status == TaskStatus.QUEUED

    run(main())


def test_窗口开着时照常领任务(build, make_repo):
    _, store, runner, sched = build(windows=open_window())

    async def main():
        await store.create_task(await _project(store, make_repo()), TaskCreate(title="夜里跑的"))
        await sched._tick()
        await _drain(sched)

        assert len(runner.calls) == 1
        assert (await store.list_tasks())[0].status == TaskStatus.AWAITING_REVIEW

    run(main())


def test_窗口配置关掉就完全不限制(build, make_repo):
    _, store, runner, sched = build(windows=closed_window(), enabled=False)

    async def main():
        await store.create_task(await _project(store, make_repo()), TaskCreate(title="不受限"))
        await sched._tick()
        await _drain(sched)
        assert len(runner.calls) == 1

    run(main())


def test_项目可以单独不受窗口限制(build, make_repo):
    _, store, runner, sched = build(windows=closed_window())

    async def main():
        override = await store.create_project(
            ProjectCreate(name="override", repo_path=str(make_repo("r2")), target_branch="main")
        )
        normal = await store.create_project(
            ProjectCreate(name="normal", repo_path=str(make_repo("r3")), target_branch="main")
        )
        await store.set_project_window(override.id, '{"mode":"always"}')
        # 不受限的项目先建，确保它排在被挡住的那个前面
        await store.create_task(override.id, TaskCreate(title="白天也能跑"))
        await store.create_task(normal.id, TaskCreate(title="只能夜里跑"))

        await sched._tick()
        await _drain(sched)

        done = {t.title: t.status for t in await store.list_tasks()}
        assert done["白天也能跑"] == TaskStatus.AWAITING_REVIEW
        assert done["只能夜里跑"] == TaskStatus.QUEUED

    run(main())


def test_项目可以配自己的时段(build, make_repo):
    """全局关着，但项目自己的时段开着 —— 走项目自己那套。"""

    _, store, runner, sched = build(windows=closed_window())

    async def main():
        p = await store.create_project(
            ProjectCreate(name="custom", repo_path=str(make_repo("r2")), target_branch="main")
        )
        await store.set_project_window(p.id, json.dumps({"mode": "custom", "windows": open_window()}))
        await store.create_task(p.id, TaskCreate(title="按自己的时段跑"))

        await sched._tick()
        await _drain(sched)
        assert len(runner.calls) == 1

    run(main())


def test_临时关掉窗口立即放行(build, make_repo):
    _, store, runner, sched = build(windows=closed_window())

    async def main():
        await store.create_task(await _project(store, make_repo()), TaskCreate(title="急事"))
        await sched._tick()
        await _drain(sched)
        assert runner.calls == []  # 先确认窗口确实挡住了

        sched.set_window_disabled(True)
        await sched._tick()
        await _drain(sched)
        assert len(runner.calls) == 1  # 一关就放行
        assert sched.window_state().disabled is True

        sched.set_window_disabled(False)
        assert sched.window_state().open is False

    run(main())


def test_窗口状态能算出下次开关时刻(build):
    _, _, _, sched = build(windows=closed_window())
    state = sched.window_state()
    assert state.enabled is True
    assert state.open is False
    assert state.next_open_at is not None
    assert state.next_close_at is None
    assert state.timezone == "Asia/Shanghai"

    sched.set_window_disabled(True)
    assert sched.window_state().open is True


def test_窗口只拦启动不打断正在跑的(build, make_repo):
    """跑起来的任务不受窗口关闭影响 —— 中途 kill 掉的钱不会退，重跑还要再花一遍。"""

    _, store, runner, sched = build(windows=open_window())

    async def main():
        await store.create_task(await _project(store, make_repo()), TaskCreate(title="跑到一半"))
        await sched._tick()
        # 任务已经在自己那条协程里跑着了，这会儿窗口关了
        sched._windows = parse_windows(closed_window())
        await _drain(sched)

        assert len(runner.calls) == 1
        assert (await store.list_tasks())[0].status == TaskStatus.AWAITING_REVIEW

    run(main())


# ---------- 定时任务触发 ----------


def test_到点触发会生成一条新任务(build, make_repo):
    _, store, runner, sched = build()

    async def main():
        pid = await _project(store, make_repo())
        schedule = await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1)

        await sched._tick()
        await _drain(sched)

        tasks = await store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].schedule_id == schedule.id  # 能追回到是哪条定时任务生成的
        assert len(runner.calls) == 1

        fresh = await store.get_schedule(schedule.id)
        assert fresh.run_count == 1
        assert fresh.last_task_id == tasks[0].id
        assert fresh.last_task_status == TaskStatus.AWAITING_REVIEW
        assert fresh.next_run_at > fresh.last_run_at  # 已经推进到下一次

    run(main())


def test_同一时刻不会被触发两次(build, make_repo):
    _, store, _, sched = build()

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1)

        for _ in range(3):  # 连着跑几轮，第二次开始已经没有到点的了
            await sched._tick()

        assert len(await store.list_tasks()) == 1

    run(main())


def test_还没到点就不触发(build, make_repo):
    _, store, _, sched = build()

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=3600)
        await sched._tick()
        assert await store.list_tasks() == []

    run(main())


def test_宽限期内迟到照常触发(build, make_repo):
    """轮询是一秒一次，只有平台重启才会迟到 —— 迟到几分钟不该算错过。"""

    _, store, _, sched = build(grace=600)

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-120)
        await sched._tick()
        assert len(await store.list_tasks()) == 1

    run(main())


def test_一次性任务错过就停下并标记(build, make_repo):
    _, store, _, sched = build(grace=60)

    async def main():
        pid = await _project(store, make_repo())
        schedule = await _add_schedule(
            store, pid, {"type": "once", "at": "2030-01-01T09:00"}, due_in_s=-3600
        )

        await sched._tick()
        assert await store.list_tasks() == []  # 不补跑

        fresh = await store.get_schedule(schedule.id)
        assert fresh.missed_at is not None
        assert fresh.enabled is False
        assert fresh.next_run_at is None
        assert fresh.run_count == 0

    run(main())


def test_周期任务错过只跳过这一轮(build, make_repo):
    _, store, _, sched = build(grace=60)

    async def main():
        pid = await _project(store, make_repo())
        schedule = await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-3600)

        await sched._tick()
        assert await store.list_tasks() == []

        fresh = await store.get_schedule(schedule.id)
        assert fresh.enabled is True  # 周期任务不会因为错过一次就停掉
        assert fresh.missed_at is None
        assert fresh.next_run_at is not None
        assert fresh.run_count == 0

        # 而且下一次确实在未来，不是把错过的几次连着补上
        assert fresh.next_run_at > iso_utc(datetime.now(UTC))

    run(main())


def test_停机多天也只跳过一次_不补跑(build, make_repo):
    _, store, _, sched = build(grace=60)

    async def main():
        pid = await _project(store, make_repo())
        # 相当于平台停了三天：到点时刻远在过去
        schedule = await _add_schedule(
            store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-3 * 24 * 3600
        )
        await sched._tick()
        await sched._tick()

        assert await store.list_tasks() == []  # 一条都没补
        fresh = await store.get_schedule(schedule.id)
        assert fresh.run_count == 0
        # 下一次是「明天/今天的 09:00」，不是三天前的下一次
        assert fresh.next_run_at > iso_utc(datetime.now(UTC))

    run(main())


def test_规则坏了只停用这一条_不拖垮调度循环(build, make_repo):
    _, store, _, sched = build()

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1)
        # 手工把规则写坏，模拟库里的脏数据
        bad = (await store.list_schedules())[0]
        await store.update_schedule(bad.id, rule="{不是 JSON")

        await sched._tick()  # 不该抛出去
        fresh = await store.get_schedule(bad.id)
        assert fresh.enabled is False
        assert await store.list_tasks() == []

    run(main())


def test_定时任务生成的任务也要走窗口(build, make_repo):
    """定时任务决定什么时候入队，窗口决定什么时候能执行 —— 两件事。"""

    _, store, runner, sched = build(windows=closed_window())

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1)

        await sched._tick()
        await _drain(sched)

        # 入队了，但窗口关着，没跑
        tasks = await store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.QUEUED
        assert runner.calls == []

    run(main())


def test_定时任务按项目串行(build, make_repo):
    """两条定时任务同时到点、同一项目 —— 只跑一条，另一条排队。"""

    _, store, runner, sched = build()

    async def main():
        pid = await _project(store, make_repo())
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1, title="甲")
        await _add_schedule(store, pid, {"type": "daily", "time": "09:00"}, due_in_s=-1, title="乙")

        await sched._tick()
        assert len(await store.list_tasks()) == 2  # 都入队了
        await _drain(sched)

        assert len(runner.calls) == 1  # 但同一项目串行，只跑了一条

    run(main())
