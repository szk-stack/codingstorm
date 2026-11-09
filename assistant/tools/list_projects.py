"""工具：list_projects —— 看 codingstorm 里有哪些项目。"""

from toolbox import register
from tools._codingstorm import list_projects

DESCRIPTION = {
    "name": "list_projects",
    "description": (
        "列出 codingstorm 里已注册的项目（id、名字、主干分支、执行器）。"
        "用户没指定项目、或你不确定项目名时先调用它。"
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def run(arguments):
    try:
        projects = list_projects()
    except Exception as exc:
        return f"错误：{exc}"
    if not projects:
        return "错误：还没有注册任何项目。请先在任务队列页注册一个项目。"
    lines = ["现有项目："]
    for p in projects:
        lines.append(
            f"- id={p['id']}  名字={p['name']}  主干={p.get('target_branch', 'main')} "
            f"执行器={p.get('runtime') or '跟随全局'}"
        )
    return "\n".join(lines)


register(DESCRIPTION, run)
