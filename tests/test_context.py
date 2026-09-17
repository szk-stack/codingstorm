"""上下文三层测试。"""

from pathlib import Path

import pytest

from codingstorm.config import Config
from codingstorm.context import (
    INDEX_FILENAME,
    POINTER_FILENAME,
    ContextStore,
    strip_html_comments,
)
from codingstorm.models import TaskOut
from codingstorm.store import utcnow


@pytest.fixture
def store(tmp_path: Path) -> ContextStore:
    cfg = Config(root=tmp_path / "root")
    cfg.ensure_dirs()
    return ContextStore(cfg)


def _task(title: str = "加个函数", body: str = "", kind: str = "requirement") -> TaskOut:
    return TaskOut(
        id="t1", project_id="p1", title=title, body=body, kind=kind,
        status="queued", priority=0, created_at=utcnow(),
    )


# ---------- 模板与存取 ----------

def test_ensure_seeds_templates(store: ContextStore):
    ctx = store.ensure("demo")
    assert ctx.pointer.read_text(encoding="utf-8").strip()
    assert ctx.journal.read_text(encoding="utf-8").strip()
    assert ctx.index.read_text(encoding="utf-8").strip()
    assert ctx.docs.is_dir()


def test_ensure_does_not_overwrite(store: ContextStore):
    store.ensure("demo")
    store.write_pointer("demo", "# 我改过了\n")
    store.ensure("demo")
    assert store.read_pointer("demo") == "# 我改过了\n"


def test_projects_are_isolated(store: ContextStore):
    store.write_pointer("a", "# A 的约定\n")
    store.write_pointer("b", "# B 的约定\n")
    assert "A 的约定" in store.read_pointer("a")
    assert "B 的约定" in store.read_pointer("b")
    assert "B" not in store.read_pointer("a")


# ---------- 注释剥离 ----------

def test_strip_html_comments():
    assert strip_html_comments("a<!-- 注释 -->b") == "ab"
    assert strip_html_comments("a\n<!--\n多行\n注释\n-->\nb") == "a\n\nb"
    assert strip_html_comments("无注释") == "无注释"


def test_prompt_does_not_contain_template_guidance(store: ContextStore):
    """模板里的说明写在 HTML 注释里。

    CLAUDE.md 的注释会被 Claude Code 自动剥离，但我们走 `-p` 注入，
    不经过那道处理 —— 不自己剥掉的话会白白占 token。
    """
    prompt = store.build_prompt("demo", _task())
    assert "只写四类内容" not in prompt, "模板说明漏进 prompt 了"
    assert "不要写「这个项目是做什么的」" not in prompt


# ---------- 组装 prompt ----------

def test_prompt_contains_all_three_layers(store: ContextStore):
    store.write_pointer("demo", "# 约定\n\n跑 `pytest -q` 测试。\n")
    store.write_doc("demo", "api.md", "# 接口约定\n")
    store.append_journal("demo", "## 之前改过 X\n\n<!-- source: 因为 Y -->\n")

    prompt = store.build_prompt("demo", _task("加个接口", "细节描述"))

    assert "跑 `pytest -q` 测试。" in prompt      # 层 1
    assert "api.md" in prompt                    # 层 2 的索引
    assert "之前改过 X" in prompt                 # 层 3
    assert "加个接口" in prompt and "细节描述" in prompt


def test_prompt_omits_empty_layers(store: ContextStore):
    store.write_pointer("demo", "<!-- 只有注释 -->\n")
    store.write_index("demo", "<!-- 空 -->\n")
    prompt = store.build_prompt("demo", _task())
    assert "项目约定" not in prompt
    assert "可查阅的项目文档" not in prompt


def test_journal_is_truncated_in_prompt(store: ContextStore):
    """变更记录会一直增长，全量塞进 prompt 迟早挤爆上下文。"""
    for i in range(200):
        store.append_journal("demo", f"## 第 {i} 条\n\n{'填充' * 50}\n")
    prompt = store.build_prompt("demo", _task(), max_journal_chars=1000)
    assert "只显示最近部分" in prompt
    assert len(prompt) < 20000


def test_prompt_mentions_context_dir_absolute_path(store: ContextStore):
    store.write_doc("demo", "note.md", "x")
    prompt = store.build_prompt("demo", _task())
    assert str(store.for_project("demo").docs) in prompt


# ---------- 文档管理 ----------

def test_list_and_read_docs(store: ContextStore):
    store.write_doc("demo", "a.md", "内容 A")
    store.write_doc("demo", "sub/b.md", "内容 B")
    paths = {d.path for d in store.list_docs("demo")}
    assert "a.md" in paths and "sub/b.md" in paths and INDEX_FILENAME in paths
    assert store.read_doc("demo", "sub/b.md") == "内容 B"


def test_doc_path_traversal_is_rejected(store: ContextStore):
    """文档路径来自 HTTP，必须挡住越界。"""
    for bad in ("../secret.txt", "../../etc/passwd", "sub/../../outside.md"):
        with pytest.raises(ValueError):
            store.resolve_doc("demo", bad)
        with pytest.raises(ValueError):
            store.write_doc("demo", bad, "x")


def test_delete_doc(store: ContextStore):
    store.write_doc("demo", "gone.md", "x")
    store.delete_doc("demo", "gone.md")
    assert "gone.md" not in {d.path for d in store.list_docs("demo")}
    store.delete_doc("demo", "gone.md")  # 再删一次不应报错


# ---------- 沉淀 ----------

def test_journal_has_task_tracks_marker(store: ContextStore):
    store.ensure("demo")
    assert not store.journal_has_task("demo", "abc123")

    store.append_journal("demo", "## 改了什么\n\n<!-- codingstorm-task: abc123 -->\n")
    assert store.journal_has_task("demo", "abc123")
    assert not store.journal_has_task("demo", "other")

    # 再追加一次不会改变判断 —— 幂等靠调用方先查这个标记
    store.append_journal("demo", "## 重复\n\n<!-- codingstorm-task: abc123 -->\n")
    assert store.journal_has_task("demo", "abc123")


def test_pointer_filename_constant_matches_template(store: ContextStore):
    assert store.for_project("demo").pointer.name == POINTER_FILENAME
