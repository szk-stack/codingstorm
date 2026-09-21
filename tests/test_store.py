"""业务查询层测试。重点验证「按项目串行、跨项目并行」的领取逻辑。"""

import asyncio
from pathlib import Path

import pytest

from codingstorm.db import Database
from codingstorm.models import ProjectCreate, TaskCreate, TaskStatus
from codingstorm.store import Store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.start()
    try:
        yield Store(db)
    finally:
        db.close()


async def _mk_project(store: Store, name: str) -> str:
    p = await store.create_project(
        ProjectCreate(name=name, repo_path=f"/tmp/{name}", target_branch="main")
    )
    return p.id


def test_create_and_list(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="写个测试", kind="requirement"))
        assert task.status == TaskStatus.QUEUED
        assert task.kind == "requirement"

        tasks = await store.list_tasks(project_id=pid)
        assert len(tasks) == 1
        assert tasks[0].title == "写个测试"

    run(main())


def test_claim_respects_per_project_serial(store: Store):
    """同项目同时只能有一条 running —— 这是整个系统的硬约束。"""

    async def main():
        pid = await _mk_project(store, "demo")
        await store.create_task(pid, TaskCreate(title="A"))
        await store.create_task(pid, TaskCreate(title="B"))

        first = await store.claim_next()
        assert first is not None, "第一条应该能被领到"

        second = await store.claim_next()
        assert second is None, "同项目还有任务在跑时不应该再领到"

    run(main())


def test_claim_allows_cross_project_parallel(store: Store):
    """不同项目可以并行 —— 这正是它相对「全局串行」的价值。"""

    async def main():
        p1 = await _mk_project(store, "alpha")
        p2 = await _mk_project(store, "beta")
        await store.create_task(p1, TaskCreate(title="A"))
        await store.create_task(p2, TaskCreate(title="B"))

        a = await store.claim_next()
        b = await store.claim_next()
        assert a is not None and b is not None
        assert {a["project_id"], b["project_id"]} == {p1, p2}

        assert await store.claim_next() is None

    run(main())


def test_claim_ordering_by_priority_then_age(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        await store.create_task(pid, TaskCreate(title="普通"))
        await store.create_task(pid, TaskCreate(title="紧急", priority=10))
        await store.create_task(pid, TaskCreate(title="稍急", priority=5))

        claimed = await store.claim_next()
        assert claimed["title"] == "紧急"

        await store.set_status(claimed["id"], TaskStatus.MERGED)
        claimed = await store.claim_next()
        assert claimed["title"] == "稍急"

    run(main())


def test_concurrent_claim_is_exclusive(store: Store):
    """并发领取同一批任务，不应出现重复领取。"""

    async def main():
        pid = await _mk_project(store, "demo")
        for i in range(5):
            await store.create_task(pid, TaskCreate(title=f"T{i}"))

        results = await asyncio.gather(*(store.claim_next() for _ in range(20)))
        got = [r for r in results if r is not None]
        assert len(got) == 1, "同项目并发时只应有一条被领到"

    run(main())


def test_requeue_clears_run_state(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))
        claimed = await store.claim_next()
        assert claimed["id"] == task.id
        assert claimed["started_at"] is not None

        await store.requeue(task.id)
        again = await store.get_task(task.id)
        assert again.status == TaskStatus.QUEUED
        assert again.started_at is None

        reclaimed = await store.claim_next()
        assert reclaimed is not None and reclaimed["id"] == task.id

    run(main())


def test_disabled_project_not_claimed(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        await store.create_task(pid, TaskCreate(title="A"))
        await store.db.execute("UPDATE projects SET enabled = 0 WHERE id = ?", (pid,))
        assert await store.claim_next() is None

    run(main())


def test_attempts_are_per_attempt(store: Store):
    """重试不能覆盖上一条 attempt —— 失败那次往往最贵，token 要分开记。"""

    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))

        for i, tokens in enumerate([(100, 50), (200, 80)], start=1):
            await store.start_attempt(task.id, i)
            await store.finish_attempt(
                task.id, i, is_error=i == 1, input_tokens=tokens[0], output_tokens=tokens[1]
            )

        attempts = await store.list_attempts(task.id)
        assert [a.attempt_no for a in attempts] == [1, 2]
        assert attempts[0].input_tokens == 100
        assert attempts[1].input_tokens == 200
        assert attempts[0].is_error is True

    run(main())


def test_next_attempt_no_increments(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))
        assert await store.next_attempt_no(task.id) == 1
        await store.start_attempt(task.id, 1)
        assert await store.next_attempt_no(task.id) == 2

    run(main())


def test_event_seq_allocates_from_max(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))
        assert await store.next_event_seq(task.id) == 0
        await store.db.append_events([(task.id, 0, "2026-09-17T00:00:00Z", "init", None)])
        await store.db.flush_events()
        assert await store.next_event_seq(task.id) == 1

    run(main())


def test_reconcile_orphans_finds_running(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        await store.create_task(pid, TaskCreate(title="A"))
        await store.claim_next()

        orphans = await store.reconcile_orphans()
        assert len(orphans) == 1
        assert orphans[0]["status"] == "running"

    run(main())


def test_finish_attempt_rejects_unknown_field(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))
        await store.start_attempt(task.id, 1)
        with pytest.raises(ValueError, match="没有字段"):
            await store.finish_attempt(task.id, 1, not_a_column=1)

    run(main())


def test_messages_are_ordered_and_numbered(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))

        assert await store.add_message(task.id, "第一句") == 1
        assert await store.add_message(task.id, "第二句") == 2

        msgs = await store.list_messages(task.id)
        assert [m.seq for m in msgs] == [1, 2]
        assert [m.text for m in msgs] == ["第一句", "第二句"]

    run(main())


def test_messages_are_per_task(store: Store):
    async def main():
        pid = await _mk_project(store, "demo")
        a = await store.create_task(pid, TaskCreate(title="A"))
        b = await store.create_task(pid, TaskCreate(title="B"))
        await store.add_message(a.id, "只给 A")

        assert [m.text for m in await store.list_messages(b.id)] == []
        assert await store.add_message(b.id, "B 的第一句") == 1

    run(main())


def test_last_session_id_follows_latest_attempt(store: Store):
    """接着聊要接在最后跑出来的那个**任务**会话上，不是最早那个。"""
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))
        assert await store.last_session_id(task.id) is None

        await store.start_attempt(task.id, 1)
        await store.finish_attempt(task.id, 1, session_id="s1")
        assert await store.last_session_id(task.id) == "s1"

        await store.start_attempt(task.id, 2)
        await store.finish_attempt(task.id, 2, session_id="s2")
        assert await store.last_session_id(task.id) == "s2"

    run(main())


def test_last_session_id_skips_sediment(store: Store):
    """沉淀是另一个会话，接错的话多轮对话会「看起来能答但没接上历史」。

    实测踩过：沉淀的 prompt 里带任务标题和 diff，接错会话照样能答对，
    所以这个 bug 从表面看是发现不了的。
    """
    async def main():
        pid = await _mk_project(store, "demo")
        task = await store.create_task(pid, TaskCreate(title="A"))

        await store.start_attempt(task.id, 1)
        await store.finish_attempt(task.id, 1, session_id="task-session")

        # 沉淀排在任务之后，序号更大
        await store.start_attempt(task.id, 2, origin="sediment")
        await store.finish_attempt(task.id, 2, session_id="sediment-session")

        assert await store.last_session_id(task.id) == "task-session"

    run(main())
