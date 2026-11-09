"""
插件：compact —— 上下文压缩（跟 Pi 学的 compaction）

聊得越来越久时，"要发给模型的上下文"会越来越大——接近模型窗口
（deepseek-flash 是 1M token）时就会变慢、甚至超出报错。
这个插件在每次"调模型之前"自动检查：

  ① 先按"上次的压缩状态"收拾上下文（只发：摘要 + 最近若干条）
  ② 估算还要花多少 token——如果超过窗口的 70%：
  ③ 把"旧摘要 + 这段时间新聊的"再交给模型总结成一段新摘要（覆盖式全量摘要），
     然后只保留更靠后的最近消息——上下文瞬间"瘦身"！

★ 和事件日志的哲学一致：**原始消息一条不删**——
  压缩只是"追加一条记录"，历史永远可查。
"""

import json
import math

import config
import llm
import toolbox
from hooks import before_llm


# ------------------------------------------------------------
# 工具函数：估算一组消息的 token 用量
# （和前端 index.html 里的 estimateTokens 保持同一套公式）
# 汉字约 0.6 token/字，其他字符约 0.25 token/个，每条消息再 +4
# ------------------------------------------------------------

def message_text(m):
    """取一条消息的"文本部分"（content 可能是字符串，也可能是多模态块数组）。"""
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[图片]")
        return "".join(parts)
    return ""


def message_image_count(m):
    """一条消息里有几张图片（估算 token 用）。"""
    content = m.get("content")
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")


def estimate_tokens(messages):
    total = 0
    for m in messages:
        if m.get("role") == "tool":
            # 工具条目的正文在 result 里（参数也占一点），而且它发给模型时
            # 会展开成"两条消息"（assistant 壳 + tool 结果）——这里如实算
            # （紧凑分隔符：和前端 JS 的 JSON.stringify 输出对齐，保证两边同值）
            args_json = json.dumps(m.get("arguments") or {}, ensure_ascii=False, separators=(",", ":"))
            text = (m.get("result") or "") + args_json
            extra = 4
        else:
            text = message_text(m)
            extra = 0
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        # 向上取整（和前端 JS 的 Math.ceil 对齐，保证两边算出的数字一致）
        total += math.ceil(cjk * 0.6 + other * 0.25) + 4 + extra
        # 图片按官方上限估算：每张最多约 384 token（取 400 保守值，和前端一致）
        total += message_image_count(m) * 400
    return total


def baseline_tokens():
    """每次请求都要占用的"基线"窗口空间：系统人设 + 工具说明书。

    ★ 概念上说清楚（这里很容易混）：
      这是"上下文用量"（窗口占用）的固定部分——不是"本次花了多少钱"。
      它每次请求都占在窗口里（也会被计费，但有前缀缓存在，很便宜）。
      估算/触发判断都要算上它；前端用量条的数字也来自这里（单一来源）。
    """
    fixed = estimate_tokens([{"role": "system", "content": config.SYSTEM_PROMPT}])
    if config.ENABLE_TOOLS:
        fixed += estimate_tokens([{
            "role": "system",
            "content": json.dumps(toolbox.all_descriptions(), ensure_ascii=False),
        }])
    return fixed


# 让模型做摘要时的"工作要求"
SUMMARY_PROMPT = (
    "你是一个对话摘要助手。请把下面的对话压缩成一段简洁的摘要（用简体中文），必须保留：\n"
    "1) 用户的目标、需求和背景信息\n"
    "2) 已经得出的结论和重要事实\n"
    "3) 未完成的任务和下一步计划\n"
    "4) 双方约定过的偏好或规则\n"
    "去掉寒暄和重复内容。直接输出摘要正文，不要任何解释。"
)


# ------------------------------------------------------------
# 两个核心动作：生成压缩 / 应用压缩
# ------------------------------------------------------------

def make_compaction(messages, old=None):
    """生成一次压缩：把"要吸收的消息"交给模型总结。

    参数：
      messages —— 完整的历史消息列表
      old      —— 上次的压缩状态 {"summary", "first_kept_index"}（没有就传 None）

    返回 {"summary", "first_kept_index"}；消息太少不值得压缩时返回 None。

    （这个函数也被"手动压缩"接口复用——界面上点"立即压缩"走的就是它）
    """
    keep = config.KEEP_RECENT_MESSAGES
    if len(messages) <= keep + 2:
        return None

    # 新的"保留边界"：至少保住最近 keep 条，而且只会向前推进、不会倒退
    old_first = old.get("first_kept_index", 0) if old else 0
    new_first = max(old_first, len(messages) - keep)

    # 边界微调：让保留段从"用户消息"开头（别把一问一答切成两半）
    while new_first > 0 and messages[new_first].get("role") != "user":
        new_first -= 1

    # 这次要吸收的"增量" = 旧边界到新边界之间的消息
    to_absorb = messages[old_first:new_first]
    if not to_absorb:
        return None

    # 组装给模型看的摘要素材：旧摘要 + 本次吸收的对话
    parts = []
    if old and old.get("summary"):
        parts.append("【之前的对话摘要（请合并进新摘要）】\n" + old["summary"])
    lines = ["【需要压缩的对话记录】"]
    for m in to_absorb:
        if m.get("role") == "system":
            continue
        if m.get("role") == "tool":
            # 工具记录也压进摘要（截前 500 字符，避免素材太长）
            lines.append(f"（工具 {m.get('name', '')} 的结果）{str(m.get('result', ''))[:500]}")
            continue
        speaker = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{speaker}：{message_text(m)}")   # 图片在素材里以"[图片]"占位
    parts.append("\n".join(lines))

    # ★ 摘要本身也是一次模型调用：把 usage 一起带出来，
    #   交给引擎记进用量账本（不然这部分 token 会从统计里凭空消失）
    summary, usage = llm.chat([
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ], with_usage=True)

    result = {"summary": summary.strip(), "first_kept_index": new_first}
    if usage:
        result["usage"] = usage
    return result


def apply_compaction(messages, compaction):
    """把压缩状态"应用"到消息列表：变成 [摘要] + [保留段]。

    发给模型的最终上下文就是这么构造出来的；
    原始消息不动（它们安安静静躺在事件日志里，随时可回溯）。
    """
    if not compaction:
        return messages

    first_kept = compaction.get("first_kept_index", 0)
    summary = compaction.get("summary") or ""
    tail = messages[first_kept:]

    result = []
    if summary:
        result.append({"role": "system", "content": "【之前的对话摘要】\n" + summary})
    result.extend(tail)
    return result


# ------------------------------------------------------------
# 插件本体：每次"调模型之前"跑一遍
# ------------------------------------------------------------

def compact_plugin(ctx):
    """执行流程：
       ① 按旧压缩状态收拾好有效上下文（这一步每次都要做）
       ② 估算用量——超阈值就再压一次
          （压缩结果记在 ctx["meta"] 里，交给引擎转告前端）
    """
    if not config.ENABLE_COMPACT:
        return

    messages = ctx["messages"]
    old = ctx["meta"].get("compaction")   # 前端随请求带来的旧压缩状态

    # ① 先按旧压缩"应用"，得到真正要发给模型的有效上下文
    effective = apply_compaction(messages, old)

    # ② 超阈值？→ 生成新压缩（估算里含"固定基线"：人设 + 工具说明书）
    limit = int(config.CONTEXT_WINDOW * config.COMPACT_THRESHOLD_RATIO)
    used = estimate_tokens(effective) + baseline_tokens()
    if used > limit:
        new = make_compaction(messages, old)
        if new:
            effective = apply_compaction(messages, new)
            # 把"压缩这件事"记在上下文中，引擎会转告前端：
            # 前端据此记账（compact 事件）+ 显示压缩卡片 + 用量条回落
            ctx["meta"]["new_compaction"] = new
            ctx["meta"]["tokens_before_compact"] = used
            # 摘要这次调用的用量，交给引擎记进"旁路调用"账本
            if new.get("usage"):
                ctx["meta"].setdefault("aux_usage", []).append(
                    {"kind": "compact", "usage": new["usage"]}
                )

    # ★ 替换 ctx 里的消息——后续引擎就用这份"瘦身过的"上下文去调模型
    ctx["messages"] = effective


# ★ 注册：挂到"调模型前"这个空槽上（空槽机制的第一个"生产级"应用）
before_llm.append(compact_plugin)
