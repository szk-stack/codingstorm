"""
工具：view_image —— 读取工作区里的一张图片，"亲眼"看一下

模型平时只能读文本；这个工具把图片交给它看。

实现上有一个讲究：官方限制"**图片只能放在 user 消息里**"（tool 消息带图会 400），
所以图片不走工具结果的文本，而是挂到"附件槽"上——引擎会在工具执行后
把图片作为一条 user 消息递回给模型（见 engine.py 的回填处）。

典型用法（自检闭环）：
    生成页面 → screenshot 截图 → view_image 看效果 → 发现问题 → 修 → 再截
"""

import base64

import config
import toolbox
from toolbox import register
from tools._common import safe_path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# ---- ① 工具说明书（会发给模型看）----
DESCRIPTION = {
    "name": "view_image",
    "description": (
        "读取工作区里的一张图片，让模型看到图片内容（支持 PNG/JPEG/GIF/WebP）。"
        "适合自检：用 screenshot 截图后查看页面效果，或查看工作区里的图片。"
        f"单张上限 {config.MAX_IMAGE_MB} MB。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对工作区的图片路径（如 .screenshots/shot-120000.png）",
            },
        },
        "required": ["path"],
    },
}


# ---- ② 执行函数 ----
def run(arguments):
    path = (arguments.get("path") or "").strip()
    if not path:
        return "错误：path 不能为空"

    try:
        target = safe_path(path)
    except ValueError as e:
        return f"错误：{e}"

    if not target.exists():
        return f"错误：图片不存在：{path}"
    if not target.is_file():
        return f"错误：这不是一个文件：{path}"

    suffix = target.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return f"错误：不支持的图片格式（支持 {'、'.join(sorted(IMAGE_SUFFIXES))}）"

    raw = target.read_bytes()
    limit = config.MAX_IMAGE_MB * 1024 * 1024
    if len(raw) > limit:
        return f"错误：图片太大（{len(raw) // 1024 // 1024 + 1} MB，上限 {config.MAX_IMAGE_MB} MB）"

    data_url = f"data:{MIME_BY_SUFFIX[suffix]};base64," + base64.b64encode(raw).decode()

    # 把图片挂到附件槽：引擎会把它作为一条 user 消息递给模型
    # （官方限制：图片只能放 user 消息，tool 消息里不行）
    toolbox.attach({"image_for_model": data_url})

    return f"已加载图片 {path}（{len(raw) // 1024} KB）。图片内容会随下一条消息一起呈现给你。"


# ---- ③ 登记进注册表 ----
# 看图依赖视觉能力（没有视觉模型就没意义），跟 ENABLE_VISION 一起开关
if config.ENABLE_VISION:
    register(DESCRIPTION, run)
