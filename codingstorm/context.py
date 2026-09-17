"""上下文三层。

设计依据见 `docs/context-design.md`，要点：

**层 1 · 必读指针图**（约 100 行，无条件注入）
只写四类真正改变行为的东西：带参数的 build/test/lint 命令、AI 推不出来的非显然约束、
项目专有工具用法、每条任务都适用的硬规则。
**不写「这个项目是做什么的」** —— 实测那类信息反而拖低表现，病根是冗余。

**层 2 · 文档索引**（按需自取）
把文档目录放进 prompt，让 AI 用 Read 自己去取。零额外基建，而且是 Claude Code
的原生工作方式。

**层 3 · 沉淀**（任务结束后自动追加）
不记「改了什么」，要记「**为什么改、何时可删**」：每条挂 source / applicability / expiry。
把「删不删」从开放判断变成封闭谓词 —— 这是文档能否活过三个月的关键。
元数据写在 HTML 注释里。注意：注释在 CLAUDE.md 里会被自动剥离，
但**我们是通过 `-p` 注入的，不会经过那道剥离，得自己处理**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codingstorm.config import Config
from codingstorm.models import TaskOut

POINTER_FILENAME = "pointer.md"
JOURNAL_FILENAME = "journal.md"
DOCS_DIRNAME = "docs"
INDEX_FILENAME = "INDEX.md"

# 指针图的软上限。超了说明在往里塞「介绍性内容」，那类信息实测无益。
POINTER_SOFT_LIMIT_BYTES = 6 * 1024

POINTER_TEMPLATE = """\
# 项目约定

<!--
只写四类内容，其余一律不写：
  1. 带参数的构建 / 测试 / 检查命令
  2. AI 看不出来的非显然约束
  3. 项目专有工具的用法
  4. 每条任务都适用的硬规则

不要写「这个项目是做什么的」「目录结构是怎样的」—— AI 读代码就能得到，
写进来只会稀释信噪比。实测这类内容反而让表现变差。

本文件里的 HTML 注释在注入前会被剥掉，不占 token，可以放心用作备注。
-->

## 构建 / 测试 / 检查命令

<!-- 例：pytest -q ; ruff check codingstorm/ -->

## 非显然约束

<!-- 例：不要用 X，因为 Y -->

## 项目专有工具

<!-- 例：改完接口要跑 `make gen` 重新生成客户端 -->

## 硬规则

<!-- 例：提交信息用中文 -->
"""

JOURNAL_TEMPLATE = """\
# 变更记录

> 由 codingstorm 在每个任务结束后自动追加。每条记录带三个字段（写在 HTML 注释里）：
> `source` 为什么改、`applicability` 何时适用、`expiry` 什么条件下可以删。
> 老记录不删也无妨，但有了 expiry 就能判断它是否已经过期。
"""

INDEX_TEMPLATE = """\
# 文档索引

<!--
把项目文档放在本目录下，然后在这里登记一行：哪个文件讲什么。
AI 会看到这份索引，需要时自己去读对应文件。
-->

（还没有文档）
"""

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html_comments(text: str) -> str:
    """去掉 HTML 注释。

    CLAUDE.md 里的注释会被 Claude Code 自动剥离，但我们是通过 `-p` 注入的，
    不经过那道处理 —— 不自己剥掉的话，模板里的说明文字会白白占 token。
    """
    return _HTML_COMMENT.sub("", text)


@dataclass(frozen=True)
class DocEntry:
    path: str          # 相对 docs/ 的路径
    size: int


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    pointer: Path
    journal: Path
    docs: Path

    @property
    def index(self) -> Path:
        return self.docs / INDEX_FILENAME


class ContextStore:
    def __init__(self, config: Config):
        self.config = config

    def for_project(self, project_name: str) -> ProjectContext:
        root = self.config.contexts_dir / project_name
        return ProjectContext(
            root=root,
            pointer=root / POINTER_FILENAME,
            journal=root / JOURNAL_FILENAME,
            docs=root / DOCS_DIRNAME,
        )

    def ensure(self, project_name: str) -> ProjectContext:
        ctx = self.for_project(project_name)
        ctx.docs.mkdir(parents=True, exist_ok=True)
        for path, template in (
            (ctx.pointer, POINTER_TEMPLATE),
            (ctx.journal, JOURNAL_TEMPLATE),
            (ctx.index, INDEX_TEMPLATE),
        ):
            if not path.exists():
                path.write_text(template, encoding="utf-8")
        return ctx

    # ---------- 读写 ----------

    def read_pointer(self, project_name: str) -> str:
        return self.ensure(project_name).pointer.read_text(encoding="utf-8")

    def write_pointer(self, project_name: str, text: str) -> None:
        ctx = self.ensure(project_name)
        ctx.pointer.write_text(text, encoding="utf-8")

    def read_journal(self, project_name: str) -> str:
        return self.ensure(project_name).journal.read_text(encoding="utf-8")

    def read_index(self, project_name: str) -> str:
        return self.ensure(project_name).index.read_text(encoding="utf-8")

    def write_index(self, project_name: str, text: str) -> None:
        self.ensure(project_name).index.write_text(text, encoding="utf-8")

    def list_docs(self, project_name: str) -> list[DocEntry]:
        ctx = self.ensure(project_name)
        out: list[DocEntry] = []
        for p in sorted(ctx.docs.rglob("*")):
            if p.is_file():
                out.append(DocEntry(path=str(p.relative_to(ctx.docs)).replace("\\", "/"),
                                    size=p.stat().st_size))
        return out

    def resolve_doc(self, project_name: str, rel_path: str) -> Path:
        """解析文档路径，**拒绝越界**（`..` 之类）。"""
        ctx = self.ensure(project_name)
        target = (ctx.docs / rel_path).resolve()
        root = ctx.docs.resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"文档路径越界: {rel_path}")
        return target

    def read_doc(self, project_name: str, rel_path: str) -> str:
        return self.resolve_doc(project_name, rel_path).read_text(encoding="utf-8")

    def write_doc(self, project_name: str, rel_path: str, text: str) -> None:
        path = self.resolve_doc(project_name, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def delete_doc(self, project_name: str, rel_path: str) -> None:
        path = self.resolve_doc(project_name, rel_path)
        if path.exists():
            path.unlink()

    # ---------- 沉淀 ----------

    def append_journal(self, project_name: str, entry: str) -> None:
        ctx = self.ensure(project_name)
        with ctx.journal.open("a", encoding="utf-8") as f:
            f.write("\n" + entry.strip() + "\n")

    def journal_has_task(self, project_name: str, task_id: str) -> bool:
        """沉淀的幂等标记 —— 重试不能重复追加。"""
        marker = f"codingstorm-task: {task_id}"
        return marker in self.read_journal(project_name)

    # ---------- 组装 prompt ----------

    def build_prompt(self, project_name: str, task: TaskOut, *, max_journal_chars: int = 6000) -> str:
        parts: list[str] = []

        pointer = strip_html_comments(self.read_pointer(project_name)).strip()
        if pointer:
            parts.append(f"# 项目约定\n\n{pointer}")

        index = strip_html_comments(self.read_index(project_name)).strip()
        docs = [d for d in self.list_docs(project_name) if d.path != INDEX_FILENAME]
        if index or docs:
            listing = "\n".join(f"- `{d.path}`" for d in docs) or "（暂无文档文件）"
            parts.append(
                "## 可查阅的项目文档\n\n"
                f"目录：`{self.for_project(project_name).docs}`\n\n"
                f"{listing}\n\n"
                + (f"索引：\n\n{index}\n\n" if index else "")
                + "需要时用 Read 工具直接读上面的路径，不要凭猜测。"
            )

        journal = strip_html_comments(self.read_journal(project_name)).strip()
        if journal:
            # 只带最近的一段 —— 变更记录会一直增长，全量塞进去迟早把上下文挤爆
            if len(journal) > max_journal_chars:
                journal = "（只显示最近部分）\n" + journal[-max_journal_chars:]
            parts.append(f"## 最近的变更记录\n\n{journal}")

        parts.append(_task_block(task))
        return "\n\n---\n\n".join(parts)


def _task_block(task: TaskOut) -> str:
    lines = [f"# 任务\n\n{task.title}"]
    if task.body.strip():
        lines.append(f"## 详情\n\n{task.body.strip()}")
    lines.append(f"（类型：{task.kind}）")
    return "\n\n".join(lines)
