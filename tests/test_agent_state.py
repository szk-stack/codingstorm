"""Phase 8 状态机契约测试。纯文件操作，不碰数据库和 git。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codingstorm.agent_state import (
    DELIVERY_ARTIFACTS,
    DELIVERY_STAGE,
    GATE_ARTIFACTS,
    GATE_DEV_TOKEN,
    GATE_STAGE,
    StateError,
    approved_idea_sha,
    build_initial_instruction,
    build_resume_instruction,
    collect_artifacts,
    describe,
    file_sha,
    is_authorized,
    new_state,
    read_state,
    record_gate,
    state_path,
    validate_delivery,
    validate_gate,
    write_state,
)
from codingstorm.config import Config
from codingstorm.runner import Runner


@pytest.fixture
def run_path(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "demo" / "task1"


def _gate_files(run_path: Path) -> None:
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "diagnosis.md").write_text("# 根因\n复现步骤与证据\n", encoding="utf-8")
    (run_path / "idea.md").write_text("# 方案\n准备改什么、什么不变\n", encoding="utf-8")


def _gate_state(run_path: Path, task_id: str = "task1") -> dict:
    _gate_files(run_path)
    st = new_state(task_id, "bugfix")
    st["stage"] = GATE_STAGE
    st["waiting_token"] = GATE_DEV_TOKEN
    st["artifacts"] = collect_artifacts(run_path, GATE_ARTIFACTS)
    write_state(state_path(run_path), st)
    return st


def test_new_state_roundtrip(run_path: Path):
    st = new_state("task1", "bugfix")
    write_state(state_path(run_path), st)
    got = read_state(state_path(run_path), task_id="task1")
    assert got["stage"] == "init"
    assert got["schema_version"] == 1
    assert got["agent_id"] == "bugfix"
    assert got["history"] == []


def test_missing_state_file_is_an_error(run_path: Path):
    with pytest.raises(StateError, match="读不到"):
        read_state(state_path(run_path), task_id="task1")


def test_broken_json_is_an_error(run_path: Path):
    run_path.mkdir(parents=True, exist_ok=True)
    state_path(run_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="不是合法 JSON"):
        read_state(state_path(run_path), task_id="task1")


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda s: s.update(schema_version=99), "版本不匹配"),
        (lambda s: s.update(task_id="other"), "另一条任务"),
        (lambda s: s.update(mode="plain"), "不是 strict"),
        (lambda s: s.update(stage="somewhere"), "未知阶段"),
        (lambda s: s.update(history="nope"), "history"),
        (lambda s: s.update(artifacts=[]), "artifacts"),
        (lambda s: s.update(review_rounds=-1), "review_rounds"),
        (lambda s: s.update(review_rounds=True), "review_rounds"),
    ],
)
def test_bad_fields_are_rejected(run_path: Path, mutate, match: str):
    st = new_state("task1", "bugfix")
    mutate(st)
    write_state(state_path(run_path), st)
    with pytest.raises(StateError, match=match):
        read_state(state_path(run_path), task_id="task1")


def test_collect_artifacts_requires_nonempty_files(run_path: Path):
    run_path.mkdir(parents=True, exist_ok=True)
    (run_path / "diagnosis.md").write_text("ok\n", encoding="utf-8")
    (run_path / "idea.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(StateError, match="idea.md"):
        collect_artifacts(run_path, GATE_ARTIFACTS)

    (run_path / "idea.md").write_text("有内容", encoding="utf-8")
    got = collect_artifacts(run_path, GATE_ARTIFACTS)
    assert got["idea.md"] == file_sha(run_path / "idea.md")


def test_validate_gate(run_path: Path):
    st = _gate_state(run_path)
    assert set(validate_gate(run_path, st, task_id="task1")) == set(GATE_ARTIFACTS)

    st["stage"] = "ship"
    with pytest.raises(StateError, match="期望停在"):
        validate_gate(run_path, st, task_id="task1")

    st["stage"] = GATE_STAGE
    st["waiting_token"] = None
    with pytest.raises(StateError, match="等待"):
        validate_gate(run_path, st, task_id="task1")


def test_validate_delivery_requires_artifacts(run_path: Path):
    st = _gate_state(run_path)
    st["stage"] = DELIVERY_STAGE
    with pytest.raises(StateError, match="产物缺失"):
        validate_delivery(run_path, st, task_id="task1")

    for name in DELIVERY_ARTIFACTS:
        (run_path / name).write_text("ok\n", encoding="utf-8")
    st["artifacts"].update(collect_artifacts(run_path, DELIVERY_ARTIFACTS))
    assert set(validate_delivery(run_path, st, task_id="task1")) == set(DELIVERY_ARTIFACTS)


def test_gate_token_is_recorded_with_idea_sha(run_path: Path):
    st = _gate_state(run_path)
    assert not is_authorized(st)
    assert approved_idea_sha(st) is None

    record_gate(st, token=GATE_DEV_TOKEN, approved_idea_sha="sha256:abc")
    assert is_authorized(st)
    assert approved_idea_sha(st) == "sha256:abc"
    assert st["history"][-1]["token"] == GATE_DEV_TOKEN

    # 平台写的是授权记录，不擅自替 Agent 迁移阶段
    assert st["stage"] == GATE_STAGE


def test_instructions_render_all_placeholders(run_path: Path):
    initial = build_initial_instruction(run_path, max_review_rounds=6)
    assert "<RUN_DIR>" not in initial
    assert "{max_review_rounds}" not in initial
    assert str(run_path) in initial
    assert "最多 6 轮" in initial
    assert "gate_dev" in initial

    st = _gate_state(run_path)
    resume = build_resume_instruction(
        run_path, st, token=GATE_DEV_TOKEN,
        approved_idea_sha="sha256:abc", max_review_rounds=3,
    )
    assert "<RUN_DIR>" not in resume
    assert "{max_review_rounds}" not in resume
    assert "最多 3 轮" in resume
    assert "sha256:abc" in resume
    assert "人工授权已由平台校验" in resume
    # 指令里有 JSON 示例，占位符替换不能用 str.format（会把花括号当格式字段）
    assert '"from"' in initial


def test_describe_lists_artifacts_and_evidence(run_path: Path):
    st = _gate_state(run_path)
    (run_path / "evidence").mkdir(exist_ok=True)
    (run_path / "evidence" / "pytest.txt").write_text("1 passed", encoding="utf-8")
    view = describe(run_path, st)
    assert view["stage"] == GATE_STAGE
    assert view["waiting_token"] == GATE_DEV_TOKEN
    assert view["authorized"] is False
    assert [a["name"] for a in view["artifacts"]] == list(GATE_ARTIFACTS)
    assert view["evidence"] == ["pytest.txt"]
    assert view["state_path"] == str(state_path(run_path))


def test_build_command_supports_multiple_add_dirs(tmp_path: Path):
    runner = Runner(Config(root=tmp_path), None)  # type: ignore[arg-type]
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"

    cmd = runner.build_command(
        "prompt", session_id="s", context_dir=None, context_dirs=[a, b]
    )
    assert cmd.count("--add-dir") == 2
    assert str(a) in cmd and str(b) in cmd

    # 只传 context_dir 的旧调用方式必须保持原样
    legacy = runner.build_command("prompt", session_id="s", context_dir=c)
    assert legacy.count("--add-dir") == 1
    assert str(c) in legacy


def test_write_state_is_atomic(tmp_path: Path):
    p = state_path(tmp_path)
    write_state(p, new_state("t", "bugfix"))
    assert not (tmp_path / "state.json.tmp").exists()
    assert json.loads(p.read_text(encoding="utf-8"))["task_id"] == "t"
