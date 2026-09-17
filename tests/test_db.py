"""数据层测试。不依赖 pytest-asyncio —— 每个测试内用 asyncio.run 显式驱动。"""

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from codingstorm.db import Database


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "test.db")
    d.start()
    try:
        yield d
    finally:
        d.close()


def test_schema_and_roundtrip(db: Database):
    async def main():
        await db.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
            ("p1", "demo", "/tmp/demo", "2026-09-17T00:00:00Z"),
        )
        row = await db.query_one("SELECT name, target_branch FROM projects WHERE id = ?", ("p1",))
        assert row is not None
        assert row["name"] == "demo"
        assert row["target_branch"] == "main"

    run(main())


def test_foreign_key_enforced(db: Database):
    async def main():
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO tasks (id, project_id, title, created_at)"
                " VALUES (?, ?, ?, ?)",
                ("t1", "nonexistent", "x", "2026-09-17T00:00:00Z"),
            )

    run(main())


def test_event_batching_and_flush(db: Database):
    async def main():
        await db.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
            ("p1", "demo", "/tmp/demo", "2026-09-17T00:00:00Z"),
        )
        await db.execute(
            "INSERT INTO tasks (id, project_id, title, created_at) VALUES (?, ?, ?, ?)",
            ("t1", "p1", "x", "2026-09-17T00:00:00Z"),
        )
        rows = [
            ("t1", i, "2026-09-17T00:00:00Z", "assistant", f'{{"n":{i}}}') for i in range(250)
        ]
        await db.append_events(rows)
        await db.flush_events()

        row = await db.query_one("SELECT COUNT(*) AS c, MAX(seq) AS m FROM task_events WHERE task_id = ?", ("t1",))
        assert row["c"] == 250
        assert row["m"] == 249

    run(main())


def test_flush_survives_restart(tmp_path: Path):
    """事件落盘后重开连接应当能读到（验证 WAL 提交确实发生）。"""
    path = tmp_path / "test.db"

    async def write():
        d = Database(path)
        d.start()
        await d.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
            ("p1", "demo", "/tmp/demo", "2026-09-17T00:00:00Z"),
        )
        await d.execute(
            "INSERT INTO tasks (id, project_id, title, created_at) VALUES (?, ?, ?, ?)",
            ("t1", "p1", "x", "2026-09-17T00:00:00Z"),
        )
        await d.append_events([("t1", 0, "2026-09-17T00:00:00Z", "init", None)])
        await d.flush_events()
        d.close()

    async def read():
        d = Database(path)
        d.start()
        row = await d.query_one("SELECT COUNT(*) AS c FROM task_events WHERE task_id = ?", ("t1",))
        d.close()
        return row["c"]

    run(write())
    assert run(read()) == 1


def test_concurrent_writes(db: Database):
    """并发提交不应触发 SQLITE_BUSY（busy_timeout + 单写线程的意义）。"""

    async def main():
        await db.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
            ("p1", "demo", "/tmp/demo", "2026-09-17T00:00:00Z"),
        )

        async def one(i: int):
            await db.execute(
                "INSERT INTO tasks (id, project_id, title, created_at) VALUES (?, ?, ?, ?)",
                (f"t{i}", "p1", f"task {i}", "2026-09-17T00:00:00Z"),
            )
            await db.query("SELECT COUNT(*) FROM tasks")

        await asyncio.gather(*(one(i) for i in range(50)))
        row = await db.query_one("SELECT COUNT(*) AS c FROM tasks")
        assert row["c"] == 50

    run(main())


def test_atomic_claim(db: Database):
    """领取用单条 UPDATE + rowcount 判断，并发下同一条任务只能被抢到一次。"""

    async def main():
        await db.execute(
            "INSERT INTO projects (id, name, repo_path, created_at) VALUES (?, ?, ?, ?)",
            ("p1", "demo", "/tmp/demo", "2026-09-17T00:00:00Z"),
        )
        await db.execute(
            "INSERT INTO tasks (id, project_id, title, created_at) VALUES (?, ?, ?, ?)",
            ("t1", "p1", "x", "2026-09-17T00:00:00Z"),
        )

        async def claim():
            return await db.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ? AND status = 'queued'",
                ("t1",),
            )

        results = await asyncio.gather(*(claim() for _ in range(20)))
        assert sum(1 for r in results if r == 1) == 1, f"应恰好抢到一次，实际 {results}"

    run(main())


def test_event_table_is_without_rowid(db: Database):
    async def main():
        row = await db.query_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_events'"
        )
        assert "WITHOUT ROWID" in (row["sql"] or "")

    run(main())
