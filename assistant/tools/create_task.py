"""工具：create_task —— 把整理好的需求加入 codingstorm 任务队列。

关键：只创建、不等待。返回 task_id 后立刻交给模型回复用户，
调度器会在后台领取执行。
"""

import json

import config
from toolbox import register
from tools._codingstorm import CodingstormError, request, resolve_project

DESCRIPTION = {
    "name": "create_task",
    "description": (
        "在指定项目里创建一条 codingstorm 任务，加入队列后立即返回。"
        "只在需求已经整理清楚时调用，且**只调用一次**。"
        "返回 task_id 和 queued 状态；不要等待执行、不要轮询进度。"
        "参数 body 要写成结构化需求：目标 / 范围 / 验收标准 / 约束与假设。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "项目名或项目 id；只有一个项目时可以省略",
            },
            "title": {
                "type": "string",
                "description": "一句话标题，动词开头，不超过 40 字",
            },
            "body": {
                "type": "string",
                "description": "结构化需求正文：目标 / 范围 / 验收标准 / 约束与假设",
            },
            "kind": {
                "type": "string",
                "enum": ["requirement", "instruction", "bug", "task"],
                "description": "任务类型，默认 requirement",
            },
            "priority": {
                "type": "integer",
                "description": "优先级，默认 0；数字越大越优先",
            },
        },
        "required": ["title", "body"],
    },
}


def run(arguments):
    try:
        project = resolve_project(arguments.get("project"))
        title = str(arguments.get("title") or "").strip()
        body = str(arguments.get("body") or "").strip()
        if not title:
            return "错误：title 不能为空"
        if len(title) > 200:
            return "错误：title 不能超过 200 字"
        if not body:
            return "错误：body 不能为空；请把需求整理成目标 / 范围 / 验收标准 / 约束"
        kind = arguments.get("kind") or "requirement"
        if kind not in ("requirement", "instruction", "bug", "task"):
            kind = "requirement"
        priority = int(arguments.get("priority") or 0)
        task = request(
            "POST",
            f"/api/projects/{project['id']}/tasks",
            {"title": title, "body": body, "kind": kind, "priority": priority},
        )
        base = config.CODINGSTORM_URL.rstrip("/")
        return (
            "任务已加入队列，立即返回，不要等待执行。\n"
            + json.dumps(
                {
                    "task_id": task["id"],
                    "status": task["status"],
                    "title": task["title"],
                    "project": project["name"],
                    "url": f"{base}/#/task/{task['id']}",
                },
                ensure_ascii=False,
            )
        )
    except CodingstormError as exc:
        return f"错误：{exc}"
    except Exception as exc:
        return f"错误：创建任务失败：{exc}"


register(DESCRIPTION, run)
