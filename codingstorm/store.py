"""业务查询层。SQL 集中在这里，上层不直接写 SQL。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from codingstorm.db import Database
from codingstorm.models import (
    AttemptOut,
    ProjectCreate,
    ProjectOut,
    TaskCreate,
    TaskOut,
    TaskStatus,
)

TASK_FIELDS = (
    "id, project_id, title, body, kind, status, priority, branch, worktree_path,"
    " base_commit, commit_sha, merge_commit_sha, last_event_at,"
    " created_at, started_at, finished_at, error_text"
)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ---------- 项目 ----------

    async def create_project(self, spec: ProjectCreate) -> ProjectOut:
        pid = new_id()
        await self.db.execute(
            "INSERT INTO projects (id, name, repo_path, target_branch, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (pid, spec.name, spec.repo_path, spec.target_branch, utcnow()),
        )
        row = await self.db.query_one("SELECT * FROM projects WHERE id = ?", (pid,))
        return _project_out(row)

    async def get_project(self, project_id: str) -> ProjectOut | None:
        row = await self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _project_out(row) if row else None

    async def get_project_by_name(self, name: str) -> ProjectOut | None:
        row = await self.db.query_one("SELECT * FROM projects WHERE name = ?", (name,))
        return _project_out(row) if row else None

    async def list_projects(self) -> list[ProjectOut]:
        rows = await self.db.query("SELECT * FROM projects ORDER BY name")
        return [_project_out(r) for r in rows]

    # ---------- 任务 ----------

    async def create_task(self, project_id: str, spec: TaskCreate) -> TaskOut:
        tid = new_id()
        await self.db.execute(
            "INSERT INTO tasks (id, project_id, title, body, kind, status, priority, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid,
                project_id,
                spec.title,
                spec.body,
                str(spec.kind),
                str(TaskStatus.QUEUED),
                spec.priority,
                utcnow(),
            ),
        )
        task = await self.get_task(tid)
        assert task is not None
        return task

    async def get_task(self, task_id: str) -> TaskOut | None:
        row = await self.db.query_one(
            f"SELECT {TASK_FIELDS} FROM tasks WHERE id = ?", (task_id,)
        )
        return _task_out(row) if row else None

    async def get_task_raw(self, task_id: str) -> sqlite3.Row | None:
        return await self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def list_tasks(
        self, *, project_id: str | None = None, status: str | None = None, limit: int = 200
    ) -> list[TaskOut]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self.db.query(
            f"SELECT {TASK_FIELDS} FROM tasks {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [_task_out(r) for r in rows]

    async def claim_next(self) -> sqlite3.Row | None:
        """原子领取下一条可执行任务。

        规则：每项目同时只能有一条 running —— 这是硬约束（同一工作目录里并发跑两个
        Claude Code 会互相踩文件）。项目之间可以并行，由调用方控制全局并发上限。

        靠单条 UPDATE + RETURNING 完成，不select-then-update，避免竞态。
        """
        return await self.db.execute_returning(
            """
            UPDATE tasks
               SET status = 'running', started_at = ?
             WHERE id = (
                   SELECT t.id
                     FROM tasks t
                     JOIN projects p ON p.id = t.project_id
                    WHERE t.status = 'queued'
                      AND p.enabled = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM tasks r
                           WHERE r.project_id = t.project_id
                             AND r.status = 'running'
                      )
                    ORDER BY t.priority DESC, t.created_at ASC
                    LIMIT 1
             )
               AND status = 'queued'
            RETURNING *
            """,
            (utcnow(),),
        )

    async def update_fields(self, task_id: str, **fields: Any) -> None:
        """只更新字段，**不碰 status**。

        需要这个是因为 `claim_next` 已经把状态置为 running 了，执行过程中再写一次
        status 会覆盖掉并发的状态变更（比如 stop() 刚写入的 interrupted）。
        """
        if not fields:
            return
        sets = [f"{key} = ?" for key in fields]
        params = list(fields.values())
        params.append(task_id)
        await self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    async def set_status(self, task_id: str, status: TaskStatus, **fields: Any) -> None:
        sets = ["status = ?"]
        params: list[Any] = [str(status)]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        if status in {
            TaskStatus.AWAITING_REVIEW,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
            TaskStatus.DISCARDED,
            TaskStatus.MERGED,
            TaskStatus.CANCELLED,
        }:
            sets.append("finished_at = ?")
            params.append(utcnow())
        params.append(task_id)
        await self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    async def requeue(self, task_id: str) -> None:
        """把失败/中断的任务放回队列。重试时会重建 worktree 与分支。"""
        await self.db.execute(
            "UPDATE tasks SET status = ?, started_at = NULL, finished_at = NULL,"
            " error_text = NULL WHERE id = ?",
            (str(TaskStatus.QUEUED), task_id),
        )

    async def touch_task(self, task_id: str) -> None:
        await self.db.execute("UPDATE tasks SET last_event_at = ? WHERE id = ?", (utcnow(), task_id))

    # ---------- 尝试 ----------

    async def next_attempt_no(self, task_id: str) -> int:
        row = await self.db.query_one(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM attempts WHERE task_id = ?",
            (task_id,),
        )
        return int(row["n"])

    async def start_attempt(
        self, task_id: str, attempt_no: int, *, origin: str = "task"
    ) -> None:
        await self.db.execute(
            "INSERT INTO attempts (task_id, attempt_no, origin, started_at)"
            " VALUES (?, ?, ?, ?)",
            (task_id, attempt_no, origin, utcnow()),
        )

    async def finish_attempt(self, task_id: str, attempt_no: int, **fields: Any) -> None:
        allowed = {
            "session_id",
            "pid",
            "pid_starttime",
            "model",
            "exit_code",
            "result_subtype",
            "is_error",
            "error_text",
            "duration_ms",
            "num_turns",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "cost_usd",
            "price_version",
            "raw_log_path",
        }
        sets, params = ["finished_at = ?"], [utcnow()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"attempts 表没有字段 {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        params.extend([task_id, attempt_no])
        await self.db.execute(
            f"UPDATE attempts SET {', '.join(sets)} WHERE task_id = ? AND attempt_no = ?", params
        )

    async def list_attempts(self, task_id: str) -> list[AttemptOut]:
        rows = await self.db.query(
            "SELECT attempt_no, session_id, model, exit_code, result_subtype, is_error,"
            " error_text, num_turns, input_tokens, output_tokens, cache_read_tokens,"
            " cache_creation_tokens, cost_usd, price_version, origin, started_at, finished_at"
            " FROM attempts WHERE task_id = ? ORDER BY attempt_no",
            (task_id,),
        )
        return [AttemptOut(**{k: r[k] for k in r.keys()}) for r in rows]

    async def usage_summary(
        self, *, project_id: str | None = None, origin: str | None = None
    ) -> list[sqlite3.Row]:
        clauses, params = [], []
        if project_id:
            clauses.append("t.project_id = ?")
            params.append(project_id)
        if origin:
            clauses.append("a.origin = ?")
            params.append(origin)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.db.query(
            f"""
            SELECT COALESCE(a.model, '未知') AS model,
                   COUNT(*) AS attempts,
                   COALESCE(SUM(a.input_tokens), 0)  AS input_tokens,
                   COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(a.cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(a.cache_creation_tokens), 0) AS cache_creation_tokens,
                   SUM(a.cost_usd) AS cost_usd
              FROM attempts a
              JOIN tasks t ON t.id = a.task_id
              {where}
             GROUP BY COALESCE(a.model, '未知')
             ORDER BY attempts DESC
            """,
            params,
        )

    async def count_attempts(self, *, origin: str | None = None) -> int:
        if origin:
            row = await self.db.query_one(
                "SELECT COUNT(*) AS c FROM attempts WHERE origin = ?", (origin,)
            )
        else:
            row = await self.db.query_one("SELECT COUNT(*) AS c FROM attempts")
        return int(row["c"]) if row else 0

    # ---------- 事件 ----------

    async def next_event_seq(self, task_id: str) -> int:
        row = await self.db.query_one(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM task_events WHERE task_id = ?",
            (task_id,),
        )
        return int(row["n"])

    async def list_events(self, task_id: str, *, after_seq: int = -1, limit: int = 1000) -> list:
        return await self.db.query(
            "SELECT seq, ts, type, payload FROM task_events"
            " WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (task_id, after_seq, limit),
        )

    # ---------- 运维 ----------

    async def reconcile_orphans(self) -> list[sqlite3.Row]:
        """崩溃恢复：把遗留的 running 任务挑出来，交给上层核对进程是否还活着。

        **以 git 和进程的真实状态为准**，不是简单地把 running 改成 failed。
        """
        return await self.db.query(
            "SELECT t.*, a.pid, a.pid_starttime, a.attempt_no"
            " FROM tasks t"
            " LEFT JOIN attempts a ON a.task_id = t.id"
            "   AND a.attempt_no = (SELECT MAX(attempt_no) FROM attempts WHERE task_id = t.id)"
            " WHERE t.status = 'running'"
        )


def _project_out(row: sqlite3.Row) -> ProjectOut:
    return ProjectOut(
        id=row["id"],
        name=row["name"],
        repo_path=row["repo_path"],
        target_branch=row["target_branch"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def _task_out(row: sqlite3.Row) -> TaskOut:
    keys = row.keys()
    return TaskOut(**{k: row[k] for k in keys if k in TaskOut.model_fields})
