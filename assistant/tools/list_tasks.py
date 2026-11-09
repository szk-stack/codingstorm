"""工具：list_tasks —— 看项目最近的任务和状态。"""

import urllib.parse

from toolbox import register
from tools._codingstorm import request, resolve_project

DESCRIPTION = {
    "name": "list_tasks",
    "description": (
        "列出某个项目最近的任务（id、状态、标题）。"
        "用户问「现在有什么在跑」「上一个任务怎么样」时使用；"
        "不要用它轮询刚创建的任务。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "项目名或项目 id；只有一个项目时可以省略",
            },
            "status": {
                "type": "string",
                "description": "可选状态过滤，如 queued / running / awaiting_review / failed",
            },
            "limit": {"type": "integer", "description": "最多返回几条，默认 10"},
        },
        "required": [],
    },
}


def run(arguments):
    try:
        project = resolve_project(arguments.get("project"))
        params = {"project_id": project["id"]}
        status = arguments.get("status")
        if status:
            params["status"] = status
        limit = max(1, min(int(arguments.get("limit") or 10), 50))
        # 主服务的 /api/tasks 只认 project_id / status，条数在本地截断
        tasks = request("GET", "/api/tasks?" + urllib.parse.urlencode(params)) or []
        tasks = tasks[:limit]
        if not tasks:
            return f"项目 {project['name']} 没有符合条件的任务。"
        lines = [f"项目 {project['name']} 最近 {len(tasks)} 条任务："]
        for t in tasks:
            lines.append(f"- id={t['id']}  状态={t['status']}  标题={t['title']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"错误：{exc}"


register(DESCRIPTION, run)
