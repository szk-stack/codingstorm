"""
工具：edit_file —— 对文件做"查找替换"式的精确修改（学 Pi 的 edit 工具）

★ 安全设计（这是整个工具最重要的地方）：
  要替换的原文（old_text）必须在文件里【恰好出现一次】。
    - 出现 0 次 → 报错（原文记错了，或者文件被改过）
    - 出现多次 → 报错（定位不唯一，要求带上更多上下文再来）
  这样模型就不可能"改错地方"——比整个文件重写安全得多。
"""

from toolbox import register
from tools._common import safe_path

# ---- ① 工具说明书 ----
DESCRIPTION = {
    "name": "edit_file",
    "description": (
        "修改已有文件：把 old_text 替换成 new_text。"
        "old_text 必须和文件里的内容一模一样、且在文件中只出现一次，否则会报错让你重试。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
            "old_text": {
                "type": "string",
                "description": "要被替换的原文（必须和文件里完全一致，且只出现一次）",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的新内容（可以是空字符串，表示删掉这一段）",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
}


# ---- ② 执行函数 ----
def run(arguments):
    target = safe_path(arguments["path"])
    old_text = arguments["old_text"]
    new_text = arguments["new_text"]

    if not target.exists():
        return f"错误：文件不存在：{arguments['path']}"
    if not target.is_file():
        return f"错误：这不是一个文件：{arguments['path']}"

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"错误：读取失败：{e}"

    # ★ 唯一性检查（0 次 / 多次都不行）
    count = text.count(old_text)
    if count == 0:
        return "错误：在文件里没找到 old_text。请先用 read_file 看一遍真实内容，确保原文一字不差。"
    if count > 1:
        return (
            f"错误：old_text 在文件里出现了 {count} 次，无法确定要改哪一处。"
            "请把 old_text 换成更长的一段（上下文多带几行）让它只出现一次。"
        )

    # 替换（第 3 个参数 1 表示只替换第一处——反正全文只有一处）
    text = text.replace(old_text, new_text, 1)
    target.write_text(text, encoding="utf-8")

    return f"修改成功：{arguments['path']} 里替换了 1 处内容。"


# ---- ③ 登记进注册表 ----
register(DESCRIPTION, run)
