"""工具：get_project_context —— 读项目上下文摘要（只读）。"""

from toolbox import register
from tools._codingstorm import request, resolve_project

DESCRIPTION = {
    "name": "get_project_context",
    "description": (
        "读取一个项目的上下文摘要：指针图、变更记录、文档清单。"
        "在需要了解项目背景/约定、或判断需求该放哪个项目时使用。"
        "只读，不会修改任何文件。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "项目名或项目 id；只有一个项目时可以省略",
            },
        },
        "required": [],
    },
}


def _cut(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def run(arguments):
    try:
        project = resolve_project(arguments.get("project"))
        ctx = request("GET", f"/api/projects/{project['id']}/context")
        docs = ctx.get("docs") or []
        doc_lines = [f"- {d.get('path')}" for d in docs[:20]]
        return (
            f"项目：{project['name']}（id={project['id']}，主干={project.get('target_branch')}）\n\n"
            f"## 指针图\n{_cut(ctx.get('pointer'), 3000)}\n\n"
            f"## 变更记录\n{_cut(ctx.get('journal'), 2000)}\n\n"
            f"## 文档清单\n" + ("\n".join(doc_lines) if doc_lines else "（暂无文档）")
        )
    except Exception as exc:
        return f"错误：{exc}"


register(DESCRIPTION, run)
