"""
llm.py —— 负责和 DeepSeek 服务器说话

只用 Python 自带的工具（标准库），不用安装任何东西。
两个出口：
  chat()        —— 普通版：等模型把话说完，一口气返回
  chat_stream() —— 流式版：模型边生成我们边收；而且这次支持"工具调用"了
"""

import json
import urllib.error
import urllib.request

import config


def chat(messages, with_usage=False):
    """把完整的消息列表发给模型，返回 AI 的回答（字符串）。

    messages 的格式像这样：
        [{"role": "user", "content": "你好"},
         {"role": "assistant", "content": "你好呀！"},
         {"role": "user", "content": "再讲个笑话"}]

    with_usage=True 时返回 (回答, usage 原始字典)——
    压缩摘要这类"看不见的调用"也要能记进用量账本（缓存命中率要用）。
    """

    # 1. 检查有没有填 Key，没填就给一句人话提示
    if not config.API_KEY:
        raise RuntimeError(
            "还没有填 API Key！请打开 config.py，把 API_KEY 填进引号里，然后重启服务。"
        )

    # 2. 按照 DeepSeek（OpenAI 兼容格式）的要求拼出请求内容
    body = json.dumps(
        {
            "model": config.MODEL,
            "messages": messages,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        config.API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.API_KEY,
        },
        method="POST",
    )

    # 3. 发出去，拿回答
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))

        content = data["choices"][0]["message"]["content"]
        if with_usage:
            # usage 里有 prompt_cache_hit_tokens / prompt_cache_miss_tokens
            # ——缓存命中率就是从这两个数字来的
            return content, data.get("usage")
        return content

    except urllib.error.HTTPError as e:
        # Key 填错了、余额不足、被限流等等，都会走到这里
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"调用 API 失败（HTTP {e.code}）：{detail}") from e

    except urllib.error.URLError as e:
        # 网络不通（没联网、开了代理等等）
        raise RuntimeError(f"连不上 API 服务器，请检查网络：{e.reason}") from e


def _has_image(messages):
    """这批消息里有没有图片（多模态块）？有的话要用视觉模型。"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def chat_stream(messages, tools=None):
    """流式请求模型，产出三类"事件"（生成器）。

    产出的事件（都是字典，方便上层按 type 分发）：
      {"type": "text", "text": "一小段文字"}      —— 回答的文字增量，来一段吐一段
      {"type": "reasoning", "text": "…"}          —— 模型的思考过程增量
                                                     （不进前端，由引擎记入链路日志）
      {"type": "usage", "usage": {...}}           —— 本次请求的 token 用量
                                                     （最后一包，含缓存命中数）
      {"type": "tool_calls", "tool_calls": [...]} —— 模型要求调用工具（流结束时才产出）

    参数：
      messages —— 对话历史
      tools    —— 工具"说明书"列表（发给模型看的；None 或空表示不带工具）

    ★ 工具调用在流式下是"分片"到达的：
      工具名、参数字符串会被切成好几块、分好几批发过来，
      所以要按序号攒进 pending_calls、拼接完整才能用——
      这是本函数最需要动脑子的几行。
    """

    # 1. 检查有没有填 Key（和 chat() 一样的提示）
    if not config.API_KEY:
        raise RuntimeError(
            "还没有填 API Key！请打开 config.py，把 API_KEY 填进引号里，然后重启服务。"
        )

    # ★ 模型路由：有图片的请求自动换视觉模型（只有视觉模型能接收图片——
    #   官方限制：其他模型收到图片会 400）
    model = config.MODEL
    if getattr(config, "ENABLE_VISION", False) and _has_image(messages):
        model = config.VISION_MODEL

    # 2. 拼请求体：这次多了 "stream"，还可以带上 "tools"
    body_dict = {
        "model": model,
        "messages": messages,
        "stream": True,        # ★ 开启流式（打字机效果的源头）
    }
    if tools:
        body_dict["tools"] = tools   # ★ 带上工具说明书，模型才知道自己能用什么工具

    # ★ 用量回执：流式响应默认**不带** usage，必须显式要——
    #   否则缓存命中数永远拿不到（命中率也就无从算起）
    if getattr(config, "ENABLE_USAGE_STATS", True):
        body_dict["stream_options"] = {"include_usage": True}
    body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        config.API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.API_KEY,
        },
        method="POST",
    )

    # 3. 一行一行地接收 SSE 流
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            pending_calls = {}   # 工具调用"拼接区"：序号 -> {"id", "name", "arguments"}

            while True:
                raw_line = response.readline()
                if not raw_line:
                    break    # 连接结束

                line = raw_line.decode("utf-8").strip()

                # SSE 格式：数据行都以 "data: " 开头（其他行直接跳过）
                if not line.startswith("data: "):
                    continue

                payload = line[len("data: "):]
                if payload == "[DONE]":
                    break    # ★ 官方规定的结束标记

                chunk = json.loads(payload)

                # 情况零：用量回执。它是流的最后一包，**choices 是空数组**——
                #   所以必须先处理它，否则下面取 choices[0] 会下标越界。
                if chunk.get("usage"):
                    yield {"type": "usage", "usage": chunk["usage"]}

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                # 情况一：一段回答文字（就是它让打字机效果一格格动起来）
                piece = delta.get("content")
                if piece:
                    yield {"type": "text", "text": piece}

                # 情况一·补充：模型的"思考过程"（思考模式下才有）
                #   DeepSeek 通过 reasoning_content 下发；不同兼容端点可能叫
                #   reasoning —— 两个都认。引擎只会把它记进链路日志，
                #   不往前端转发（想展示思考过程时再改引擎那一处）。
                reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning_piece:
                    yield {"type": "reasoning", "text": reasoning_piece}

                # 情况二：工具调用的小碎片，按序号拼起来
                for call_piece in delta.get("tool_calls") or []:
                    index = call_piece.get("index", 0)
                    slot = pending_calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if call_piece.get("id"):
                        slot["id"] = call_piece["id"]
                    fn = call_piece.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]   # 参数是一段段拼出来的

            # 4. 流结束：如果攒到了工具调用，作为最后一个事件交出去
            #    （模型可能"先说一句话，再要求调工具"，所以这一步放在最末）
            if pending_calls:
                calls = [pending_calls[i] for i in sorted(pending_calls)]
                yield {"type": "tool_calls", "tool_calls": calls}

    except urllib.error.HTTPError as e:
        # Key 填错了、余额不足、被限流等等
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"调用 API 失败（HTTP {e.code}）：{detail}") from e

    except urllib.error.URLError as e:
        # 网络不通
        raise RuntimeError(f"连不上 API 服务器，请检查网络：{e.reason}") from e
