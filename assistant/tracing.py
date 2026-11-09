"""
tracing.py —— 链路观测（"飞行记录仪"）

把"一次任务"的完整执行过程记下来：一次任务 = 用户发一条消息触发的一整轮
agent 执行（可能包含多轮"调模型 ⇄ 执行工具"）。每条链路记录：

  - 每轮**实际发给模型的上下文结构**（请求快照）
  - 模型的**思考过程**（reasoning）
  - 模型的文字输出与**工具调用**（参数 / 结果 / 耗时）
  - 每轮的 **token 用量与缓存命中**（命中率 = 命中 ÷（命中 + 未命中））
  - 最终回答、错误、总耗时

存进 traces.jsonl（一行一条，追加写）。

为什么单独一个文件、不用事件日志？
  事件日志（data.jsonl）记的是"对话数据"——它要参与回放、是状态来源；
  链路是"运行观测"——体积大、只管查询、丢了不心疼。分开存互不拖累。
  traces.jsonl 已在 .gitignore 里（可能含敏感内容）。
"""

import json
import time
from datetime import datetime
from pathlib import Path

import envconfig

# 链路日志文件（JSONL 格式：一行一条链路）
# 和 data.jsonl 一样走数据目录；仍是模块级常量，测试里 patch TRACE_FILE 照常有效。
TRACE_FILE = envconfig.data_dir() / "traces.jsonl"

# 上下文快照里单条消息的字符上限（"完整结构"也要有个防爆上限）
MAX_MESSAGE_CHARS = 4000


def new_trace(conversation_id, workspace, model, source="main"):
    """开始记录一条链路：返回一个待填充的 trace 字典。

    source: "main"（主对话）或 "subagent"（子代理内部链路）
    """
    return {
        "id": f"t{time.time_ns()}",
        "conversation_id": conversation_id,
        "workspace": workspace,
        "model": model,
        "source": source,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "duration_ms": 0,
        "status": "running",     # running / ok / error / interrupted
        "rounds": [],
        "final_text": "",
        "error": None,
    }


def slim_text(text, limit=MAX_MESSAGE_CHARS):
    """截断过长文本并注明原文长度（上下文快照防爆用）。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，原文 {len(text)} 字符）"


def save(trace):
    """把一条链路追加进 traces.jsonl（一行一条）。"""
    trace["finished_at"] = datetime.now().isoformat(timespec="seconds")
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")


def save_safe(trace):
    """安全落盘：观测本身失败不能影响主流程（只打警告）。"""
    try:
        save(trace)
    except Exception as e:
        print(f"[警告] 链路日志写入失败：{e}")


# ------------------------------------------------------------
# 用量账本：token 花了多少、其中多少命中了前缀缓存
# ------------------------------------------------------------
#
# 为什么要单独一套字段名：
#   各家兼容端点的叫法不一样——DeepSeek 是 prompt_cache_hit_tokens /
#   prompt_cache_miss_tokens；OpenAI 风格是 prompt_tokens_details.cached_tokens；
#   Anthropic 风格是 cache_read_input_tokens。这里统一成一套内部格式，
#   上层（引擎 / 接口 / 前端）只认内部这套。
#
# 命中率的口径（很重要，别算错）：
#   命中率 = 命中 token ÷ (命中 + 未命中)，**只统计"服务端报了缓存信息"的请求**。
#   没报缓存的端点，不能算成 0%——那会把整体数字拉低成假的。

_NUMERIC_USAGE_KEYS = (
    "requests", "cache_reported_requests",
    "prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "completion_tokens", "total_tokens",
)


def _num(value):
    """尽量把值变成非负整数（None / 脏数据都当 0）。"""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def empty_usage():
    """一份"零用量"账本（累加的起点）。"""
    return {k: 0 for k in _NUMERIC_USAGE_KEYS}


def normalize_usage(raw):
    """把一次请求的原始 usage 转成内部格式；不是字典就返回 None。

    内部格式（键固定，方便累加）：
      requests / cache_reported_requests
      prompt_tokens / cache_hit_tokens / cache_miss_tokens
      completion_tokens / total_tokens
    """
    if not isinstance(raw, dict) or not raw:
        return None

    details = raw.get("prompt_tokens_details") or {}
    prompt = _num(raw.get("prompt_tokens"))
    completion = _num(raw.get("completion_tokens"))
    total = _num(raw.get("total_tokens")) or (prompt + completion)

    # 缓存命中：三种叫法都认
    hit_raw = raw.get("prompt_cache_hit_tokens")
    if hit_raw is None:
        hit_raw = raw.get("cache_read_input_tokens")
    if hit_raw is None:
        hit_raw = details.get("cached_tokens")

    miss_raw = raw.get("prompt_cache_miss_tokens")
    if miss_raw is None:
        miss_raw = details.get("miss_tokens")

    reported = hit_raw is not None or miss_raw is not None
    if hit_raw is not None:
        hit = _num(hit_raw)
        miss = _num(miss_raw) if miss_raw is not None else max(prompt - hit, 0)
    elif miss_raw is not None:
        miss = _num(miss_raw)
        hit = max(prompt - miss, 0)
    else:
        hit = miss = 0

    u = empty_usage()
    u.update({
        "requests": 1,
        "cache_reported_requests": 1 if reported else 0,
        "prompt_tokens": prompt,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "completion_tokens": completion,
        "total_tokens": total,
    })
    return u


def merge_usage(acc, usage):
    """把一份用量累加进账本（acc 为 None 时新建）。返回账本。"""
    if not isinstance(usage, dict):
        return acc
    if acc is None:
        acc = empty_usage()
    for key in _NUMERIC_USAGE_KEYS:
        acc[key] = _num(acc.get(key)) + _num(usage.get(key))
    return acc


def add_usage(acc, raw):
    """把一次原始 usage 归一化后累加进账本。"""
    return merge_usage(acc, normalize_usage(raw))


def hit_rate(usage):
    """命中率（百分数，保留一位小数）；没有缓存数据时返回 None。"""
    if not isinstance(usage, dict):
        return None
    hit = _num(usage.get("cache_hit_tokens"))
    miss = _num(usage.get("cache_miss_tokens"))
    if hit + miss <= 0:
        return None
    return round(hit * 100.0 / (hit + miss), 1)


def _with_rate(usage):
    """账本副本 + 命中率（给接口/前端直接用，省得两边各算一遍）。"""
    out = dict(usage)
    out["hit_rate"] = hit_rate(usage)
    return out


def _coerce_usage(u):
    """兼容"原始 usage"和"已归一化的账本"两种形态。"""
    if not isinstance(u, dict) or not u:
        return None
    if "cache_hit_tokens" in u or "cache_miss_tokens" in u:
        return merge_usage(None, u)
    return normalize_usage(u)


def _usage_brief(u):
    """链路列表里用的小结（只留前端要显示的几项）。"""
    u = _coerce_usage(u)
    if not u:
        return None
    return {
        "requests": u["requests"],
        "prompt_tokens": u["prompt_tokens"],
        "cache_hit_tokens": u["cache_hit_tokens"],
        "cache_miss_tokens": u["cache_miss_tokens"],
        "completion_tokens": u["completion_tokens"],
        "hit_rate": hit_rate(u),
    }


def _decorate(trace):
    """给链路副本里的账本补上算好的命中率（落盘数据不动，只动显示用的副本）。"""
    out = dict(trace)

    u = _coerce_usage(out.get("usage"))
    if u:
        out["usage"] = _with_rate(u)

    rounds = []
    for r in trace.get("rounds") or []:
        r2 = dict(r)
        ru = _coerce_usage(r2.get("usage"))
        if ru:
            r2["usage"] = _with_rate(ru)
        rounds.append(r2)
    out["rounds"] = rounds
    return out


def usage_stats(days=14, recent=12):
    """汇总所有链路里的用量，给「缓存命中」面板用。

    返回：
      totals  总账本（含 hit_rate）
      series  按天聚合（最近 days 天，日期升序，画柱状图用）
      recent  最近 recent 条任务的用量（新的在前，画列表用）
    """
    total = None
    daily = {}
    rows = []

    for t in _read_all():
        u = _coerce_usage(t.get("usage"))
        if not u:
            continue

        total = merge_usage(total, u)

        day = (t.get("started_at") or "")[:10]
        if day:
            daily[day] = merge_usage(daily.get(day), u)

        rows.append({
            "id": t.get("id"),
            "started_at": t.get("started_at"),
            "source": t.get("source", "main"),
            "status": t.get("status"),
            "user_preview": _first_user_text(t),
            "usage": _with_rate(u),
        })

    series = []
    for day in sorted(daily)[-days:]:
        series.append({"date": day, **_with_rate(daily[day])})

    return {
        "totals": _with_rate(total) if total else _with_rate(empty_usage()),
        "series": series,
        "recent": rows[-recent:][::-1],
    }


def list_traces(limit=50):
    """列出最近的链路摘要（不带完整上下文快照，给列表页用）。"""
    items = _read_all()
    items.reverse()          # 最新的在前
    result = []
    for t in items[:limit]:
        result.append({
            "id": t.get("id"),
            "conversation_id": t.get("conversation_id"),
            "source": t.get("source", "main"),
            "started_at": t.get("started_at"),
            "duration_ms": t.get("duration_ms"),
            "status": t.get("status"),
            "rounds": len(t.get("rounds") or []),
            "user_preview": _first_user_text(t),
            "final_preview": (t.get("final_text") or "")[:80],
            "usage": _usage_brief(t.get("usage")),
        })
    return result


def get_trace(trace_id):
    """按 id 取一条完整链路；找不到返回 None。

    取出时顺手把命中率算好（usage / 每一轮都补上 hit_rate）——
    前端只管显示，不用自己再算一遍、也不会因为漏算而显示"—"。
    """
    for t in _read_all():
        if t.get("id") == trace_id:
            return _decorate(t)
    return None


# ---------- 内部小工具 ----------

def _read_all():
    """读出日志里的所有链路（坏行跳过——和事件日志一样的容错哲学）。"""
    if not TRACE_FILE.exists():
        return []
    items = []
    with open(TRACE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _first_user_text(trace):
    """取本次任务里的第一条用户消息（列表摘要用）。"""
    for r in trace.get("rounds") or []:
        for m in (r.get("request") or {}).get("messages") or []:
            if m.get("role") == "user":
                return (m.get("content") or "")[:80]
    return ""
