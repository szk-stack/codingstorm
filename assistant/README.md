# assistant/ — codingstorm 本地助手

这是 codingstorm 仓库里的**助手 sidecar**：一个独立进程（默认 `127.0.0.1:8001`），
被主服务用 iframe 嵌进顶栏「助手」页。代码来自 `D:\project\朴素agent`（myWheel）的
裁剪副本，**原项目不参与运行、也不会被修改**。

它不是 `codingstorm` Python 包的一部分，`pyproject.toml` 的 wheel 只打包
`codingstorm/`，所以这份代码不会进 wheel；`pip install` 出来的环境要用助手，
得把本目录一起部署，并在 `codingstorm.toml` 里配 `[assistant] dir`。

## 它做什么

**两类能力，人设按用户措辞选择，拿不准会先问一句：**

A. 投递开发任务（codingstorm 主流程）

- 把自然语言需求整理成结构化任务（目标 / 范围 / 验收标准 / 约束与假设）；
- `create_task` 把任务加入队列，**立即返回** `task_id`，不等待、不轮询执行；
- 平台工具：`list_projects`、`get_project_context`、`create_task`、`list_tasks`。

B. 直接在本机工作区里动手

- `list_dir` / `read_file` / `write_file` / `edit_file`：列目录、读写改文件；
- `exec_command`：在工作区里跑一条 Windows 命令（60 秒超时、输出截断）；
- `view_image` / `screenshot`：看图片，或用 Edge 无头模式截图后再看；
- 输入框可以粘贴/上传图片（`ENABLE_VISION`）。

共 **11 个工具**。回复里的链接统一新标签页打开，避免把整个应用装进 iframe。

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
- 默认工作区是新会话自动建的沙箱目录；想操作主项目，把工作区选成仓库根目录。

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
`ENABLE_KB = False`、`MAX_TOOL_ROUNDS = 12`（文件/命令可能要多轮，留上限防死循环）。
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
  + `task_id` + 任务链接，助手随即回复「已加入队列」且不查进度。

核心代码按其 `LICENSE` 使用。这是裁剪副本，不是原项目的替代品。
