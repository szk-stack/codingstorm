"""
工具：list_dir —— 列出目录里的文件和子目录

第一个、也是最简单的工具。模型想知道"项目里有什么"时会用它。
"""

from toolbox import register
from tools._common import safe_path

# ---- ① 工具说明书（会发给模型看）----
DESCRIPTION = {
    "name": "list_dir",
    "description": "列出项目里某个目录的内容（文件和子目录）。不传 path 就列项目根目录。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对项目根目录的目录路径，比如 'static'；不传则列根目录",
            }
        },
        "required": [],
    },
}


# ---- ② 执行函数（模型点名时真正运行）----
def run(arguments):
    target = safe_path(arguments.get("path", ""))

    if not target.exists():
        return f"错误：目录不存在：{arguments.get('path')}"
    if not target.is_dir():
        return f"错误：这不是一个目录：{arguments.get('path')}"

    lines = []
    for entry in sorted(target.iterdir()):
        name = entry.name
        # 跳过隐藏项（.git、.idea、.workbuddy……）和缓存目录，
        # 免得模型在垃圾堆里迷路（这个做法学自 Pi 的 ls 工具）
        if name.startswith(".") or name == "__pycache__":
            continue
        # 目录加个 "/" 后缀，是程序员最熟悉的标注方式
        lines.append(name + "/" if entry.is_dir() else name)

    if not lines:
        return "（这个目录是空的）"

    return f"目录 {arguments.get('path') or '.'} 的内容：\n" + "\n".join(lines)


# ---- ③ 登记进注册表 ----
register(DESCRIPTION, run)
