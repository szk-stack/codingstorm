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
    ScheduleOut,
    TaskCreate,
    TaskMessageOut,
    TaskOut,
    TaskStatus,
)
from codingstorm.timing import TimingError, iso_utc, parse_rule

TASK_FIELDS = (
    "id, project_id, title, body, kind, status, priority, branch, worktree_path,"
    " base_commit, commit_sha, merge_commit_sha, last_event_at,"
    " created_at, started_at, finished_at, error_text, schedule_id"
)


def utcnow() -> str:
    return iso_utc(datetime.now(UTC))


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

    async def set_project_window(self, project_id: str, override: str | None) -> ProjectOut:
        await self.db.execute(
            "UPDATE projects SET window_override = ? WHERE id = ?", (override, project_id)
        )
        project = await self.get_project(project_id)
        assert project is not None
        return project

    # ---------- 任务 ----------

    async def create_task(
        self, project_id: str, spec: TaskCreate, *, schedule_id: str | None = None
    ) -> TaskOut:
        return await self.db.transaction(
            lambda conn: _insert_task(
                conn, project_id=project_id, spec=spec, schedule_id=schedule_id
            )
        )

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

    async def claim_next(self, *, allowed_projects: list[str] | None = None) -> sqlite3.Row | None:
        """原子领取下一条可执行任务。

        规则：每项目同时只能有一条 running —— 这是硬约束（同一工作目录里并发跑两个
        Claude Code 会互相踩文件）。项目之间可以并行，由调用方控制全局并发上限。

        靠单条 UPDATE + RETURNING 完成，不select-then-update，避免竞态。

        `allowed_projects` 是**执行窗口**的落点：调度器算出此刻哪些项目允许跑，
        这里只在这些项目里取。窗口的时间判断不能写进 SQL —— 跨越零点、多段、
        项目级覆盖，放在 Python 里是几行，写在 SQL 里是一团。传 None 表示不限制。
        """
        if allowed_projects is not None and not allowed_projects:
            return None

        scope, scope_params = "", []
        if allowed_projects is not None:
            placeholders = ", ".join("?" for _ in allowed_projects)
            scope = f" AND t.project_id IN ({placeholders})"
            scope_params = list(allowed_projects)

        return await self.db.execute_returning(
            f"""
            UPDATE tasks
               SET status = 'running', started_at = ?
             WHERE id = (
                   SELECT t.id
                     FROM tasks t
                     JOIN projects p ON p.id = t.project_id
                    WHERE t.status = 'queued'
                      AND p.enabled = 1
                      {scope}
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
            [utcnow(), *scope_params],
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
        """把失败/中断/追加过消息的任务放回队列。

        是重建工作区还是接着上一轮跑，由调度器按「有没有追加消息」决定 ——
        存储层只负责把它放回队列。
        """
        await self.db.execute(
            "UPDATE tasks SET status = ?, started_at = NULL, finished_at = NULL,"
            " error_text = NULL WHERE id = ?",
            (str(TaskStatus.QUEUED), task_id),
        )

    # ---------- 定时任务 ----------

    _SCHEDULE_SELECT = (
        "SELECT s.*, t.status AS last_task_status"
        " FROM schedules s LEFT JOIN tasks t ON t.id = s.last_task_id"
    )

    async def create_schedule(
        self, project_id: str, spec, *, rule_json: str, next_run_at: str
    ) -> ScheduleOut:
        sid = new_id()
        await self.db.execute(
            "INSERT INTO schedules"
            " (id, project_id, title, body, kind, priority, rule, next_run_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                project_id,
                spec.title,
                spec.body,
                str(spec.kind),
                spec.priority,
                rule_json,
                next_run_at,
                utcnow(),
            ),
        )
        schedule = await self.get_schedule(sid)
        assert schedule is not None
        return schedule

    async def get_schedule(self, schedule_id: str) -> ScheduleOut | None:
        row = await self.db.query_one(
            f"{self._SCHEDULE_SELECT} WHERE s.id = ?", (schedule_id,)
        )
        return _schedule_out(row) if row else None

    async def list_schedules(self, *, project_id: str | None = None) -> list[ScheduleOut]:
        where, params = "", []
        if project_id:
            where, params = " WHERE s.project_id = ?", [project_id]
        rows = await self.db.query(
            f"{self._SCHEDULE_SELECT}{where} ORDER BY s.created_at DESC", params
        )
        return [_schedule_out(r) for r in rows]

    async def delete_schedule(self, schedule_id: str) -> None:
        await self.db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        """改定时任务。`next_run_at` 单独走 `set_schedule_next` —— 它要和任务创建同事务。"""
        allowed = {"enabled", "title", "body", "priority", "rule", "next_run_at", "missed_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"schedules 表没有字段 {', '.join(sorted(unknown))}")
        if not fields:
            return
        sets = [f"{key} = ?" for key in fields]
        params = [*fields.values(), schedule_id]
        await self.db.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", params)

    async def due_schedules(self, now_iso: str) -> list[ScheduleOut]:
        rows = await self.db.query(
            f"{self._SCHEDULE_SELECT}"
            " WHERE s.enabled = 1 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ?"
            " ORDER BY s.next_run_at",
            (now_iso,),
        )
        return [_schedule_out(r) for r in rows]

    async def fire_schedule(
        self,
        schedule: ScheduleOut,
        spec: TaskCreate,
        *,
        expected_next_run_at: str,
        next_run_at: str | None,
        now_iso: str,
    ) -> TaskOut | None:
        """触发一次：**建任务和推进 `next_run_at` 必须同生共死**。

        崩在两者之间，要么这次触发凭空消失、要么重启后重复建一条任务。
        所以走一个事务，并且用「比对读取时的 next_run_at」当乐观锁：
        不匹配就说明已经有人触发过，返回 None，绝不重复。
        """

        def run(conn: sqlite3.Connection) -> TaskOut | None:
            cur = conn.execute(
                "UPDATE schedules SET next_run_at = ?, last_run_at = ?,"
                " run_count = run_count + 1 WHERE id = ? AND next_run_at = ?",
                (next_run_at, now_iso, schedule.id, expected_next_run_at),
            )
            if cur.rowcount == 0:
                return None
            created = _insert_task(
                conn, project_id=schedule.project_id, spec=spec, schedule_id=schedule.id
            )
            conn.execute("UPDATE schedules SET last_task_id = ? WHERE id = ?", (created.id, schedule.id))
            return created

        return await self.db.transaction(run)

    async def expire_schedule(
        self, schedule: ScheduleOut, *, expected_next_run_at: str, now_iso: str
    ) -> bool:
        """错过了时刻：停用并标记，**不建任务**。

        一次性任务错过就永远没了，静默消失最糟糕 —— 留在列表里标成「已错过」，
        用户看得见，还能自己重新排期。返回是否真的标记成功（乐观锁）。
        """
        cur = await self.db.execute(
            "UPDATE schedules SET next_run_at = NULL, missed_at = ?, enabled = 0"
            " WHERE id = ? AND next_run_at = ?",
            (now_iso, schedule.id, expected_next_run_at),
        )
        return cur > 0

    async def run_schedule_now(self, schedule: ScheduleOut, spec: TaskCreate) -> TaskOut:
        """立刻跑一次。

        **不动 `next_run_at`** —— 这是额外的一次，原定排期照旧。也不计进 `run_count`
        （那个数专门表示「自动触发了几次」），但会更新 `last_task_id`，
        这样界面上「上一轮还没审」的提示把手动那次也算进去。
        """

        def run(conn: sqlite3.Connection) -> TaskOut:
            created = _insert_task(
                conn, project_id=schedule.project_id, spec=spec, schedule_id=schedule.id
            )
            conn.execute(
                "UPDATE schedules SET last_task_id = ? WHERE id = ?", (created.id, schedule.id)
            )
            return created

        return await self.db.transaction(run)

    async def advance_schedule(
        self, schedule_id: str, *, expected_next_run_at: str, next_run_at: str
    ) -> bool:
        """只推进下一次触发时刻，不建任务 —— 周期任务错过一轮时走这里。

        跳过而不是补跑：**从当前时刻往后算，不做「上次 + 间隔」的追赶**，
        否则平台停机三天，开机时会一口气冒出三条任务。
        """
        cur = await self.db.execute(
            "UPDATE schedules SET next_run_at = ? WHERE id = ? AND next_run_at = ?",
            (next_run_at, schedule_id, expected_next_run_at),
        )
        return cur > 0

    # ---------- 追加消息（多轮对话） ----------

    async def add_message(self, task_id: str, text: str) -> int:
        """追加一轮用户输入，返回序号。序号在同一条语句里算，不会撞号。"""
        row = await self.db.execute_returning(
            "INSERT INTO task_messages (task_id, seq, text, created_at)"
            " VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_messages WHERE task_id = ?), ?, ?)"
            " RETURNING seq",
            (task_id, task_id, text, utcnow()),
        )
        assert row is not None
        return row["seq"]

    async def list_messages(self, task_id: str) -> list[TaskMessageOut]:
        rows = await self.db.query(
            "SELECT seq, text, created_at FROM task_messages WHERE task_id = ? ORDER BY seq",
            (task_id,),
        )
        return [TaskMessageOut(**{k: r[k] for k in r.keys()}) for r in rows]

    async def last_session_id(self, task_id: str) -> str | None:
        """最近一次任务执行跑出来的会话 id —— 接着聊就是接在它后面。

        **必须排掉沉淀**：沉淀是另一次独立调用、另一个会话，而且它的 prompt 里
        有任务标题和 diff。接错会话的话，多轮对话看起来还能答上来（因为沉淀那次
        也见过任务内容），实际上完全没接在任务历史上 —— 实测踩过。
        """
        row = await self.db.query_one(
            "SELECT session_id FROM attempts"
            " WHERE task_id = ? AND session_id IS NOT NULL AND origin = 'task'"
            " ORDER BY attempt_no DESC LIMIT 1",
            (task_id,),
        )
        return row["session_id"] if row else None

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


def _insert_task(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    spec: TaskCreate,
    schedule_id: str | None,
) -> TaskOut:
    """写一条 queued 任务并回读。

    **必须在写它的那条连接上回读** —— 读连接是另一条，看不到未提交的行。
    """
    tid = new_id()
    conn.execute(
        "INSERT INTO tasks"
        " (id, project_id, title, body, kind, status, priority, created_at, schedule_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            project_id,
            spec.title,
            spec.body,
            str(spec.kind),
            str(TaskStatus.QUEUED),
            spec.priority,
            utcnow(),
            schedule_id,
        ),
    )
    row = conn.execute(f"SELECT {TASK_FIELDS} FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row is not None
    return _task_out(row)


def _project_out(row: sqlite3.Row) -> ProjectOut:
    keys = row.keys()
    return ProjectOut(
        id=row["id"],
        name=row["name"],
        repo_path=row["repo_path"],
        target_branch=row["target_branch"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        window_override=row["window_override"] if "window_override" in keys else None,
    )


def _task_out(row: sqlite3.Row) -> TaskOut:
    keys = row.keys()
    return TaskOut(**{k: row[k] for k in keys if k in TaskOut.model_fields})


def _schedule_out(row: sqlite3.Row) -> ScheduleOut:
    keys = row.keys()
    data = {k: row[k] for k in keys if k in ScheduleOut.model_fields}
    data["enabled"] = bool(row["enabled"])
    data["last_task_status"] = row["last_task_status"] if "last_task_status" in keys else None
    try:
        data["rule_text"] = parse_rule(row["rule"]).describe()
    except (TimingError, ValueError):
        # 规则坏了不该让整个列表打不开，原样显示出来让人看见问题在哪
        data["rule_text"] = str(row["rule"])
    return ScheduleOut(**data)
