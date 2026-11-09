"""
config.example.py —— 配置模板

用法：把本文件复制一份、去掉文件名里的 ".example"，变成 config.py，
然后填上你自己的 API Key。

为什么要有这个文件？
  config.py 里装着你的私密 API Key，所以它被 .gitignore 排除，不会提交到仓库；
  而这份"没有 Key 的模板"会跟着仓库走——换台电脑重新克隆项目时，
  复制它、填上 Key 就能用，不会缺少配置。
"""

# ============================================================
# ★★★ 第一步：把下面的 API Key 填上（其余先不用管）★★★
# ------------------------------------------------------------
# 1. 去 https://platform.deepseek.com 注册并创建一个 API Key（以 sk- 开头）
# 2. 把 Key 填进下面这行引号里面，保存文件
# ============================================================
API_KEY = ""

# ============================================================
# 以下都是可选配置，想改再改
# ============================================================

# DeepSeek 的接口地址（兼容 OpenAI 格式，一般不用改；
# 以后想换成其他家的大模型，改这一行就行）
API_URL = "https://api.deepseek.com/v1/chat/completions"

# 用哪个模型：deepseek-flash（快速、1M 上下文）/ deepseek-v4-pro（更强，同样 1M）
MODEL = "deepseek-flash"

# codingstorm 主服务地址（create_task / list_projects 等工具用它）
CODINGSTORM_URL = "http://127.0.0.1:8788"

# 系统提示词：给 AI 定人设的地方，随便改
SYSTEM_PROMPT = """你是 codingstorm 的需求分析师与任务投递助手。

你的工作不是写代码，而是把用户的自然语言需求整理成一条可以排队的任务。

规则：
- 先用 list_projects 看有哪些项目。用户没指定、且不止一个项目时，先问一句在哪个项目做；只有一个项目就直接用。
- 信息不够时只问必要的问题，不要盘问。能合理假设的就先假设，并在任务正文里写清楚。
- 需求清楚后，把内容整理成结构化任务：
  标题：一句话，动词开头，不超过 40 字。
  正文：目标 / 范围 / 验收标准 / 约束与假设。验收标准要可验证。
- 调用 create_task 创建任务。**只调用一次**，不要重复创建。
- create_task 会立即返回 task_id 和 queued 状态；任务由后台执行，你**不要等待、不要轮询、不要假装盯着进度**。
- 拿到结果后，回复：已加入队列 + 任务 id + 一句话摘要。可以附上任务链接：
  http://127.0.0.1:8788/#/task/<task_id>
- 如果用户还要补充，就继续对话；不要主动去查任务执行结果。
- 不要编造项目 id、任务 id 或执行结果。工具报错就如实告诉用户，并给出下一步建议。
"""

# ===== 工具调用 =====

# 工具总开关：True 打开（模型学会自己读文件、查目录、改文件）
ENABLE_TOOLS = True

# 轮次上限：一轮对话里最多允许模型连续调用工具几轮。
#   0 = 不限制（默认），让模型自己决定什么时候收工；想加保险就填个正数（比如 30）。
MAX_TOOL_ROUNDS = 4

# 执行命令工具（exec_command）的总开关：
# True = 模型可以在工作区里执行系统命令（跑脚本、git、查环境等）；
# False = 工具不注册，模型看不到这个能力
ENABLE_EXEC = False

# ===== 图片 / 截图理解（多模态输入）=====

# 总开关：True = 可以在输入框粘贴/上传图片，有图片的请求自动用视觉模型
ENABLE_VISION = False

# 视觉模型：只有它接受图片输入（官方实验版，纯文本能力与 deepseek-flash 持平）
VISION_MODEL = "deepseek-v4-flash-vision-exp"

# 单张图片的大小上限（MB）——原图直传，不做压缩
MAX_IMAGE_MB = 5

# ===== 上下文管理（自动压缩）=====

# 总开关
ENABLE_COMPACT = True

# 模型上下文窗口大小（token）——deepseek-flash 是 1M（100 万）
CONTEXT_WINDOW = 1000000

# 用量达到窗口的百分之多少时，自动压缩上下文（0.7 = 70%）
COMPACT_THRESHOLD_RATIO = 0.7

# 压缩后保留最近多少条消息（更早的合并成一段摘要）
KEEP_RECENT_MESSAGES = 12

# ===== 用量统计（缓存命中率）=====

# 流式响应默认不带 usage，必须显式向服务端要（include_usage）。
# 打开后每次请求都会回一包 token 用量，其中包含缓存命中/未命中数——
# 「链路日志 → 缓存命中」面板就是靠它算出来的。
# 只有一种情况需要关掉：换了一家不认 stream_options 的兼容端点（会报 400）。
ENABLE_USAGE_STATS = True

# ===== 子代理：spawn_agent（让主 agent"派小弟去干活"）=====

# 总开关：True 打开（模型会多出一个 spawn_agent 工具）；
# 关掉后子代理功能完全消失（工具不注册，模型看不到）
ENABLE_SUBAGENT = False

# 子代理的人设：子代理有它自己的独立上下文（看不到主对话），
# 它的"最终回复"会被当作报告带回主对话——所以重点强调：精炼、给结论。
SUBAGENT_PROMPT = """你是主 agent 派出的子代理（探子）。
你只能看到下面的任务描述，看不到主对话——任务让你做什么，你就去做什么，不要反问（没人能回答你）。
规则：
1. 用工具自己去查、去做，不要凭空猜测
2. 不要寒暄、不要客套——你最后的那段回复就是交给主 agent 的报告
3. 报告要精炼、给结论，写清关键事实（文件名、行号、数字）
4. 能一段话说完，就别写两段
5. 全程用简体中文（包括过程里说的话）"""

# ===== 知识库（RAG 的检索部分）=====

# 总开关：True = 模型多出一个 search_kb 工具，可以主动查知识库
ENABLE_KB = False

# 哪些目录要进知识库（相对项目根目录）
# 可以是目录，也可以是单个文件。
# 根目录的 README 要单独列——它不在 docs/ 下，只扫目录会漏掉，
# 而安装步骤、常见问题这些恰恰都在里面。
KNOWLEDGE_SOURCES = ["docs", "knowledge", "README.md"]

# 每块最多多少字符
KB_CHUNK_MAX = 800

# 相邻块之间的重叠字数
KB_CHUNK_OVERLAP = 120

# 检索默认返回几块
KB_TOP_K = 5

# 标题里的词算几遍（1 = 和正文同权，3 = 三倍）。
# 调大 → 更相信"章节标题"，调小 → 更相信"正文反复出现"。
# 这个值是用实验定的：1/2/3/4 各跑一遍，3 的时候命中章节标题的排第一，4 就饱和了。
KB_TITLE_WEIGHT = 3

# 检索结果一次最多塞多少字符（防止把模型上下文撑爆）
KB_MAX_OUTPUT = 4000

# ===== 向量检索（第二路召回，用阿里云百炼）=====
#
# 向量这一环必须另找一家：DeepSeek 和 Anthropic 都不提供 embedding 接口。
#
# 怎么拿 Key：
#   1. 打开 https://bailian.console.aliyun.com 开通百炼（免费，送 100 万 token）
#   2. 右上角「API-KEY」→ 创建
#   3. 填到下面（是百炼的 API-KEY，不是阿里云主账号的 AccessKey）
DASHSCOPE_API_KEY = ""

# 向量模型与维度
EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 1024

# 一次最多送几条文本（v4 的限制是 10 条）
EMBED_BATCH = 10

# 接口地址（OpenAI 兼容格式，请求体就是标准 JSON）
EMBED_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

# 两路召回各取前几名去融合
RECALL_TOP = 30

# ===== 视频分析（B站 → 文字）=====

# 总开关：True = 模型多出 video_search / video_fetch / video_transcribe 三个工具
ENABLE_VIDEO = True

# B站 cookie（可选）
# 不填也能用：搜索、视频信息、音频流都免登录。
# 填了才看得到 **AI 字幕**（未登录时字幕接口返回空数组）。
# 怎么拿：浏览器登录 B站 → F12 → 应用/存储 → Cookie →
#         复制 SESSDATA 那一项，写成 "SESSDATA=xxxxx" 即可。
VIDEO_BILI_COOKIE = ""

# 单个视频的时长上限（分钟）：超过就拒绝，避免一个 3 小时的视频把对话卡死
VIDEO_MAX_MINUTES = 120

# 转写后端：
#   voice2text —— 调用本机的 voice2text 服务（D:\project\voice2text）。
#                 它自带 Python 运行时、faster-whisper、cuDNN 和 ffmpeg，
#                 按 cuda + large-v3-turbo 配好了 —— 白捡一个调好的 GPU 后端。
#   local      —— 项目内自己跑 faster-whisper（要求 pip install，无 GPU 加速）
ASR_BACKEND = "voice2text"

# voice2text 服务地址（它的 set.ini 里 web_address 那一行）
VOICE2TEXT_URL = "http://127.0.0.1:9977"

# voice2text 的启动程序（服务没开时自动拉起；不想自动启动就把下一行设 False）
VOICE2TEXT_EXE = r"D:\project\voice2text\start.exe"
VOICE2TEXT_AUTOSTART = True

# 用它里面哪个模型（它的 model_list 里的名字）
VOICE2TEXT_MODEL = "large-v3-turbo"

# ---- 以下只在 ASR_BACKEND = "local" 时才用 ----

# 转写模型：先看 ASR_MODEL_DIR，没有才按名字找（tiny/base/small/medium/large-v3）
ASR_MODEL_SIZE = "small"

# 本地模型目录（绝对路径）。填了就优先用它，不再按名字下载。
# 例：D:\project\voice2text\models\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo\snapshots\<hash>
ASR_MODEL_DIR = ""

# 设备：auto = 能用 CUDA 就用，否则 CPU；也可强制 cpu / cuda
# 注意：ctranslate2 的 CUDA 需要 CUDA 12 + cuDNN 9，缺了会自动退回 CPU
ASR_DEVICE = "auto"

# GPU 算力类型：float16（显存占用中等、速度快）；CPU 上固定用 int8
ASR_COMPUTE_TYPE = "float16"

# CUDA 运行库所在目录（可选，Windows 用）
# 如果 cublas/cudnn 是 pip 装的（在 site-packages\nvidia\*\bin），
# 或想借用某个打包应用自带的库，把目录写在这里
CUDA_DLL_DIRS = []

# ffmpeg 所在目录（可选）。为空则用系统 PATH 里的 ffmpeg。
# 抽帧、转码要用；只做转写不需要它。
FFMPEG_DIR = ""

# 领域词表（专有名词，逗号分隔）：能显著减少专名错字
# 例："RAG,向量检索,召回率,重排"
ASR_HOTWORDS = ""

# 确定性纠错表：稳定复现的错字，用"错=对"成对写，逗号分隔
# 例："REG=RAG,GBT-4O=GPT-4o"
# ★ 它是**不看上下文**的粗暴替换，只放你确认没歧义的
ASR_FIXES = ""

# 下载模型的镜像站。国内直连 huggingface.co 基本不通，所以默认走镜像；
# 能直连的话把它改成 "" 即可
HF_ENDPOINT = "https://hf-mirror.com"

# 网页服务监听地址：默认 127.0.0.1（只有本机能访问，安全，不弹防火墙提示）。
# ★ 部署到 Docker / Linux 服务器时必须改成 0.0.0.0：容器里的 127.0.0.1 指的是
#   容器自己，外面访问不到。可以用环境变量 MYWHEEL_HOST=0.0.0.0 覆盖，
#   不用改这个文件、也不用重建镜像。
HOST = "127.0.0.1"

# 网页服务端口（如果启动时提示端口被占用，把 8000 改成 8001 再试）
PORT = 8001
