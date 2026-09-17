"""执行器测试。不真的拉起 Claude Code —— 只验证命令行构造与事件处理逻辑。"""

import asyncio
import json
from pathlib import Path

import pytest

from codingstorm.config import Config
from codingstorm.db import Database
from codingstorm.models import ProjectCreate, TaskCreate, TaskStatus
from codingstorm.runner import (
    MAX_PAYLOAD_BYTES,
    RunOutcome,
    Runner,
    read_pid_starttime,
    truncate_payload,
)
from codingstorm.store import Store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path: Path):
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    db = Database(tmp_path / "test.db")
    db.start()
    store = Store(db)
    runner = Runner(cfg, store)
    try:
        yield cfg, store, runner
    finally:
        db.close()


async def _mk_task(store: Store) -> str:
    p = await store.create_project(
        ProjectCreate(name="demo", repo_path="/tmp/demo", target_branch="main")
    )
    t = await store.create_task(p.id, TaskCreate(title="A"))
    return t.id


def _line(event: dict) -> bytes:
    return json.dumps(event).encode()


# ---------- 命令行 ----------

def test_build_command_has_required_flags(env):
    cfg, _, runner = env
    ctx = Path("/tmp/ctx")
    cmd = runner.build_command("do it", session_id="abc", context_dir=ctx)
    # Phase 0 实测：缺 --verbose 会直接报错退出
    assert "--verbose" in cmd
    assert "--output-format" in cmd and "stream-json" in cmd
    # 无人值守时必须禁掉提问工具
    assert "--disallowed-tools" in cmd and "AskUserQuestion" in cmd
    assert "--add-dir" in cmd and str(ctx) in cmd
    assert "--session-id" in cmd and "abc" in cmd
    assert "--max-turns" in cmd


def test_build_command_omits_add_dir_when_none(env):
    _, _, runner = env
    cmd = runner.build_command("x", session_id="s", context_dir=None)
    assert "--add-dir" not in cmd


# ---------- 事件处理 ----------

def test_result_event_is_authoritative(env):
    """退出码粒度太粗，成败判据只能是 result 的 subtype/is_error。"""
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "num_turns": 4,
                "duration_ms": 1234,
                "total_cost_usd": 0.26,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 500,
                },
            }),
            task_id,
            outcome,
        )
        assert outcome.saw_result
        assert outcome.ok
        assert outcome.result_text == "done"
        assert outcome.input_tokens == 100
        assert outcome.cache_read_tokens == 500

    run(main())


def test_error_max_turns_marks_failure(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": None}),
            task_id,
            outcome,
        )
        assert outcome.saw_result
        assert not outcome.ok

    run(main())


def test_init_event_captures_session_and_model(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({
                "type": "system",
                "subtype": "init",
                "session_id": "sess-1",
                "model": "deepseek-v4-flash",
                "cwd": "/tmp/x",
                "tools": ["Bash"],
            }),
            task_id,
            outcome,
        )
        assert outcome.session_id == "sess-1"
        assert outcome.model == "deepseek-v4-flash"
        await store.db.flush_events()
        rows = await store.list_events(task_id)
        assert [r["type"] for r in rows] == ["init"]

    run(main())


def test_thinking_tokens_is_not_persisted(env):
    """实测它占事件量的 99.6%，一条都不能落库。"""
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        for i in range(50):
            await runner._handle_line(
                _line({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": i}),
                task_id,
                outcome,
            )
        await store.db.flush_events()
        rows = await store.list_events(task_id)
        assert rows == []

    run(main())


def test_stream_event_is_not_persisted(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({"type": "stream_event", "event": {"type": "content_block_delta"}}),
            task_id,
            outcome,
        )
        await store.db.flush_events()
        assert await store.list_events(task_id) == []

    run(main())


def test_fatal_auth_retry_stops_early(env):
    """401 会退避重试 10 次（约 3 分钟），必须立刻停。"""
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({
                "type": "system",
                "subtype": "api_retry",
                "attempt": 1,
                "max_retries": 10,
                "retry_delay_ms": 622,
                "error_status": 401,
                "error": "authentication_failed",
            }),
            task_id,
            outcome,
        )
        assert outcome.error_text is not None
        assert "401" in outcome.error_text

    run(main())


def test_transient_retry_does_not_stop(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({"type": "system", "subtype": "api_retry", "error_status": 503}),
            task_id,
            outcome,
        )
        assert outcome.error_text is None

    run(main())


def test_malformed_line_does_not_break_loop(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(b"{not json", task_id, outcome)
        await runner._handle_line(b"", task_id, outcome)
        await runner._handle_line(b'["a list", "not an object"]', task_id, outcome)
        # 还能继续处理正常事件
        await runner._handle_line(
            _line({"type": "result", "subtype": "success", "is_error": False}), task_id, outcome
        )
        assert outcome.saw_result

    run(main())


def test_assistant_tool_use_is_captured(env):
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        outcome = RunOutcome()
        await runner._handle_line(
            _line({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "我来跑一下"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                    ]
                },
            }),
            task_id,
            outcome,
        )
        await store.db.flush_events()
        rows = await store.list_events(task_id)
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["tool_uses"][0]["name"] == "Bash"

    run(main())


def test_consecutive_events_get_distinct_seq(env):
    """回归：seq 必须在内存里自增。

    之前每次都去库里查 MAX(seq)，但事件是批量提交的，前一条还没落盘时查出来是旧值，
    连续几条会拿到同一个 seq，然后被 INSERT OR REPLACE 互相覆盖 —— 静默丢事件。
    线上就是这么丢掉一整条 Write 工具调用的。
    """
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        for i in range(5):
            await runner._emit(task_id, "assistant", {"text": f"msg{i}"})
        await store.db.flush_events()

        rows = await store.list_events(task_id)
        assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4], "seq 撞号了"
        assert len(rows) == 5, "有事件被覆盖丢失"

    run(main())


def test_seq_resumes_from_db_after_restart(env):
    """内存计数器要从库里已有的最大值续上，不能从头开始。"""
    _, store, runner = env

    async def main():
        task_id = await _mk_task(store)
        await runner._emit(task_id, "assistant", {"text": "first"})
        await store.db.flush_events()
        runner._seq.pop(task_id, None)  # 模拟进程重启

        await runner._emit(task_id, "assistant", {"text": "second"})
        await store.db.flush_events()

        rows = await store.list_events(task_id)
        assert [r["seq"] for r in rows] == [0, 1]

    run(main())


# ---------- 工具函数 ----------

def test_truncate_payload_keeps_small_objects():
    obj = {"a": 1}
    assert truncate_payload(obj) == obj


def test_truncate_payload_replaces_large_objects():
    obj = {"big": "x" * (MAX_PAYLOAD_BYTES * 2)}
    out = truncate_payload(obj)
    assert "preview" in out
    assert out["truncated_bytes"] > MAX_PAYLOAD_BYTES


def test_read_pid_starttime_returns_none_for_dead_pid():
    assert read_pid_starttime(999_999_999) is None


def test_missing_result_falls_back_to_log_tail(env, tmp_path: Path):
    """崩在 result 落库之前时，从原始日志尾把结果捞回来。"""
    _, _, runner = env
    log = tmp_path / "task.ndjson"
    log.write_bytes(
        b'{"type":"system","subtype":"init"}\n'
        + json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 2,
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }).encode()
        + b"\n"
    )
    outcome = RunOutcome()
    run(runner._fill_from_log_tail(log, outcome))
    assert outcome.saw_result
    assert outcome.input_tokens == 7
