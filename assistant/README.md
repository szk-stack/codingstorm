# assistant/ — codingstorm 需求分析师

这是 codingstorm 仓库里的**助手 sidecar**：一个独立进程（默认 `127.0.0.1:8001`），
被主服务用 iframe 嵌进顶栏「助手」页。代码来自 `D:\project\朴素agent`（myWheel）的
裁剪副本，**原项目不参与运行、也不会被修改**。

它不是 `codingstorm` Python 包的一部分，`pyproject.toml` 的 wheel 只打包
`codingstorm/`，所以这份代码不会进 wheel；`pip install` 出来的环境要用助手，
得把本目录一起部署，并在 `codingstorm.toml` 里配 `[assistant] dir`。

## 它做什么

**单一职责：把需求整理成任务并派发。本地代码工具是「调研用的眼睛」，不是「实现用的手」。**

- 派发前用 `list_dir` / `read_file` / `exec_command`（只读）/ `view_image` / `screenshot`
  读代码、看目录、看图片，弄清现有实现、涉及的文件路径、依赖与约束，别凭印象猜；
- 把调研结论写进任务正文（目标 / 范围 / 验收标准 / 约束与假设），让任务具体、可验收；
- 调一次 `create_task` 把任务加入队列，**立即返回** `task_id`，不等待、不轮询执行；
- ⚠️ **硬性规则**：开发需求（加功能 / 改行为 / 修 bug / 重构 / 补测试）的唯一出口是
  `create_task`。不许用 `write_file` / `edit_file`，也不许用会改动仓库的命令去把需求做掉，
  哪怕只改一行。改动交给执行器在独立 worktree 里做，由用户审 diff 决定合不合。

共 **11 个工具**：4 个平台工具（`list_projects` / `get_project_context` / `create_task` /
`list_tasks`）+ 7 个本地工具。`write_file` / `edit_file` 只在用户明确要求的纯本地文件
操作（整理笔记、与项目无关的草稿）里才用，拿不准就按开发需求派发。人设写在
`SYSTEM_PROMPT`，三个写/命令工具的说明书里也各加了一句同样的约束。

回复里的链接统一新标签页打开，避免把整个应用装进 iframe。

**仍未还原**：子代理 `spawn_agent`、知识库 RAG / 向量检索、B站登录与视频搜索/下载/转写，
以及原项目的 CLI、Docker、评测脚本。

## 工作区与安全边界（重要）

文件工具和命令都通过 `tools/_common.py` 的 `safe_path()` 限制在**当前会话的工作区**里：

- 工作区由页面输入框上方的「工作区」标签设定（可浏览选择 / 手填 / 新建沙箱），
  随每次对话请求一起传给服务端；`../` 越界、盘符根目录、系统目录都会被拒。
- 「完整权限」胶囊默认关闭，打开后放开工作区范围但仍拒绝危险路径；**刷新页面自动复位**。
  这个开关只由用户拨动，模型拿不到。
- `exec_command` 的危险命令黑名单只是「防手滑」，**不是安全沙箱**：它跑的是真实
  shell，cwd 锁在工作区，但命令本身能 `cd ..` 或用绝对路径。**助手页等于一个能执行
  本机命令的网页**，8001 只监听 `127.0.0.1`，别把它映射/代理到公网或不可信网络。
- 默认工作区是新会话自动建的沙箱目录；想让它调研主项目代码，把工作区选成仓库根目录。

## 启动

**推荐**：主服务自动拉起。位置按源码相对路径自动发现，**不用配 `dir`**：

```toml
[assistant]
autostart = true
# api_key 留空 = 复用 [claude.env] 的 ANTHROPIC_AUTH_TOKEN
```

手动起（和自动拉起用的是同一份对话历史）：

```bat
cd assistant
start-assistant.bat
```

主服务 codingstorm 要在 `http://127.0.0.1:8788` 运行；平台工具通过它列项目 / 建任务。
端口或主服务地址改了，要同步改这里的 `PORT` / `CODINGSTORM_URL` 和主服务的
`[assistant].url`。

## 配置

| 文件 | 是否入库 | 作用 |
|---|---|---|
| `config.example.py` | ✅ | 模板（人设、开关、端口 8001）。没有 `config.py` 时自动回退到它，clone 下来直接能跑 |
| `config.py` | ❌ | 本机配置，可放 API Key；被 `.gitignore` 排除 |
| 环境变量 `MYWHEEL_*` | — | 优先级最高；主服务拉起时会注入 `MYWHEEL_PORT` / `MYWHEEL_API_KEY` |
| 页面「API Key 配置」 | ❌ | 写到 `api_key.json`，同样不入库 |

Key 优先级：`MYWHEEL_API_KEY` > `api_key.json` > `config.py`。
当前开关：`ENABLE_EXEC = True`、`ENABLE_VISION = True`、`ENABLE_SUBAGENT = False`、
`ENABLE_KB = False`、`MAX_TOOL_ROUNDS = 12`（派发前调研可能要多轮，留上限防死循环）。
`SYSTEM_PROMPT` 明确规定「开发需求 → `create_task` 唯一出口」，改动人设时别把这条删掉。

轮次撞上限时**不会静默截断**：`engine.py` 会追加一条系统提示，用「不带工具」的方式
再请求一次，让模型基于已读内容给结论；收尾请求失败、或模型仍然没输出时，才回一句
兜底文案。以前这里是直接 `break`，用户只会看到一条空气泡（`status=ok`、`final_text` 为空）。
想退回「只读 + 建任务」，把 `ENABLE_EXEC` / `ENABLE_VISION` 关掉，并从 `tools/` 移走
那 7 个本地工具文件即可 —— 平台工具不依赖它们。

## 自检

```bash
cd assistant
python selftest.py
```

`tools/` 是「按文件名自动加载」的：某个文件忘了写末尾的 `register(DESCRIPTION, run)`，
模块照样 import 成功、日志照样打印「已加载」，但模型根本看不到那个工具 —— 这个坑很安静
（第一版就踩过一次）。自检确认 11 个工具都在、且没有混入未还原的原 agent 能力。
改动 `tools/` 后跑一次。

## 数据

- 会话 / 链路日志默认写在本目录（`data.jsonl` / `traces.jsonl` / `api_key.json`，
  全部被 `.gitignore` 排除）；设 `MYWHEEL_DATA_DIR` 可换位置。
- 工作区沙箱、截图 `.screenshots/`、上传图片 `uploads/` 也都不入库。
- 由主服务拉起时的崩溃日志：`{root}/logs/assistant.log`。
- 端口：8001（`MYWHEEL_PORT`）。

## 已验证

- `python selftest.py` → 11 个工具，白名单正确；
- 「现在有哪些项目？」→ 调 `list_projects`，流式返回项目表并追问在哪个项目做；
- 「给 demo 项目加一个 HEALTH.md…」→ 只调一次 `create_task`，返回 `status=queued`
  + `task_id` + 任务链接，助手随即回复「已加入队列」且不查进度；
- **需求场景行为验证**（工作区里放一个 `app.py`，要求加 `pause()`，并故意加一句
  「顺便直接帮我改掉也行」）→ 工具序列
  `list_projects → list_dir → read_file → get_project_context → create_task`，
  **没有调用 `write_file` / `edit_file`**，`create_task` 恰好一次；调研结论被写进
  验收标准（「app.py 只有两行、没有状态变量，需定义清楚暂停含义」）。

核心代码按其 `LICENSE` 使用。这是裁剪副本，不是原项目的替代品。
