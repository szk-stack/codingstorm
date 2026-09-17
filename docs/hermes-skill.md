---
name: codingstorm
description: 把编码任务投递到 codingstorm 队列，由服务器上的 Claude Code 异步执行。当用户说「提交任务」「入队」「让 AI 改这个」「看看任务跑完没」「批准/丢弃那个改动」时使用。
version: 1.0.0
metadata:
  hermes:
    tags: [coding, task-queue, async, git, review]
    related_skills: []
---

# codingstorm

codingstorm 是一个**按项目隔离**的任务队列：用户提交需求/指令/缺陷，服务器上的 Claude Code
在独立工作区（git worktree + 独立分支）里执行，跑完等用户审阅 diff。
核心价值是**异步** —— 提交完就能走，不用盯着等它跑完。

## 命令

用 `cs`（已装在 PATH 上；不在就试 `~/codingstorm/.venv/bin/python -m codingstorm.cli`）。

```bash
cs projects                        # 有哪些项目（提交前先确认项目名）
cs submit <项目> "<标题>" [--body "..."] [--kind requirement|instruction|bug|task]
cs ls [--project <名>] [--status awaiting_review]
cs show <任务id>                   # 详情 + 用量
cs events <任务id>                 # 执行过程（工具调用与输出）
cs diff <任务id>                   # 改动内容
cs approve <任务id>                # 批准并合入主干
cs discard <任务id>                # 丢弃
cs requeue <任务id>                # 重新入队（失败后重试）
cs usage                           # 用量与成本
```

任务 id 支持唯一前缀，不用打全。

## 关键行为

**提交后立刻返回**，任务在后台排队执行。不要等它跑完，也不要去轮询到完成 ——
告诉用户「已入队」并把任务 id 给他就够了。

用户问「跑完没」时，用 `cs ls --status awaiting_review` 或 `cs show <id>` 查一次，把结果报给他。

**批准是不可逆的**（会合入主干）。除非用户明确说批准，否则只做查看。

## 典型对话

- 「让 AI 给 stats.py 加个 mode 函数」
  → `cs submit demo "给 stats.py 加一个 mode(numbers) 函数" --kind requirement`
  → 回：「已入队 746735ae8c5a，跑完告诉我一声我帮你看」

- 「那个任务跑完没」
  → `cs ls --status awaiting_review`
  → 有的话：`cs diff <id>` 把改动摘要报给用户

- 「批准它」
  → `cs approve <id>`

## 注意

- 提交前如果不确定项目名，先 `cs projects`
- 标题要写成**一件事**，不要一句话塞多个需求 —— 一个任务一次执行
- 服务器上有边界规则，任务里试图 `git push` 会被拦下，这是预期行为
