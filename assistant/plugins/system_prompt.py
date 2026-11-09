"""
插件示例①：在调模型之前，插入"系统提示词"

系统提示词 = 给 AI 定人设 + 定行为规范。内容写在 config.py 的 SYSTEM_PROMPT 里，
由本插件负责拼装并放进对话最前面。

【这里做了个分层（参考 DeepSeek Harness 的做法）】
    稳定部分：身份 + 行为规则 + 知识库规则   ← 几乎不变，写在 config 里
    变动部分：当前工作区、知识库有没有就绪   ← 每轮可能不同，由本插件现拼

为什么要分开？因为这两段的"变化频率"差了几个数量级。
把它们混在一起，只要工作区一动，整段提示词就变了——模型侧的缓存全部作废，
token 白花钱。分开之后，变动的只有最后那十几行。

（同一个思路在 Pi 里是"不做动态时间戳"：它的系统提示词刻意不含日期，
  因为任何动态值出现在前面，都会让后面所有 token 的缓存失效。）
"""

import config
import toolbox
from hooks import before_llm
from pathlib import Path

# 人设段的起始标记。
# 用它判断"这条 system 是不是我们插的"，比比较全文更稳——
# 工作区一变，全文就变了，比全文会被误判成"还没插"，结果插出第二条人设。
PERSONA_MARK = "<persona>"

# 两个约定的文件名（抄 Pi 的做法）
AGENTS_FILE = "AGENTS.md"     # 追加式：项目规矩，叠在人设后面
SYSTEM_FILE = "SYSTEM.md"     # 替换式：整个换掉人设

# 用户级目录：放跨项目通用的规矩
USER_DIR = Path.home() / ".mywheel"


def _find_layered(name, workspace):
    """从外往里找同名文件，返回 [(路径, 内容)]。

    ★ 顺序为什么是"从外到内"：越靠里越具体，应该覆盖外面的通用规则。
      模型读到最后一条时，手里握着的是最具体的那个项目的规矩。
      这和 CSS、.gitignore 的思路是一样的。

    搜索范围：用户级目录 → 项目根 → … → 当前工作区。
    工作区在项目外面时（比如切到了 D 盘某个素材目录），
    就只取"用户级 + 工作区本身"，别把无关的父目录也扫进来。
    """
    found = []
    seen = set()

    def take(p):
        try:
            p = p.resolve()
        except Exception:
            return
        if p in seen or not p.is_file():
            return
        seen.add(p)
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            return
        if text:
            found.append((p, text))

    take(USER_DIR / name)

    root = Path(toolbox.PROJECT_ROOT).resolve()
    ws = Path(workspace).resolve() if workspace else root

    if ws == root or ws.is_relative_to(root):
        # 项目根 → 工作区这条链上，逐层收
        chain = [root]
        try:
            rel = ws.relative_to(root)
            cur = root
            for part in rel.parts:
                cur = cur / part
                chain.append(cur)
        except Exception:
            pass
        for d in chain:
            take(d / name)
    else:
        # 工作区在项目外：只认它自己那一层
        take(ws / name)

    return found


def _persona(workspace):
    """人设段：有 SYSTEM.md 就整份替换，没有就用 config 里的默认人设。"""
    custom = _find_layered(SYSTEM_FILE, workspace)
    if custom:
        path, text = custom[-1]        # 最靠里的那份说了算
        return text, f"（人设来自 {path.name}）"
    return config.SYSTEM_PROMPT.strip(), ""


def _agents_block(workspace):
    """项目规矩：把找到的 AGENTS.md 按从外到内叠起来。"""
    files = _find_layered(AGENTS_FILE, workspace)
    if not files:
        return ""

    lines = ["<project-instructions>"]
    for path, text in files:
        try:
            shown = path.relative_to(Path(toolbox.PROJECT_ROOT))
        except Exception:
            shown = path
        lines.append(f"<!-- 来自 {shown} -->")
        lines.append(text)
        lines.append("")
    lines.append("</project-instructions>")
    return "\n".join(lines)


def _environment_block():
    """现场情况：工作区在哪、知识库是否就绪。放在提示词最末尾。"""
    lines = ["<environment>"]

    workspace = toolbox.current_workspace()
    if workspace:
        lines.append(f"当前工作区：{workspace}")
    else:
        lines.append("当前工作区：项目目录（默认）")

    # 知识库状态：只在开了开关时才提，免得模型去找一个不存在的工具
    if getattr(config, "ENABLE_KB", False):
        try:
            import rag
            info = rag.stats()
            if info:
                lines.append(
                    f"知识库：已就绪（{info['chunks']} 块 / {info['files']} 个文件，"
                    f"可用 search_kb 检索）"
                )
            else:
                lines.append("知识库：尚未建索引（需要先运行 python rag.py build）")
        except Exception:
            # 知识库出任何问题都不该影响正常聊天
            pass

    lines.append("</environment>")
    return "\n".join(lines)


def build_prompt():
    """拼出完整提示词：固定的规矩在前，变动的现场在后。

    四层，从稳到变：
      <persona>              人设（SYSTEM.md 可整份替换）
      <project-instructions> 项目规矩（AGENTS.md，从外到内叠加）
      <environment>          现场（工作区、知识库状态）
    """
    workspace = toolbox.current_workspace()
    persona, persona_note = _persona(workspace)
    agents = _agents_block(workspace)

    parts = [f"{PERSONA_MARK}\n{persona}\n</persona>"]
    if persona_note:
        parts.append(f"<!-- {persona_note} -->")
    if agents:
        parts.append(agents)
    parts.append(_environment_block())

    return "\n\n".join(parts)


def add_system_prompt(ctx):
    """把系统提示词放进对话最前面；已经有了就原地更新。"""
    # 打了"跳过项目人设"标记的场合（比如子代理——它自带专门人设）：不动
    if ctx["meta"].get("skip_persona"):
        return

    messages = ctx["messages"]
    prompt = build_prompt()

    # 已经插过？更新它，别插第二条。
    # 注意：上下文压缩产生的"摘要"也是 system 消息，但它不会以 PERSONA_MARK 开头，
    # 所以不会被误认成人设（这是用标记而不是"第一条是不是 system"的原因）。
    for m in messages:
        if m.get("role") == "system" and str(m.get("content", "")).startswith(PERSONA_MARK):
            if m.get("content") != prompt:
                m["content"] = prompt
            return

    messages.insert(0, {"role": "system", "content": prompt})


# ★ 注册：把函数挂到"调模型前"这个空槽上（功能就这么加上了）
before_llm.append(add_system_prompt)
