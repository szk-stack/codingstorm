# assistant/ — codingstorm 需求助手

这是 codingstorm 仓库里的**助手 sidecar**：一个独立进程（默认 `127.0.0.1:8001`），
被主服务用 iframe 嵌进顶栏「助手」页。代码来自 `D:\project\朴素agent`（myWheel）的
精简裁剪副本，**原项目不参与运行、也不会被修改**。

它不是 `codingstorm` Python 包的一部分，`pyproject.toml` 的 wheel 只打包
`codingstorm/`，所以这份代码不会进 wheel；`pip install` 出来的环境要用助手，
得把本目录一起部署，并在 `codingstorm.toml` 里配 `[assistant] dir`。

## 它做什么

- 用人设把自然语言需求整理成结构化任务（目标 / 范围 / 验收标准 / 约束与假设）；
- 调用 `create_task` 把任务加入 codingstorm 队列，**立即返回** `task_id`，不等待、不轮询执行；
- 只暴露四个工具：`list_projects`、`get_project_context`、`create_task`、`list_tasks`。

没有 shell、没有文件读写、没有图片/视频/子代理/RAG。最坏结果是建了一条不该建的任务。
回复里的链接统一新标签页打开，避免任务链接把整个应用装进 iframe。

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

主服务 codingstorm 要在 `http://127.0.0.1:8788` 运行；工具通过它列项目 / 建任务。
端口或主服务地址改了，要同步改这里的 `PORT` / `CODINGSTORM_URL` 和主服务的
`[assistant].url`。

## 配置

| 文件 | 是否入库 | 作用 |
|---|---|---|
| `config.example.py` | ✅ | **需求分析师版模板**（人设、工具开关、端口 8001）。没有 `config.py` 时自动回退到它，clone 下来直接能跑 |
| `config.py` | ❌ | 本机配置，可放 API Key；被 `.gitignore` 排除 |
| 环境变量 `MYWHEEL_*` | — | 优先级最高；主服务拉起时会注入 `MYWHEEL_PORT` / `MYWHEEL_API_KEY` |
| 页面「API Key 配置」 | ❌ | 写到 `api_key.json`，同样不入库 |

Key 优先级：`MYWHEEL_API_KEY` > `api_key.json` > `config.py`。
`ENABLE_EXEC / VISION / SUBAGENT / KB` 全部为 `False`，别打开 ——
那些能力对应的工具文件已经从 `tools/` 移除了。

## 自检

```bash
cd assistant
python selftest.py
```

`tools/` 是「按文件名自动加载」的：某个文件忘了写末尾的 `register(DESCRIPTION, run)`，
模块照样 import 成功、日志照样打印「已加载」，但模型根本看不到那个工具 —— 这个坑很安静
（第一版就踩过一次）。自检确认四个工具都在、且没有混入原 agent 的危险工具。
改动 `tools/` 后跑一次。

## 数据

- 会话 / 链路日志默认写在本目录（`data.jsonl` / `traces.jsonl` / `api_key.json`，
  全部被 `.gitignore` 排除）；设 `MYWHEEL_DATA_DIR` 可换位置。
- 由主服务拉起时的崩溃日志：`{root}/logs/assistant.log`。
- 端口：8001（`MYWHEEL_PORT`）。

## 已验证

- `python selftest.py` → 4 个工具，白名单正确；
- 「现在有哪些项目？」→ 调 `list_projects`，流式返回项目表并追问在哪个项目做；
- 「给 demo 项目加一个 HEALTH.md…」→ 只调一次 `create_task`，返回 `status=queued`
  + `task_id` + 任务链接，助手随即回复「已加入队列」且不查进度。

核心代码按其 `LICENSE` 使用。这是裁剪副本，不是原项目的替代品。
