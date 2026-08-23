# ADR-0004：砍掉 repo map 的 PageRank，只保留 Glob/Grep + 自描述截断

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：docs/specs/05-code-retrieval_CN.md · docs/BUILD_LIST_CN.md「5. Glob/Grep + 自描述截断」与「C 档 · repo map（PageRank 那半）」· ADR-0001（工具结果是上下文的主要消耗方）· docs/PITFALLS.md（待建）

## 背景

今天这个 agent 没有任何检索原语。`mini_agent/tools/__init__.py:3-6` 导出的就是 `Tool, ToolResult, ReadTool, WriteTool, EditTool, BashTool, SessionNoteTool, RecallNoteTool`；`cli.py:422-424` 只注册 Read/Write/Edit。于是所有发现动作都落到 `bash`，而 bash 输出**零截断**：`BashOutputResult.format_content`（`bash_tool.py:33-47`）把 `self.stdout` 直接 `+=` 进 content（`bash_tool.py:37`），不做任何长度检查；失败路径更糟 —— `bash_tool.py:418-421` 用**整份** stderr 拼 `error_msg`，而 `agent.py:470` 对失败工具发的是 `f"Error: {result.error}"`，**彻底绕过 formatter**。

规模差距是实测的（本机 Darwin 25.5.0，仓库 `953b943` 工作树，2026-08-24）：

```
rg --files              →   382
rg --files --hidden     →   452
Path('.').rglob('*')    →  7062
```

约 18 倍。这一行就说清了为什么需要一套 ignore 策略，也说清了为什么两个后端（有 ripgrep / 没有）必须共用同一套策略 —— 否则同一个问题在两台机器上得到两种答案。

至于 repo map 的 PageRank 那半：aider 有公开博客写了公式、tree-sitter 查询和预算二分，它是全世界文档最全的 agent 组件；而前沿 agent（Claude Code / Codex）恰恰没走这条路。

## 选项

1. **A —— 完整 aider 式 repo map**：tree-sitter 抽取 def/ref → 文件级引用图 → personalized PageRank → 按 token 预算二分渲染 → 每会话注入一次、编辑后刷新。
2. **B —— 只做 Glob/Grep + 自描述截断 + bash 输出钳制**：两个廉价原语，硬上限，尊重 ignore 规则，每一处截断都带真实总数。
3. **C —— embedding 检索**：对代码分块 embed，查询时向量召回。
4. **D —— 子 agent 扇出搜索**：派生若干子 agent 并行 grep/read，父上下文只收结论。

## 决定

选 B。范围：`GlobTool` / `GrepTool`（有 ripgrep 用 ripgrep，否则纯 Python，**两个后端强制共用同一套 ignore 策略并写测试 diff 两边的结果集**）、bash 输出的中间挖空式钳制（成功路径和失败路径都要覆盖）、以及一条硬不变量 —— **任何被截断的结果必须自描述**，页脚带真实总数，例如 `[truncated: showing 50 of 312 matches]`。

不做：符号抽取、tree-sitter 依赖、引用图、PageRank、预算二分、map 注入与它跟压缩器的交互（`agent.py:186` 那圈 `user_indices` 扫描）。

## 为什么否决其他的

**A —— 完整 repo map + PageRank。** 三条理由，按分量排。(a) **它最容易被一句话打发**："你复现了 aider 的 `tags.py`。" 公式、tree-sitter query、预算二分全都是公开的，我做的是复现而不是判断。(b) **它的评测不可证伪**：招牌指标是 recall@10 / MRR，而那十条 gold（问题 → 标准答案文件）是我在调完排序之后自己写的 —— 面试官第一件事就是重跑你的 bench，这个 bench 重跑了也证明不了什么。(c) 前沿 agent 没走这条路，所以"我实现了它"回答不了"你为什么认为它该被实现"。**要说清的是：它的机制难点是真的。** 本仓库实测 `grep -rn "def execute" mini_agent`（排除 vendored `skills/`）有 **11 处定义分布在 6 个文件**（`tools/skill_tool.py:40`、`tools/mcp_loader.py:89`、`tools/file_tools.py:108,195,256`、`tools/note_tool.py:91,163`、`tools/bash_tool.py:309,485,568`、`tools/base.py:34`）—— 一个 `tool.execute(...)` 调用点会发出 11 条等权边，不做 definer 数归一化，rank 就汇聚到字母序最前的那个文件；再加上 dangling 节点吸收质量、均匀 teleport 让 `tools/base.py` 稳坐第一，这三个坑都是真机制。砍它不是因为它简单。**什么条件下它反而是对的**：存在**外部**的、不是自己写的排序评测（比如 SWE-bench 的定位子任务），排序质量能被别人验证；或者产品形态里根本没有 agentic search 能力（一次性索引服务、IDE 插件的静态大纲），那时这个先验是唯一的定位手段。

**C —— embedding 检索。** 它在**词汇错配**上真的赢：问"我们在哪儿处理限流"，代码里写的是 `retry` 和 `backoff` —— 本仓库就是字面例子，`grep -in "rate limit\|ratelimit\|429\|throttl" mini_agent/retry.py` 返回空，文件里只有 `backoff`（`retry.py:6,41,52`）。但它输在**写放大**：一个每几秒就改一次代码的 agent，每次编辑都要重新 embed，而 chunk 边界会把一个类和它的方法切开，召回回来半个签名。**什么条件下它反而是对的**：代码库基本只读（文档站、代码搜索产品、跨仓库检索），或者查询确实是自然语言意图而不是标识符 —— 那时词汇错配是主要失败模式，而新鲜度不是约束。

**D —— 子 agent 扇出搜索。** 它在精度和规模上都赢：排序由一个上下文里真有任务的模型做，不需要索引，在二十万文件的 monorepo 里优雅降级（那种规模下任何静态 map 都是舍入误差）。输在延迟和 token：第一次编辑之前要好几轮往返，每个子 agent 各烧自己的窗口。**什么条件下它反而是对的**：ADR-0002 的事件缝和 ADR-0001 的上下文管理都已落地之后 —— 它需要一条能观测子 agent 的事件流，也需要一个能把子 agent transcript 挡在父上下文之外的机制。但即便那时它的边际信号也是递减的：它是"限制什么进入上下文"这个洞见的第二次应用，而且它的招牌指标"父上下文增长"要拿新压缩器做基线，所以它必须排在 ADR-0001 之后，不能并行。

**保留下来的那条洞见，单独点出：静默截断是正确性 bug，不是显示问题。** 模型看不见没被展示给它的东西 —— 给它 312 个匹配里的 50 个而不说，它会得出"一共 50 个调用点，我都看过了"，然后重构 50/312 并报告完成。这条洞见跨三处生效，而且三处今天都是坏的或缺失的：(1) `grep` 的 `head_limit` 必须带 `showing N of M` 页脚；(2) `glob` 的文件上限必须在**截断之前**按 mtime 倒序排 —— 如果非丢不可，就丢最不可能正在被改的那些；(3) bash 输出的钳制必须是**中间挖空**而不是只留头部，因为构建和测试的失败摘要在**末尾**，只留头部的截断会稳定地删掉唯一重要的那一行。这一条比 PageRank 值钱，是因为它是一个可以写成断言的不变量，而 PageRank 只是一个排序器。

## 怎么验证它是对的

- `pytest tests/test_search_tools.py -q`，三条断言：(a) **后端一致性** —— 同一个 fixture 目录，ripgrep 后端和纯 Python 后端返回同一个路径集合（今天两条朴素路径的差距实测是 `382` vs `7062`）；(b) **截断自描述** —— 任何被截断的 `ToolResult.content` 都含 `showing N of M`，且 `M` 是真实总数而不是上限；(c) **glob 排序在截断之前** —— 构造 300 个文件、上限 200，断言返回的是 mtime 最新的 200 个。
- **bash 钳制必须覆盖失败路径**：跑一条 `exit != 0` 且 stderr 巨大的命令，断言进入 `agent.py:470` 那条 `f"Error: {result.error}"` 的字符串也被钳住。这是最容易漏的洞 —— 成功路径走 `format_content`（`bash_tool.py:33-47`），失败路径走 `bash_tool.py:418-421`，两条独立。
- **待测**：钳制前后 `rg -n "def " .` 的字符数与 tiktoken 计数；并排给出 `GrepTool(output_mode="files_with_matches")` 回答同一问题的 token 数 —— 目的是说明这个工具不只是一个上限，而是一种更便宜的**形状**。
- 前置修复（不做就没法诚实演示"编辑后检索仍然正确"）：`file_tools.py:280` 是 `content.replace(old_str, new_str)`，替换**所有**匹配，而 `file_tools.py:231` 的工具描述写的是 "and appear uniquely in the file, otherwise the operation will fail"。加一条 `if content.count(old_str) != 1` 的守卫，三行。

## 回头看

> 待实现后补。
