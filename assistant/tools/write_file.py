"""
工具：write_file —— 创建新文件，或整体覆盖已有文件

注意：它会替换文件的全部内容！只改文件里的几处地方请用 edit_file，
那样更安全（不会因为"重新生成整个文件"而在别处翻车）。
"""

from toolbox import register
from tools._common import safe_path

# ---- ① 工具说明书 ----
DESCRIPTION = {
    "name": "write_file",
    "description": "创建新文件或整体覆盖已有文件。只修改文件中几处内容时，请改用 edit_file（更安全）。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    },
}


# ---- ② 执行函数 ----
def run(arguments):
    target = safe_path(arguments["path"])
    content = arguments.get("content", "")

    existed = target.exists()

    # 目标目录不存在就顺手建出来（比如写 "abc/def.txt" 时 abc 还不存在）
    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text(content, encoding="utf-8")

    action = "覆盖" if existed else "创建"
    line_count = content.count("\n") + 1
    return f"已{action}文件 {arguments['path']}（{line_count} 行，{len(content)} 字符）"


# ---- ③ 登记进注册表 ----
register(DESCRIPTION, run)
