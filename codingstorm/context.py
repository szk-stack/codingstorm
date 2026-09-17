"""上下文的组装与沉淀。

Phase 1 只把任务本身拼成 prompt —— 够跑通。
Phase 4 在这里实现三层机制（必读指针图 / 文档索引按需自取 / 任务后自动沉淀），
设计依据见 docs/context-design.md。
"""

from __future__ import annotations

from codingstorm.models import TaskOut

# 探针会读这个目录（Phase 4 起生效）
POINTER_FILENAME = "pointer.md"
JOURNAL_FILENAME = "journal.md"


def build_prompt(task: TaskOut) -> str:
    """把任务拼成给 Claude Code 的 prompt。

    Phase 4 会在这里前置「指针图」和「文档索引」。注意指针图只写四类内容
    （带参数的 build/test/lint 命令、非显然约束、项目专有工具、硬规则），
    **不要写「这个项目是做什么的」** —— 实测那类信息反而拖低表现。
    """
    parts = [f"# 任务\n\n{task.title}"]
    if task.body.strip():
        parts.append(f"## 详情\n\n{task.body.strip()}")
    if task.kind:
        parts.append(f"（类型：{task.kind}）")
    return "\n\n".join(parts)
