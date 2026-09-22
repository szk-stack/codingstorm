"""SQLite 数据层（通用连接与批量写入，不含业务查询）。

设计要点（都是「不做就会出问题」的）：

- **单写连接 + 单写线程**。同步 sqlite3 直接在事件循环里跑会阻塞整个 loop（每次 commit 都含 fsync）。
- **状态变更立即提交，事件批量提交**。状态变更必须马上对读者可见；事件是高频低价值路径，
  攒够一批再 commit，反正还有原始日志兜底。
- **读走各自线程的独立连接**。WAL 下读写不互斥。
- **事务绝不跨 await**。
- 连接跑在 autocommit（`isolation_level=None`），多语句批处理显式 BEGIN/COMMIT。
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    repo_path     TEXT NOT NULL,
    target_branch TEXT NOT NULL DEFAULT 'main',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    -- 该项目对全局执行窗口的覆盖；NULL = 继承全局。
    -- 存 JSON：{"mode":"always"} 或 {"mode":"custom","windows":[["09:00","18:00"]]}
    window_override TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    body              TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'task',
    status            TEXT NOT NULL DEFAULT 'queued',
    priority          INTEGER NOT NULL DEFAULT 0,
    branch            TEXT,
    worktree_path     TEXT,
    base_commit       TEXT,
    commit_sha        TEXT,
    merge_commit_sha  TEXT,
    prompt_snapshot   TEXT,
    last_event_at     TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    error_text        TEXT,
    -- 由哪条定时任务生成；手工提交的任务为 NULL
    schedule_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue
    ON tasks (project_id, status, priority DESC, created_at);

-- 定时任务。**每次触发是生成一条新任务，不是复用同一条** ——
-- 一条任务对应一个分支、一个工作区、一份待审 diff，复用会把这套模型直接搞乱。
CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'task',
    priority     INTEGER NOT NULL DEFAULT 0,
    rule         TEXT NOT NULL,        -- JSON：{"type":"daily","time":"09:00"}
    next_run_at  TEXT,                 -- UTC；NULL = 不再触发（已停用或一次性已过）
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_task_id TEXT,                 -- 上一轮生成的任务，界面据此提示「上轮还没审」
    run_count    INTEGER NOT NULL DEFAULT 0,
    -- 一次性任务错过了时刻（平台没在跑）。留在列表里让人看得见，而不是静默消失
    missed_at    TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS attempts (
    task_id               TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no            INTEGER NOT NULL,
    session_id            TEXT,
    pid                   INTEGER,
    pid_starttime         TEXT,
    model                 TEXT,
    exit_code             INTEGER,
    result_subtype        TEXT,
    is_error              INTEGER,
    error_text            TEXT,
    duration_ms           INTEGER,
    num_turns             INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens     INTEGER,
    cost_usd              REAL,
    price_version         TEXT,
    raw_log_path          TEXT,
    origin                TEXT NOT NULL DEFAULT 'task',
    started_at            TEXT,
    finished_at           TEXT,
    PRIMARY KEY (task_id, attempt_no)
);

-- 只存语义事件（见 runner.PERSISTED_EVENT_TYPES）。
-- thinking_tokens 与 stream_event 永不落库 —— 实测它们占 99.6%。
CREATE TABLE IF NOT EXISTS task_events (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL,
    payload  TEXT,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;

-- 用户追加的消息（第 2 轮起）。第 1 轮存在 tasks 的 title/body 里。
-- 有这张表的行意味着「这条任务已经开过口」，调度器据此决定是接着上一轮
-- 会话跑，还是从最新的主干重开。
CREATE TABLE IF NOT EXISTS task_messages (
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
) WITHOUT ROWID;
"""

_SENTINEL = object()

# 后加的列。`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，
# 所以升级老库只能靠 ALTER。SQLite 的 ADD COLUMN 是纯元数据操作，不重写数据。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("tasks", "schedule_id", "TEXT"),
    ("projects", "window_override", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


@dataclass(slots=True)
class _Op:
    fn: Callable[[sqlite3.Connection], Any]
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop


class Database:
    def __init__(self, path: Path, *, batch_size: int = 100, batch_interval_s: float = 0.2):
        self.path = Path(path)
        self._batch_size = batch_size
        self._batch_interval_s = batch_interval_s

        self._q: queue.Queue = queue.Queue()
        self._pending_events: list[tuple] = []
        self._events_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._read_conn_obj: sqlite3.Connection | None = None
        self._read_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._writer_loop, name="cs-db-writer", daemon=True)
        self._thread.start()
        # 等建表完成，避免读发生在表存在之前
        if not self._ready.wait(timeout=10):
            raise RuntimeError("数据层初始化超时")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(_SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=10)
        with self._read_lock:
            if self._read_conn_obj is not None:
                self._read_conn_obj.close()
                self._read_conn_obj = None

    def __enter__(self) -> "Database":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 连接 ----------

    @staticmethod
    def _make_conn(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
        conn = sqlite3.connect(
            path, isolation_level=None, timeout=5.0, check_same_thread=check_same_thread
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _read_conn(self) -> sqlite3.Connection:
        """单条读连接，用时加锁。

        不能用 threading.local —— 读走 asyncio.to_thread，线程池会换线程，
        sqlite3 会拒绝跨线程使用连接。
        """
        if self._read_conn_obj is None:
            self._read_conn_obj = self._make_conn(self.path, check_same_thread=False)
        return self._read_conn_obj

    def _read(self, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._read_conn().execute(sql, params).fetchall()

    # ---------- 写线程 ----------

    def _writer_loop(self) -> None:
        conn = self._make_conn(self.path)
        conn.executescript(SCHEMA)
        _migrate(conn)
        self._ready.set()
        pending: list[tuple] = []
        last_flush = time.monotonic()

        def flush() -> None:
            if not pending:
                return
            conn.execute("BEGIN")
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO task_events (task_id, seq, ts, type, payload)"
                    " VALUES (?, ?, ?, ?, ?)",
                    pending,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            pending.clear()

        def drain() -> None:
            # 全部取走，由 flush 控制批的大小。若只取 batch_size，
            # 剩下的会滞留在共享缓冲里，导致 flush 返回时数据还没落盘。
            with self._events_lock:
                take = list(self._pending_events)
                self._pending_events.clear()
            pending.extend(take)

        def flush_pending() -> None:
            nonlocal last_flush
            try:
                flush()
            except Exception:
                pending.clear()
            last_flush = time.monotonic()

        while True:
            drain()
            due = len(pending) >= self._batch_size or (
                pending and time.monotonic() - last_flush >= self._batch_interval_s
            )
            if due:
                flush_pending()

            try:
                op = self._q.get(timeout=0.05)
            except queue.Empty:
                continue

            if op is _SENTINEL:
                drain()
                try:
                    flush()
                finally:
                    conn.close()
                return

            # 处理任何指令前先落盘。必须重新 drain —— 本线程刚才是阻塞在 q.get 上，
            # 期间可能有新事件进到共享缓冲，只 flush pending 会漏掉它们。
            drain()
            if pending:
                flush_pending()

            assert isinstance(op, _Op)
            try:
                result = op.fn(conn)
            except Exception as exc:
                op.loop.call_soon_threadsafe(_set_exception, op.future, exc)
            else:
                op.loop.call_soon_threadsafe(_set_result, op.future, result)

    # ---------- 异步接口 ----------

    async def _submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        if self._closed:
            raise RuntimeError("Database 已关闭")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._q.put(_Op(fn=fn, future=fut, loop=loop))
        return await fut

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行一条写语句并**立即提交**（autocommit 下语句本身即事务）。返回 rowcount。"""

        def run(conn: sqlite3.Connection) -> int:
            return conn.execute(sql, params).rowcount

        return await self._submit(run)

    async def execute_returning(
        self, sql: str, params: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        """执行带 RETURNING 的写语句，返回首行。同样是提交后才返回。"""

        def run(conn: sqlite3.Connection) -> sqlite3.Row | None:
            return conn.execute(sql, params).fetchone()

        return await self._submit(run)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = list(rows)
        if not rows:
            return

        def run(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN")
            try:
                conn.executemany(sql, rows)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        await self._submit(run)

    async def transaction(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """在一个事务里跑多条语句。

        定时任务触发要用：**建任务和推进 `next_run_at` 必须同生共死**，
        否则崩在中间要么丢一次触发、要么重启后再触发一遍。
        只有写线程会碰这条连接，所以不需要额外的锁。
        """

        def run(conn: sqlite3.Connection) -> Any:
            conn.execute("BEGIN")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return result

        return await self._submit(run)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._read, sql, params)

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    # ---------- 事件批量通道 ----------

    async def append_events(self, rows: Iterable[tuple[str, int, str, str, str | None]]) -> None:
        """把语义事件排进批量通道，**不等待提交**。丢掉的风险由原始 NDJSON 日志兜底。"""
        with self._events_lock:
            self._pending_events.extend(rows)

    async def flush_events(self) -> None:
        """强制把待写事件刷出去。写线程在处理任何指令前都会先落事件，所以这里提交个空操作即可。"""
        await self._submit(lambda conn: None)
