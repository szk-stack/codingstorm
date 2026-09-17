# 上下文方案：调研结论与设计

> 结论日期：2026-09-17
> 本文是 codingstorm 上下文子系统的设计依据。改动设计前请先读这里，避免重新踩坑。

## 要解决的问题

codingstorm 的每个任务是**独立会话**（不 resume）。好处是干净、可复现；代价是 AI 完全不知道项目背景和之前发生过什么。所以上下文必须靠外部文档补，而不能靠会话记忆。

方案是三层：**必读层 → 索引层 → 沉淀层**。下面是每一层经过调研修正后的最终形态。

---

## 层 1 · 必读层 —— 无条件注入，但要砍到极小

### 关键反直觉结论：架构概览类文档有害

这是我最初方案里**错得最厉害**的一块。原本打算每次注入一份 200~500 行的项目概览（做什么、技术栈、目录结构、关键约定）。实证研究推翻了这个做法：

| 上下文文件来源 | 成功率变化 | 成本变化 |
|---|---|---|
| LLM 自动生成 | **−3%** | +20% |
| 人工编写 | +4% | +19% |

更关键的是：**架构概览和目录结构描述对 agent 定位文件没有帮助** —— 有没有概览，找文件的耗费一样。病根是**冗余**：把仓库里已有的文档移走后，同一个自动生成文件反而 **+2.7%**。

所以瓶颈是「**塞太多**」，不是「找不到」。这一点决定了后面 RAG 的取舍。

### 只有四类东西真正改变行为

1. **精确的 build / test / lint 命令**（带参数，能直接抄进终端跑）
2. **AI 推不出来的非显然约束**（比如"这个模块不能用 X，因为 Y"）
3. **项目专有工具的调用方式** —— 数据很极端：*提到则被调用 2.5 次，不提则 0.05 次*
4. **每条任务都适用的硬规则**

### 最终形态：约 100 行的「指针图」

只写上面四类，外加「X 类事看 docs/Y.md」的指针。**不要写"这个项目是做什么的"** —— 那类信息 agent 自己读代码就能得到，写进去只是稀释信噪比。

### 注入点

Vibe Kanban 的 `append_prompt`（agent profile 字段，追加到 system prompt 的静态文本）是这个注入点的现成参考。见 `prior-art-vibe-kanban.md`。

---

## 层 2 · 索引层 —— 按需自取，不做检索

### 机制

把一份**文档目录**（哪份文档讲什么）放进 prompt，让 AI 用原生的 Read / Grep 自己去取深层内容。零额外基建。

### 独立背书：progressive disclosure

Andrew Ng 的 `context-hub`（13.8k stars）独立走到同一结论，术语叫 **progressive disclosure**：

- 每个条目是一个目录，入口 `DOC.md` ≤500 行，链接到细节 reference
- agent 先读概览，再按需加载
- 数据策略：**registry 常驻 + 文档按需拉取 + 可选全量包**

### 值得抄的拆分：docs vs skills

`context-hub` 按**访问模式**把内容分成两类，这个拆分可以直接用：

| 类型 | 回答的问题 | 体量 | 加载方式 |
|---|---|---|---|
| **docs** | "要知道什么" | 10K–50K token | 每任务现拉 |
| **skills** | "怎么做" | <500 行 | 可持久安装 |

### 值得抄的机制：注解回路

agent 把踩到的坑记在本地（`chub annotate`），跨 session 保留，下次拉取时自动带出（默认按不可信输入处理）。**零向量库的轻量记忆**，是它唯一真正新颖的设计。

### 两条警告

- `context-hub` 第三方评测**只有 2/5**，且与 Context7、`@url` 功能重叠。**别照抄代码，只抄思路。**
- **`llms.txt` 不要碰**：30 万域名分析中对 AI 引用**无可测影响**；Google 称无 AI 系统在推理时读它；采纳率约 10%；且**过期比没有更糟**。当内部约定可以，别当基础设施。

---

## 层 3 · 沉淀层 —— 最关键的一层

### 为什么它决定成败

Cline 的 **Memory Bank** 是现成的反面教材：6 个 markdown 文件、结构很好，**但更新靠用户手动说 "update memory bank"**。AI 不会可靠地自更新 —— 这就是它烂掉的根本原因。

> **结论：自动触发是这层的前提，不是加分项。** 如果一个机制需要人记得去维护它，它就会死。

### 腐烂是实证问题，不是态度问题

- **23%** 的仓库 AI 配置文件含**失效的代码元素引用**（扫了 356 个仓库）
- 指令文件约 8 个月增长 **226%**，每 commit 净增 **4.9 条**
- 最麻烦的：规则的「**可删除危险度**」随指令年龄**下降** —— 老规则留着**不是因为有用，是因为没人能证明它没用**

第三条是核心矛盾：删除成本随时间升高，于是文件只增不减。

### 解法：给每条记录挂生命周期元数据

把"删不删"从**开放判断**变成**封闭谓词**：

| 字段 | 含义 |
|---|---|
| `source` | 为什么加这条 —— 记录观察到的失败 |
| `applicability` | 什么条件下触发 |
| `expiry` | 什么条件下可以删 |

所以沉淀**不能只记「改了什么」**，要记 **「为什么改、何时可删」**。

数据点：去掉「复发频率」一项，效果损失 **37%** —— 说明元数据里哪些项是真正吃劲的。

### 实用细节：元数据写在 HTML 注释里

**CLAUDE.md 里的 HTML 注释在进模型前会被剥离** —— 所以把生命周期元数据写在 HTML 注释里，**不花一个 token**，但人和工具都能读到。

### 现成参考

- **`888wing/codetape`**（Claude Code skill）最接近这层：`/trace` 记录语义变更（含原因）、`/trace-sync` 同步文档、`/trace-review` 检测漂移。全本地、零依赖。
- **`Fission-AI/OpenSpec`** 的 `/opsx:propose` 是轻量 spec 工作流的参考。

---

## RAG —— 明确不做

两个理由：

1. **对标不划算**：`context-hub` 那个量级都还是 registry + 按需拉取 + 本地缓存，**零向量库**。
2. **更根本的**：层 1 的实证显示瓶颈是「**塞太多**」，而 RAG 治的是「**找不到**」—— **治错了病**。

将来文档量到几百份、单份很长时再重新评估。

---

## 一条独立验证

`ElectricJack/agent-queue` 把 profile / 记忆 / 项目覆盖**写成可编辑的 markdown 文件，数据库只做投影**。

这跟上面三层是**同一个答案**，两条独立的研究路径撞到了一起 —— 说明「**文档即上下文、DB 只是索引**」这个方向站得住。实现时应遵循：**markdown 是真相来源，数据库是投影**。

---

## 实现清单

- [ ] 每项目一份约 100 行的指针图，走 `append_prompt` 式注入点，无条件注入
- [ ] 按 docs / skills 两类组织文档，各自独立的加载策略
- [ ] prompt 里放文档索引，让 agent 自己 Read / Grep 取深层内容
- [ ] 任务结束时**自动**追加变更记录（含 `source` / `applicability` / `expiry`），元数据写 HTML 注释
- [ ] 提供漂移检测（参考 `codetape` 的 `/trace-review`）
- [ ] 不引入向量库、不引入 `llms.txt`

---

## 来源

- [Context Rot in AI-Assisted Software Development (arXiv 2606.09090)](https://arxiv.org/abs/2606.09090v1) —— 23% 失效引用
- [Catastrophic Remembering: Instruction Files That Only Grow (arXiv 2608.11095)](https://arxiv.org/abs/2608.11095v1) —— 226% 增长、删除危险度衰减
- [AIDev 实证分析 (arXiv 2606.13449)](https://arxiv.org/abs/2606.13449v1)
- [andrewyng/context-hub](https://github.com/andrewyng/context-hub) · [设计文档](https://github.com/andrewyng/context-hub/blob/main/docs/design.md)
- [Context Hub 第三方评测](https://github.com/FlorianBruniaux/claude-code-ultimate-guide/blob/614dcc46/docs/resource-evaluations/2026-03-16-andrewyng-context-hub.md)
- [Cline Memory Bank 文档](https://docs.cline.bot/best-practices/memory-bank)
- [888wing/codetape](https://github.com/888wing/codetape)
- [AgentPatterns: Evaluating AGENTS.md](https://www.agentpatterns.ai/instructions/evaluating-agents-md-context-files/) · [Stale AI Configuration Artifacts](https://www.agentpatterns.ai/anti-patterns/stale-ai-configuration-artifacts/) · [AGENTS.md as a Table of Contents](https://github.com/agentpatterns-ai/website/blob/main/instructions/agents-md-as-table-of-contents.md) · [llms.txt 评估](https://github.com/agentpatterns-ai/website/blob/main/standards/llms-txt.md)
- [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec)
