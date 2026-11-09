"""
engine.py —— 引擎：把整个流程串起来

一次对话的完整流程（升级成了"agent 循环"）：

    before_llm 空槽
        ↓
    [ 调模型（流式）→ 想调工具？→ 执行工具 → 结果塞回历史 → 再调模型 …… ]  ← 循环
                          └─ 不想调了 → 直接回答 → 结束
        ↓
    after_llm 空槽

引擎自己不干具体活儿，只负责"按顺序调度"。
具体功能由插件挂在空槽上、工具登记在 toolbox 注册表里。
"""

import importlib
import json
import pkgutil
import time

import config
import hooks
import llm
import toolbox
import tracing


def chat(messages):
    """处理一次对话，返回 AI 的回答。"""

    # 上下文：在整条流程里传递的"共享数据袋"
    # 三个键的含义：
    #   messages —— 完整对话历史（插件可以修改它）
    #   reply    —— AI 的回答（调用模型之后才有值）
    #   meta     —— 插件之间传递临时数据用（如压缩状态、跳过人设标记）
    ctx = {
        "messages": messages,
        "reply": None,
        "meta": {},
    }

    # ① 调模型之前：执行 before_llm 槽里的所有插件
    hooks.run(hooks.before_llm, ctx)

    # ② 调用 DeepSeek 模型（用的是插件处理过的 messages）
    ctx["reply"] = llm.chat(ctx["messages"])

    # ③ 模型回答之后：执行 after_llm 槽里的所有插件
    hooks.run(hooks.after_llm, ctx)

    # ④ 把最终回答返回出去
    return ctx["reply"]


def _convert_tool_history(messages):
    """把历史里的"工具记录"转换成模型协议格式。

    我们平时把工具记录存成一条 role=tool 的条目（方便前端显示），
    但发给模型时要还原成标准的"成对"格式：
        assistant（带 tool_calls）  +  tool（结果）
    这样模型就能"看到"它之前读过什么文件、执行过什么——
    不用每次都重新读一遍。
    """
    out = []
    for m in messages:
        if m.get("role") == "tool":
            call_id = f"hist_{len(out)}"   # 历史调用的编号（只需在本次请求内成对）
            out.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": m.get("name", ""),
                        "arguments": json.dumps(m.get("arguments") or {}, ensure_ascii=False),
                    },
                }],
            })
            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": m.get("result") or "",
            })
        else:
            out.append(m)
    return out


def _image_data_url(path):
    """把 uploads/xxx.png 读成给模型看的 data URL；文件不存在返回 None。

    安全：路径必须落在项目目录内（和文件工具同一个世界观）。
    """
    import base64

    import envconfig

    # 图片是运行时数据，跟着 MYWHEEL_DATA_DIR 走，不再固定在项目根。
    # 事件日志里存的仍是 "uploads/xxx.png" 这种相对路径，格式没变。
    root = envconfig.data_dir()
    p = (root / path).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(p.suffix.lower())
    if not mime:
        return None
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _convert_images(messages):
    """把消息里的"图片块"转成模型能读的多模态格式。

    我们存储时图片用轻量引用：{"type": "image", "path": "uploads/xx.png"}
    （图片本体在磁盘上，事件日志里只存路径）；
    发给模型前转换为 OpenAI 格式：{"type": "image_url", "image_url": {"url": dataURL}}。

    只处理 user 消息——官方限制：图片放在 system / assistant 消息里会 400。
    """
    out = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            out.append(m)
            continue
        blocks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image" and block.get("path"):
                data_url = _image_data_url(block["path"])
                if data_url:
                    blocks.append({"type": "image_url", "image_url": {"url": data_url}})
                else:
                    blocks.append({"type": "text", "text": "（图片文件缺失，无法加载）"})
            elif block.get("type") == "text":
                blocks.append({"type": "text", "text": block.get("text", "")})
        out.append({"role": "user", "content": blocks})
    return out


def _slim_message(m):
    """给链路快照里的单条消息瘦身：内容 / 工具参数截断，防快照无限膨胀。"""
    out = dict(m)
    content = out.get("content")
    if isinstance(content, str):
        out["content"] = tracing.slim_text(content)
    elif isinstance(content, list):
        # 多模态块数组：文本块截断，图片块保持原样（里面只有轻量路径引用）
        slim_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                slim_blocks.append({**block, "text": tracing.slim_text(block.get("text", ""))})
            elif isinstance(block, dict) and block.get("type") == "image_url":
                # 图片数据（base64）不进链路快照——用占位代替（否则一条 trace 能几 MB）
                slim_blocks.append({"type": "image_url",
                                    "image_url": {"url": "（图片数据已省略）"}})
            else:
                slim_blocks.append(block)
        out["content"] = slim_blocks
    if out.get("tool_calls"):
        slim_calls = []
        for tc in out["tool_calls"]:
            fn = tc.get("function") or {}
            slim_calls.append({
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": tracing.slim_text(fn.get("arguments", ""), 800),
                },
            })
        out["tool_calls"] = slim_calls
    return out


def chat_stream(messages, workspace=None, compaction=None, skip_persona=False,
                conversation_id=None, trace_source="main", full_access=False):
    """流式版对话流程（生成器）。

    参数 workspace：本次对话的工作区目录（None = 默认项目目录）——
    由用户在前端设定、随请求传进来；注意：模型没有权力改它，
    它只是被锁在工作区里干活的人。

    参数 full_access：用户在输入框旁拨的「完整权限」开关——
    True 时文件工具可以读写工作区以外的路径（仍会拒绝盘符根目录和系统目录）。
    同样是**用户**的权力，模型改不了。

    参数 compaction：会话的上下文压缩状态（{"summary", "first_kept_index"} 或 None）
    —— 由前端随请求带来，放进 ctx["meta"] 供 compact 插件使用。

    参数 skip_persona：跳过"项目人设"（默认 False）。
    —— 子代理（spawn_agent 工具）用它：子代理有自己的专门人设
    （config.SUBAGENT_PROMPT），不希望项目人设再混进去。

    参数 conversation_id / trace_source：链路观测用——把这次执行记进
    traces.jsonl（哪个会话、主对话还是子代理）。观测失败不影响主流程。

    ★ 一次调用 = 一个"任务"的完整生命周期（从收到请求到最终回答）。
      每一步都会记进链路日志（tracing）：每轮**实际发给模型的上下文快照**、
      模型的思考过程（reasoning）、工具调用与耗时、最终输出。

    产出的事件（都是字典）：
      {"type": "text", "text": "..."}                           文字增量
      {"type": "tool_call", "name": "...", "arguments": {...}}  开始执行某个工具
      {"type": "tool_result", ...}                              工具执行完毕（带结果）
      {"type": "compacted", ...}                                本轮发生了自动压缩

    （llm 层的 reasoning 事件只进链路日志，不往前端转发。）

    两个从流式改造时延续下来的取舍（没变）：
      - before_llm 照常工作（可以改 messages）
      - after_llm 拿得到完整回答，但改不了已经流出去的内容（只服务记录类用途）
    """
    ctx = {
        "messages": messages,
        "reply": None,
        "meta": {},
    }

    # ★ 链路观测：从任务开始计时，先建一份"空记录"（收尾时统一落盘）
    trace_started = time.time()
    trace = tracing.new_trace(conversation_id, workspace, config.MODEL, trace_source)

    text_pieces = []      # 攒完整回答（给 after_llm 和链路日志用）
    used_rounds = 0       # 已经执行过几轮工具

    try:
        # ★ 第 0 步：先把工作区告诉工具箱——接下来所有工具都被锁在它里面干活
        #   （用"线程本地"记录：多线程服务器里，不同对话的请求互不串台）
        toolbox.set_workspace(workspace)

        # ★ 失败闸清零：每次对话重新开始记账（上一个话题的失败不该连坐）
        toolbox.reset_fail_streak()

        # ★ 完整权限开关（用户在前端拨的，默认关）
        toolbox.set_full_access(full_access)

        # ★ 第 0.5 步：把"压缩状态"放进数据袋——compact 插件用它来
        #   "应用"已有的压缩（切成"摘要 + 最近消息"）
        ctx["meta"]["compaction"] = compaction

        # ★ 第 0.6 步：把"跳过项目人设"标记放进数据袋——
        #   system_prompt 插件看到它就收手（子代理自带人设）
        ctx["meta"]["skip_persona"] = skip_persona

        # ① 调模型之前：before_llm 插件照常工作
        #    （其中 compact 插件可能在这里做"上下文压缩"——它会应用旧压缩，
        #      并在超阈值时生成新摘要，结果记在 ctx["meta"] 里）
        hooks.run(hooks.before_llm, ctx)

        # ★ 顺手记下"旁路调用"的用量：压缩摘要本身就是一次模型调用，
        #   它烧掉的 token（以及有没有命中缓存）也要进账本，
        #   否则命中率会被算低——它可是把一大段对话当输入发出去的。
        for aux in ctx["meta"].get("aux_usage") or []:
            usage = tracing.normalize_usage(aux.get("usage"))
            if not usage:
                continue
            trace.setdefault("aux_usage", []).append({
                "kind": aux.get("kind", "aux"),
                "usage": usage,
            })
            trace["usage"] = tracing.merge_usage(trace.get("usage"), usage)

        # ★ 如果刚才发生了压缩，把这件事告诉前端（记账 + 显示压缩卡片 + 用量回落）
        if ctx["meta"].get("new_compaction"):
            new_c = ctx["meta"]["new_compaction"]
            yield {
                "type": "compacted",
                "summary": new_c["summary"],
                "first_kept_index": new_c["first_kept_index"],
                "tokens_before": ctx["meta"].get("tokens_before_compact", 0),
            }

        # 准备工具"说明书"（发不发给模型，看 config 里的总开关）
        tools_desc = toolbox.all_descriptions() if config.ENABLE_TOOLS else []

        # 工作副本：先把历史里的"工具记录"转换成模型能读懂的标准格式
        # （工具结果从此会跟着上下文发给模型——它不用重复读文件了），
        # 再把图片块转成多模态格式（路径 → base64 data URL），
        # 之后的工具对话也追加在它身上，不动原来的 messages 列表
        work_messages = _convert_images(_convert_tool_history(ctx["messages"]))

        # ② agent 循环
        while True:
            pending_calls = None
            round_started = time.time()
            round_text = ""        # 本轮的文字输出（链路日志用）
            round_reasoning = ""   # 本轮的思考过程（链路日志用）
            round_usage = None     # 本轮的 token 用量（含缓存命中，链路日志用）

            # ★ 链路观测：每轮调模型前，抓一份"实际发送的上下文快照"
            #   ——这是排查问题时的第一现场（压缩后、协议转换后的真实形态）
            request_snapshot = {
                "messages": [_slim_message(m) for m in work_messages],
                "tools": [t["function"]["name"] for t in tools_desc],
                "message_count": len(work_messages),
            }

            # 调一次模型（流式），把文字一段段往外转交
            for event in llm.chat_stream(work_messages, tools=tools_desc):
                if event["type"] == "text":
                    text_pieces.append(event["text"])
                    round_text += event["text"]
                    yield event                     # 文字直接往外流
                elif event["type"] == "reasoning":
                    round_reasoning += event["text"]   # 只进链路日志，不转发
                elif event["type"] == "usage":
                    # ★ 用量回执：累加进本轮账本（随后并入整条链路）
                    round_usage = tracing.add_usage(round_usage, event["usage"])
                elif event["type"] == "tool_calls":
                    pending_calls = event["tool_calls"]

            # 把这一轮的过程记进链路
            round_record = {
                "index": len(trace["rounds"]),
                "request": request_snapshot,
                "reasoning": round_reasoning,
                "text": round_text,
                "tool_calls": [],
                "duration_ms": int((time.time() - round_started) * 1000),
            }
            if round_usage:
                # 本轮用量 + 并进整条链路的账本（一个任务可能有很多轮）
                round_record["usage"] = round_usage
                trace["usage"] = tracing.merge_usage(trace.get("usage"), round_usage)
            trace["rounds"].append(round_record)

            if not pending_calls:
                break    # 模型正常答完了 → 收工

            # 轮次计数：MAX_TOOL_ROUNDS 为 0 = 不限制（默认）——让模型自己
            # 决定什么时候收工，和 Pi / dsh 的做法一致；
            # 配了正数才当保险丝用（防死循环烧钱）。
            # ★ 注意：撞上限是"直接截断"，模型没机会说"我没做完"——
            #   这个行为等闸门方案定了再改（现在默认 0，走不到这里）。
            if config.MAX_TOOL_ROUNDS > 0 and used_rounds >= config.MAX_TOOL_ROUNDS:
                print(f"[警告] 工具调用达到上限（{config.MAX_TOOL_ROUNDS} 轮），停止继续执行")
                break
            used_rounds += 1

            # ③ 按官方协议，把"模型要求调工具"这件事记进工作历史
            #    （OpenAI 兼容格式：assistant 消息带 tool_calls 字段）
            #    ★ 思考模式下带 tools 的请求，reasoning_content 要随 assistant
            #      消息完整回传（官方文档要求，否则后续请求可能 400）；
            #      本轮没有思考内容就不带这个字段
            assistant_msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                    for call in pending_calls
                ],
            }
            if round_reasoning:
                assistant_msg["reasoning_content"] = round_reasoning
            work_messages.append(assistant_msg)

            # 逐个执行这次要求的工具（模型可能一次要调好几个）
            for call in pending_calls:
                # 模型给的参数是 JSON 字符串，解析成字典
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                # 告诉外面："要开始执行工具了"（前端用来显示状态）
                yield {"type": "tool_call", "name": call["name"], "arguments": arguments}

                call_started = time.time()
                result = toolbox.call(call["name"], arguments)   # ★ 真正执行！
                call_ms = int((time.time() - call_started) * 1000)

                # 取走这次工具"捎带的附件"（比如子代理的完整对话记录）——
                # 它会跟着 tool_result 一起流向对前端和日志
                extra = toolbox.take_attachments()

                # ★ 工具"递图"给模型（view_image 专用）：附件里的图片数据
                #   不能走 tool 消息（官方限制：图片只能放 user 消息），
                #   先摘出来——下面回填历史时以一条 user 消息递回模型；
                #   同时从 extra 里 pop 掉（base64 不进事件日志和链路快照）
                image_for_model = None
                if isinstance(extra, dict) and extra.get("image_for_model"):
                    image_for_model = extra.pop("image_for_model")

                # ★ 链路观测：工具调用（参数 / 结果 / 耗时）
                round_record["tool_calls"].append({
                    "name": call["name"],
                    "arguments": arguments,
                    "ok": not result.startswith("错误"),
                    "result": tracing.slim_text(result, 2000),
                    "duration_ms": call_ms,
                })

                # 告诉外面："执行完了"（前端用来显示结果、并记账）
                event = {
                    "type": "tool_result",
                    "name": call["name"],
                    "arguments": arguments,
                    "ok": not result.startswith("错误"),
                    "result": result,
                }
                if extra:
                    event["extra"] = extra
                yield event

                # 按官方协议把结果塞回工作历史（role=tool + 对应的 tool_call_id）
                work_messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

                # ★ 图片递回：以一条 user 消息把图片交给模型（官方限制：
                #   图片只能放 user 消息）。模型下一轮就能"看到"这张图。
                if image_for_model:
                    pic_path = arguments.get("path", "")
                    work_messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text",
                             "text": f"（这是工具 {call['name']} 读到的图片：{pic_path}）"},
                            {"type": "image_url",
                             "image_url": {"url": image_for_model}},
                        ],
                    })

        # ④ 循环结束：把完整回答交给 after_llm（记录类用途，前面解释过）
        ctx["reply"] = "".join(text_pieces)
        try:
            hooks.run(hooks.after_llm, ctx)
        except Exception as e:
            # after_llm 插件出错不影响已经发出的内容，打个警告就好
            print(f"[警告] after_llm 插件出错：{e}")

        trace["status"] = "ok"
        trace["final_text"] = ctx["reply"]

    except Exception as e:
        # 出错也要留痕：链路日志是排查问题最有用的第一手材料
        trace["status"] = "error"
        trace["error"] = str(e)
        trace["final_text"] = "".join(text_pieces)   # 已经流出去的部分
        raise
    finally:
        # 生成器被提前关闭（用户断开 / 页面关掉）也会走到这里
        if trace["status"] == "running":
            trace["status"] = "interrupted"
            trace["final_text"] = "".join(text_pieces)
        trace["duration_ms"] = int((time.time() - trace_started) * 1000)
        tracing.save_safe(trace)   # 观测失败不能影响主流程


def load_plugins():
    """自动加载 plugins/ 文件夹里的所有插件。

    原理：把 plugins/ 里每个 .py 文件都"执行"一遍。
    插件文件里那行 append 代码会随之执行，函数就自动挂到空槽上了。

    所以以后加功能 = 往 plugins/ 扔一个文件，别的都不用管。
    """
    import plugins

    for info in pkgutil.iter_modules(plugins.__path__):
        if info.name.startswith("_"):  # 跳过 __init__ 之类的内部文件
            continue
        importlib.import_module(f"plugins.{info.name}")
        print(f"[插件] 已加载：{info.name}")
