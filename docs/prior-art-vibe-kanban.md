# 前车之鉴：Vibe Kanban

> 调研日期：2026-09-17
> 这是目前同类产品里做得最完整的一个。读它的价值有二：**验证我们的选择**，以及**补上我们漏掉的机制**。同时它没解决的问题，正是我们的差异点。

## 它是什么，现在什么状态

`BloopAI/vibe-kanban`，Rust + React，★28k，YC 出身。形态跟 codingstorm 的目标高度重合：看板建单 → 给 agent 一个 workspace（分支 + 终端）→ UI 里审 diff 留行内评论 → 开 PR。支持多个 agent（Claude Code、Codex、Gemini CLI 等）。

**状态：冻结，但能用。**

- Bloop 于 **2026-04-10 关停**，CEO 原话：「绝大多数是免费用户，我们找不到商业模式」
- 云服务 30 天后下线并退款，**但本地功能一直可用**
- 开源协议 **Apache-2.0**，`npx vibe-kanban` 现在仍可安装（0.1.44）
- 仓库冻结：main 自 **2026-04-24** 起无提交；躺着 **383 个 open issue** 和 **157 个 open PR**
- 社区 fork `flashlan/vibe-kanban-alternative` 存在，成熟度未验证

> 准确定位：**一套可用但冻结的免费基础设施**。很好的设计参考，但没人修 bug，别作为长期依赖。

---

## 它**没有**解决上下文问题

这是本次调研最重要的结论。官方 Sessions 文档原文：

> **Sessions share files but not conversation context.**
> （会话共享文件，但不共享对话上下文。）

文档里的警告框更直白：

> 新会话不继承对话历史。新 agent 不知道之前会话发生了什么，**除非你重新解释一遍，或者它自己读文件改动**。

官方给的应对办法是：「每个会话聚焦一件事」、「用 workspace notes 记录哪个会话是干嘛的」—— **纯手工记笔记**。

上下文压力大了怎么办？官方建议是**「看 token 仪表盘，橙了或红了就开新会话」**。

### 它的上下文设施只有三个，且没有一个是项目级的

| 设施 | 是什么 | 评价 |
|---|---|---|
| `append_prompt` | agent profile 字段，追加到 system prompt 的静态文本 | **最值得抄**，我们的指针图正好塞这里 |
| Tags | `@mention` 插入的复用片段 | 官方明说全局通用，是手工模板不是上下文管理 |
| setup / dev / cleanup 脚本 | 仓库环境准备 | 是环境不是知识 |

项目级设置（Projects / Repositories 两个 tab）只有显示名、仓库路径、三条脚本，**没有任何"项目知识"字段**。

### 战略含义

**这个赛道最大、最成熟的产品都没解决上下文，用户在忍受。** 我们那套三层文档机制不是锦上添花，是真正的差异点。详见 `context-design.md`。

---

## 值得抄的四样

### 1. target / working 双分支模型

workspace = **git worktree**（独立目录）+ 自动分支 `vk/abc123-task-name`，**原仓库不动**。

它明确区分两个概念：

- **target branch** —— 你设，合并的去向
- **working branch** —— 从 target 切出，agent 在这里工作

这跟 codingstorm 定的「从主干切、批准即合并」**一模一样**，属于独立验证。

### 2. 落后即 rebase —— 这个我们漏了

它检测到分支落后于 target 时会**提示先 rebase**。

这恰好补上一个设计洞：任务 B 从主干切出后，任务 A 才被批准合并 —— 此时 B 就看不到 A 的改动。原本的备选方案是"链式堆叠"（B 基于 A 的分支切），但那样 A 被否决时整条链都要重做。**rebase 是更干净的解法。**

### 3. `append_prompt` 作为注入点

见上文。层 1 的指针图就挂这里。

### 4. profile 级环境变量

它专门文档化了用 `ANTHROPIC_BASE_URL` 接第三方 provider —— 跟我们服务器上的 DeepSeek 中转**是同一个模式**，说明这条路是验证过的。

---

## 顺带：worktree 让"并行"成为可能

worktree 让每个任务有自己的目录，所以**同项目并行在文件层面是安全的**。

这修正了我们选串行的一个理由 —— 真正的理由不是"两个 Claude Code 互相踩文件"，而是：

1. **合并冲突**：两个任务从同一基点切出、都改同一个文件，合并时必然冲突
2. **内存**：服务器只有约 2.3G 可用，只够 2~3 个进程

**结论不变（保持串行），但门是开着的** —— 将来想放开并行，不需要重新设计。

---

## 不抄的部分

- **Planning Mode**：profile 里的 `plan` 开关，让 agent 先出计划、你批准再写代码。它是**审批闸门，不是可复用文档** —— 没有持久计划文件。如果我们需要计划文件，得自己设计成文档形态（见 `context-design.md` 的层 3）。
- **看板 UI 形态**：对单人异步投递来说，看板可能过重。待定。

---

## 来源

- [Sessions](https://vibekanban.com/docs/workspaces/sessions) · [Creating Workspaces](https://vibekanban.com/docs/workspaces/creating-workspaces) · [Git Operations](https://vibekanban.com/docs/workspaces/git-operations)
- [Agent Profiles & Configuration](https://vibekanban.com/docs/settings/agent-configurations) · [Projects & Repositories](https://vibekanban.com/docs/settings/projects-repositories) · [Creating Tags](https://vibekanban.com/docs/settings/creating-task-tags)
- [Vibe Kanban 研究笔记（tomrochette，2026-09-13 核实）](https://blog.tomrochette.com/agents/vibe-kanban/index.md)
- [Vibe Kanban After Bloop（Nimbalyst）](https://nimbalyst.com/blog/vibe-kanban-after-bloop-whats-next/)
- [flashlan/vibe-kanban-alternative](https://github.com/flashlan/vibe-kanban-alternative)
- [npm registry: vibe-kanban@0.1.44](https://registry.npmjs.org/vibe-kanban/latest)
