"""
工具：read_file —— 读文件内容

两个设计点（都学自 Pi 的 read 工具）：
  1. 可以按行号范围读（offset/limit）——大文件不用一口吞
  2. 默认带上行号输出——模型引用代码时方便说"第 42 行"
另外有截断保护：一次最多返回 MAX_CHARS 个字符，防止把模型的上下文塞爆。
"""

from toolbox import register
from tools._common import safe_path

# 一次最多返回多少字符（大约 2000~4000 个汉字，够模型理解一段代码了）
MAX_CHARS = 8000

# ---- ① 工具说明书 ----
DESCRIPTION = {
    "name": "read_file",
    "description": (
        f"读取项目里的一个文本文件，结果带行号。"
        f"内容过长会自动截断（一次最多 {MAX_CHARS} 字符），可用 offset/limit 分段读。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对项目根目录的文件路径"},
            "offset": {"type": "integer", "description": "从第几行开始读（行号从 1 数起），默认从第 1 行"},
            "limit": {"type": "integer", "description": "最多读多少行，默认读到文件结尾"},
        },
        "required": ["path"],
    },
}


# ---- ② 执行函数 ----
def run(arguments):
    target = safe_path(arguments.get("path", ""))

    if not target.exists():
        return f"错误：文件不存在：{arguments.get('path')}"
    if not target.is_file():
        return f"错误：这不是一个文件：{arguments.get('path')}"

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"错误：{arguments.get('path')} 不是文本文件（按 UTF-8 解码失败）"
    except Exception as e:
        return f"错误：读取失败：{e}"

    all_lines = text.splitlines()
    total = len(all_lines)

    # 行号范围切片（offset 从 1 数起，更符合人的直觉）
    offset = int(arguments.get("offset") or 1)
    limit = arguments.get("limit")
    if limit is not None:
        selected = all_lines[offset - 1: offset - 1 + int(limit)]
    else:
        selected = all_lines[offset - 1:]

    if not selected:
        return f"（没有内容：文件共 {total} 行，你请求的是从第 {offset} 行开始）"

    # 每行前面带上行号，比如 "  42 | print('hi')"
    numbered = []
    for i, line in enumerate(selected):
        numbered.append(f"{offset + i:>4} | {line}")
    result = "\n".join(numbered)

    # 截断保护
    note = ""
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS]
        note = f"\n…（太长已截断。文件共 {total} 行，可以用 offset/limit 分段读）"

    return (
        f"文件：{arguments.get('path')}（从第 {offset} 行起，共 {total} 行）\n"
        f"{result}{note}"
    )


# ---- ③ 登记进注册表 ----
register(DESCRIPTION, run)
