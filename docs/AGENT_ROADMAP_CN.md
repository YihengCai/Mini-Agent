# Mini-Agent 现状分析与演进路线（对标 Claude Code / Codex）

> **先读 [BUILD_LIST_CN.md](./BUILD_LIST_CN.md)**：目标已明确为"证明研究过并亲手实现了关键模块"，
> 行动清单以那份为准（本文第 5–6 节按"把项目做完善"排序，已被取代）。本文仍是现状诊断与 bug 证据的依据。

> 分析基准：commit `953b943`，`mini_agent/` 约 4.1k LOC（不含 vendored `mini_agent/skills` submodule）。
> 所有 `file:line` 均在该 commit 下核对过。

---

## 0. 结论先行

**当前定位**：Mini-Agent 是一个**跑通了 tool-use 闭环的 demo**——它证明"LLM 能调工具、结果能回灌、任务能收敛"。
**Claude Code / Codex 不是"工具更多的它"**，而是另一类东西：一个带**策略（permission）、状态（session/plan/checkpoint）、可观测（event stream）、可恢复（resume/rewind）、可度量（eval）**的执行运行时。

差距不在工具数量，在四条主轴：

| 主轴 | 现状 | 目标形态 |
|---|---|---|
| **内核形态** | `run()` 里 ~30 处 `print()`，渲染焊死在引擎里 | `run()` 返回 `AsyncIterator[AgentEvent]`，CLI / ACP / logger / eval 都是消费者 |
| **上下文工程** | 超阈值时一次性把执行过程换成 LLM 散文 | 分层：工具结果驱逐 → 带逐字尾巴的窗口摘要 → 结构化文件账本 + prompt cache |
| **安全与可回滚** | **零**：无权限、无沙箱、无路径约束、无快照 | 解析 argv 的权限引擎 + sandbox 降级 + shadow-git 检查点 / `/rewind` |
| **可度量** | 无 eval；`tests/test_agent.py` 有 **0 个 assert**，文件没创建也 `return True` | 20~24 个任务的 eval 套件，pass@1 / steps / tokens / $ / 失败分类 |

**最有力的证据（不是推测，是仓库自己写下的）**：`mini_agent/acp/__init__.py:127-165` 把整个主循环**又抄了一遍**，而且已经漂移——ACP 那份没有上下文压缩、没有 logger、没有逐工具取消、`{SKILLS_METADATA}` 占位符原样发给模型、无视 config 里的 `provider` 永远建 Anthropic client。这就是"渲染耦合进引擎"的账单，已经产生了。

### 简历视角：先解决归属，再谈技术

```
git shortlog -sn  →  akai 33, ... YihengCai 2      # 56 commits，你名下 2 个
git remote -v     →  github.com/YihengCai/Mini-Agent.git（fork 自 MiniMax-AI/Mini-Agent）
README.md:5       →  "demo project that showcases ... the MiniMax M2.5 model"
```

面试官在 AI lab 一定会敲 `git shortlog`。**如果简历写"我做了一个 coding agent"，被查出是 fork，后面所有技术细节都会被打折。**
正确做法不是放弃这个项目，而是**把 delta 做成作品**：

- `ARCHITECTURE.md` 开篇直说：*"Forked from MiniMax's Mini-Agent demo (4.1k LOC). 我重写了 context manager / 执行内核 / 权限层，下表是我自建 eval 上的 before-after。"*
- 简历 bullet 永远是 **"rebuilt X, measured Y"**，不是 "built an agent"。
- 诚实标注 + 有数字的 delta，**比一个全新玩具项目更有说服力**——它证明的正是 lab 真正要招的能力：把一个能跑但幼稚的 agent 变好，并且能量化。

---

## 1. 现状架构

```
cli.main() ──► Config.from_yaml (config.yaml 明文 api_key，无 env / 无 flag 层)
   │
   ├─► initialize_base_tools()  ── skills 发现 + MCP 串行连接（顺带 print 一堆 ✅）
   ├─► add_workspace_tools()    ── Read/Write/Edit/Bash(+Output/Kill)/Note
   │
   └─► Agent(llm, system_prompt, tools, max_steps)        # token_limit 永远是默认 80000
          │
          └─ run()  while step < max_steps:
                 ├─ _summarize_messages()      # 超阈值 → 整段历史换成散文
                 ├─ llm.generate()             # 非流式，阻塞整轮
                 ├─ print(thinking / content)  # ← 渲染在引擎内部
                 └─ for tool_call in ...:      # ← 严格串行
                        await tool.execute()   # ← 没有任何审批 / 沙箱 / 路径约束
                        print(result[:300])

acp/__init__.py._run_turn()  ── 上面这个循环的第二份拷贝（已漂移）
```

**一句话总结架构**：单 Agent、单上下文、单循环、单工具表，`Agent.__init__` 已经有 6 个参数——下一档能力（委派 / plan 状态 / 检查点 / 权限模式）**不是往 `run()` 里打补丁能到达的**，需要一个 `Session` 对象持有 tools + context + policy + transcript。

---

## 2. 模块逐一体检

### 2.1 Agent 主循环（`agent.py`，496 行）

**当前设计**：`self.messages: list[Message]` 扁平列表 + `dict[str, Tool]` 工具表；`run()` 是 `while step < max_steps` 的同步式循环：检查取消 → 压缩 → 调 LLM → 打印 → 串行执行工具 → 追加 tool 消息 → step+1。工具异常被 `except Exception` 兜住转成失败 `ToolResult`（含 traceback）回灌给模型。

**做对的地方**（这些是可以在面试里讲的底子）：
- 内部 schema 与厂商解耦（`schema.py`），`LLMClientBase` 抽象干净；
- thinking 块会回传给下一轮，不像很多玩具 agent 直接丢掉；
- 工具异常不杀进程，traceback 回灌让模型自纠；
- 重试是独立装饰器（`retry.py`），`RetryExhaustedError` 带 attempts。

**关键缺失**：

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | **无流式**：整轮 API 调用阻塞，屏幕空白；Esc 也打不断已发出的请求 | `llm/*.py` 全仓库无 `stream=` |
| P0 | **工具严格串行**：模型一次给 3 个 `read_file`，仍然一个一个跑 | `agent.py:404` `for tool_call in response.tool_calls` |
| P1 | **取消会删掉已落盘的工具记录**：步骤边界取消时 `_cleanup_incomplete_messages` 会把**上一个已完成**的 assistant 轮次连同它的 tool 结果一起截掉，但文件已经被改了——历史与磁盘状态不一致 | `agent.py:73-94`，由 `:316-319` 调用 |
| P1 | **工具结果每条一个 API message**，不是一个 user turn 里 N 个 `tool_result` block；且从不设 `is_error` | `anthropic_client.py:162-175` |
| P1 | **无 prompt caching**：system prompt + skills metadata + 全部工具 schema 每一步全价重算（补上它依赖 C1/C2，见[能力矩阵](PROVIDER_CAPABILITIES.md)，待测；不支持就只剩上下文预算这条路） | 全仓库无 `cache_control` |
| P1 | **无权限门 / 无 hook**：模型输出什么就执行什么，单一 dispatch 点 `agent.py:436` 前后没有任何拦截 | `grep -rni permission\|approve` → 无 |
| P1 | **无子 agent、无 todo/plan 状态、无 session 持久化**：崩溃或 Ctrl-C 丢掉整个会话 | — |
| P1 | **渲染在引擎内**：~30 处 `print()`，已导致 ACP 复制循环 | `agent.py` 全文 vs `acp/__init__.py:127-165` |
| P2 | `max_steps` 到顶就是死路；无"无进展"检测；重试无 jitter 且叠加 SDK 自带重试 | `agent.py:487-490`、`retry.py:99-131` |

### 2.2 上下文管理（README 的招牌，也是最大的坑）

**当前设计**：`_estimate_tokens()`（tiktoken `cl100k_base`，只算 messages **不算工具 schema**）或 API 上报的 `api_total_tokens` 超过 `token_limit`（**硬编码 80000，config 里根本没有这个字段**）时，把历史重建成 `system + [user_i + LLM 写的散文摘要_i]`。

**为什么这是 P0**：

1. **连"正在执行的这一轮"一起摘要掉**（`agent.py:204-207`：最后一个 user 的 `next_user_idx = len(self.messages)`）。你在 step 18 触发压缩，step 17 刚 `read_file` 拿到的**带行号的文件内容**就变成了"Assistant 读了 file.py 并找到了配置类"。step 19 模型去 `edit_file`，`old_str` 已经没有逐字来源 → 匹配失败 → 反复猜 → 把剩余 step 烧光。**没有任何逐字尾巴，没有文件状态账本。**
2. **不幂等**：`agent.py:186` 重新收集 `user_indices` 时，上一轮生成的摘要本身就是 `role="user"`，于是"user 消息"越压越多，压缩后再也压不下去——历史无界增长。
3. **摘要 prompt 用的是未截断的工具输出**（`agent.py:249-283`，`:259` 那个 `...` 是**假的**，字符串前面没截断）。也就是说：上下文越爆，摘要请求本身越大 → **压缩恰好在最需要它的时候失败**。
4. **摘要失败时返回原始未压缩文本**（`agent.py:289-292`）——压缩动作可能让历史变得更长。
5. `api_total_tokens` 在 `/clear` 后不清零（`cli.py:673-677`），空历史也会触发一次无意义压缩。

**对标**：Claude Code 的 auto-compact 产出**结构化**摘要（files read/modified、task state、next steps、user intent）并保留最近若干轮逐字。另一个可借鉴的公开设计是 server-side 的 `context_management`（`clear_tool_uses_20250919` 清旧 tool 结果、保留 tool_use/tool_result 骨架；来源：Anthropic 公开文档）—— **借鉴的是它的设计思路（驱逐旧 tool 结果、保留配对骨架），不是去调这个 API**：我们在客户端自己实现同等效果（本来也只能这样 —— 只有自己实现，驱逐状态才能接进 FileLedger 和检查点）；本端点是否接受这类厂商专有字段属于未探测项，按 C1/C2 的方式先探测再依赖，不支持就纯客户端做。

### 2.3 LLM 抽象层（`llm/`，约 800 行）

**当前设计**：`LLMClient` 门面按 provider 实例化 `AnthropicClient` / `OpenAIClient`，两者各自做 message 转换、tool schema 转换、response 解析、重试。

**关键缺失**：

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | 无流式（同上） | — |
| P1 | **extended thinking 往返有损**：解析时丢掉 `signature`，回放时也不带，`redacted_thinking` 直接丢弃；且从未真正开启 thinking 参数（修它依赖 C10，待测；不支持就不回传 thinking，并记一条 PITFALL 说明代价） | `anthropic_client.py:219-220`、`:139-140` |
| P1 | **重试无错误分类**：`retryable_exceptions=(Exception,)` → 400/401 也重试 3 次；不看 `Retry-After`；无 jitter；与 SDK 自带重试相乘 | `cli.py:521-527`、`retry.py:99-131` |
| P1 | **无累计 token / 成本统计**：`api_total_tokens` 是**覆盖**不是累加，CLI 里 "API Tokens Used" 显示的其实是最后一次调用的 total；摘要调用完全不计 | `agent.py:358-360` + `cli.py:245-246` |
| P1 | 生成参数全无：`max_tokens=16384` 硬编码，无 temperature / top_p / stop / thinking budget，config 里也够不着 | `anthropic_client.py:70` |
| P1 | 无 request timeout、无 fallback model；无模型上下文窗口注册表 | — |
| P1 | `tool_result` 只能是字符串——图片、结构化内容都进不来 | `schema.py:33` 允许 block list，但没有任何代码产出它 |
| P1 | OpenAI 客户端其实是"披着 OpenAI 皮的 MiniMax 客户端"：无 `tool_choice` / `parallel_tool_calls` / reasoning 模型支持，厂商私有字段无条件下发 | `openai_client.py:160-166` |
| P2 | `finish_reason` 被解析但从不判断——`max_tokens` 截断和正常完成无法区分 | `agent.py:390` 只看 `tool_calls` |

**真 bug**：`openai_client.py:221-224` 里 `reasoning_details` 是 SDK 的 extra 字段，返回的是 **dict 列表**，而代码用 `hasattr(detail, "text")` 判断 → 恒为 False → **OpenAI 路径的 thinking 永远被静默丢弃**；`openai_client.py:231` `json.loads(tool_call.function.arguments)` 未保护，一个畸形 tool-call 载荷直接把整个 run 抛崩。

> 这两条只记录、**不修**：项目不采用 OpenAI API（见 [AGENTS.md](../AGENTS.md) 的「Provider 约束」），所以"OpenAI 客户端保真"从"低优先级"升级为"不做"；上表里带 `openai_client.py` 的行同理。

### 2.4 文件工具（`file_tools.py`，285 行）

**当前设计**：`ReadTool`（带行号、可 offset/limit、按 token 截断）、`WriteTool`（整文件覆盖）、`EditTool`（字符串替换）。

**关键缺失 / 真 bug**：

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | **`edit_file` 替换所有匹配**，而它的 description 明确承诺"必须唯一否则失败"——模型按"唯一替换"语义在用，实际是全局替换 | `file_tools.py:280` `content.replace(old_str, new_str)` vs `:230-232` |
| P0 | **read-before-write 只是 description 里的一句话**，没有任何 FileTracker 强制 | 全仓库无 tracker |
| P0 | **无路径沙箱**：绝对路径 / `..` / 符号链接随意逃逸 workspace，`read_file("/etc/hosts")` 返回 success | `file_tools.py:111-114, 198-201, 259-262` |
| P0 | **没有 glob，没有 grep**：任何搜索都得走 bash，而 bash 输出**零截断**——一条 `grep -rn` 能吃掉整个上下文窗口 | `tools/__init__.py` 无搜索工具；`bash_tool.py:412-429` |
| P0 | 无 stale-write 保护：外部改动被无声覆盖 | — |
| P1 | 空 `old_str` 会通过校验并把整个文件打散 | `file_tools.py:273` → `:280` |
| P1 | 无 diff 返回：模型和用户都看不到这次改了什么 | `ToolResult` 只有 `content: str` |
| P1 | 无 `replace_all` 开关、无 multi-edit；匹配失败没有近似诊断，模型只拿到死胡同错误 | — |
| P1 | 每次 read 都跑一遍 tiktoken 全文编码（而且是错的 tokenizer） | `file_tools.py:41-44` |
| P2 | CRLF 文件被整体转成 LF（整文件 diff）；二进制/目录读取直接抛原始 codec 错误；`offset` 越界返回空但 success | `file_tools.py:271/281`、`:124`、`:128-150` |

### 2.5 Bash 与进程管理（`bash_tool.py`，617 行）

**当前设计**：前台 `create_subprocess_shell` + `wait_for(timeout)`；后台模式返回 `bash_id`，由进程级 `BackgroundShellManager` 单例持有，配一个 readline 监控协程；另有 `bash_output` / `bash_kill`。

这是全仓库设计最认真的工具（后台进程管理确实是 Claude Code 才有的东西），但**安全和正确性问题最密集**：

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | **没有任何权限 / 审批 / 白名单 / 沙箱**——模型想跑什么就跑什么，包括 `rm -rf`、`curl \| sh`、`git push --force` | `agent.py:436` 直接 `await tool.execute(**arguments)` |
| P0 | **超时和 kill 只作用于直接子进程 `/bin/sh`，不是进程组**：`npm run dev`、`pytest -n` 的孙子进程全部存活，`bash_kill` 还返回 success | `bash_tool.py:96-106, 401` |
| P0 | **bash 输出零截断**：一条命令就能撑爆上下文 | `bash_tool.py:412-429` |
| P0 | **无持久 shell 会话**：`cd` / `export` / venv 激活每次调用后全部丢失，而且没有任何东西告诉模型这件事 | `bash_tool.py:225-235` 固定 `cwd=workspace_dir` |
| P0 | `bash_output` **从不报告进程状态**（还在跑 / 已退出），运行中的 shell 返回 `exit_code=0`，与"干净成功"无法区分——description 却承诺返回 status | `bash_tool.py:517-523`、`:42` |
| P1 | 工具名叫 `bash`、文档写 bash，实际执行 `/bin/sh`——Debian/Ubuntu 上 bashism 直接失败 | `bash_tool.py:354/391` `create_subprocess_shell` |
| P1 | 前台超时会**丢弃超时前已产出的全部输出** | `bash_tool.py:398-409` |
| P1 | 继承完整父进程环境，输出零脱敏：一条 `env` 就把 API key 写进 transcript | — |
| P1 | 后台监控严格按行读：没有换行的输出（进度条、`input()` 提示）永远看不见 | `bash_tool.py:146-160` |
| P2 | `BackgroundShellManager` 是**进程级类变量**，ACP 多 session 会互相串；后台缓冲区无上限；CLI 退出不清理后台进程 | `bash_tool.py:111-112`、`cli.py:435-448` |

### 2.6 扩展层：MCP / Skills / Memory

**当前设计**：`mcp_loader.py` 支持 stdio + SSE/HTTP，带连接与执行超时；`skill_loader.py` 扫描 `SKILL.md` frontmatter，元数据注入 system prompt（Level 1），`get_skill()` 按需加载全文（Level 2）——**这套 progressive disclosure 是项目里最贴近 Claude Code 的部分**。

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | **"记忆"是只写的**：`RecallNoteTool` 定义了、导出了，但 CLI **从不注册它**——agent 能写笔记，永远读不回来 | `cli.py:38` 只 import `SessionNoteTool`，`cli.py:431` 只注册它 |
| P0 | **完全没有项目记忆文件**（CLAUDE.md / AGENTS.md 这一层）：agent 每次进一个仓库都是失忆的 | 全仓库无相关加载逻辑 |
| P0 | MCP 工具名**不做命名空间、不做冲突检测**：`agent.py:31` 的 `{tool.name: tool}` 会静默覆盖同名内置工具 | `agent.py:31` + `mcp_loader.py:201` |
| P1 | 单个 server 连接失败可能把**已加载的所有 MCP 工具一起废掉**（错误路径里无保护的 `exit_stack.aclose()`；streamable-HTTP 端点挂掉时抛的是 `CancelledError`，`except Exception` 接不住 → **直接杀死 CLI 启动**） | `mcp_loader.py:219-228`、`:224/:420` |
| P1 | 非文本 MCP 内容被 str() 进 transcript（整个 base64 pydantic repr），`structuredContent` 直接丢 | `mcp_loader.py:99-106` |
| P1 | MCP 启动串行阻塞，无重连、无健康检查、无 `tools/list_changed`；无凭证方案（无 env 展开、无 OAuth） | `mcp_loader.py` 全文 |
| P1 | Skills 只有一个目录、首次匹配优先；没有 user/project/plugin 分层，没有热重载，`allowed-tools` 解析了但**从不生效** | `skill_loader.py:23` |
| P1 | 扩展面只有 skills + MCP tools：**没有 slash command、没有 subagent、没有 hook、没有 plugin** | — |
| P2 | `mcp_loader.py:95/179` 用了 `asyncio.timeout`（3.11+），而 `pyproject.toml:7` 声明 `>=3.10`；CRLF 的 SKILL.md 被判定为"缺少 frontmatter" | `skill_loader.py:74-78` |

### 2.7 入口层：CLI / ACP / Config / System Prompt

| 级别 | 问题 | 证据 |
|---|---|---|
| P0 | **ACP 模式往 stdout 打人类日志**，而 stdout 正是 JSON-RPC 传输通道——协议流被污染（`initialize_base_tools()` 里那一堆 ✅ 全进去了） | `acp/__init__.py:175` → `cli.py:325,329,361,386,405,412,419` |
| P0 | **无会话持久化**：没有 `--continue` / `--resume`，进程一退全没；ACP 也 `loadSession=False` | `acp/__init__.py:89` |
| P0 | **headless 模式在 CI 里不可用**：**全仓库没有一处 `sys.exit`**，配置缺失、API key 无效、任务失败，退出码统统是 0；`--task` 模式连 `Agent.run()` 的返回值都丢掉了；输出是带 ANSI 的散文，无 JSON | `grep -rn sys.exit mini_agent/` → 空；`cli.py:585-595` |
| P0 | **Esc 取消是抢 stdin 的线程**：任何转义序列（方向键、功能键、鼠标）都会触发取消；`asyncio.Event.set()` 从非 async 线程调用；agent task 是轮询 `done()` 而不是 `cancel()`；线程 200ms 内没退出终端就卡在 cbreak 模式 | `cli.py:725-791` |
| P0 | **system prompt 不注入任何环境上下文**（cwd / git 分支 / 平台 / 日期），也不加载任何项目记忆文件 | `config/system_prompt.md` 全文 |
| P1 | Config **无 env / CLI flag 层**，API key 只能明文 YAML；而文件工具没有路径约束，agent 自己就能 `read_file` 这个 key | `config.py:107-111` |
| P1 | Agent 不可嵌入：`Agent.__init__` 要求外部拼好 system_prompt 和 tool 列表，没有 `run(prompt) -> events`，于是每个宿主都得抄一遍 `cli.py:449-575`——ACP 已经抄了 | `__init__.py:3-17` |
| P1 | slash 命令只有 6 个（help/clear/history/stats/log/exit）；没有 `/compact` `/model` `/cost` `/tools` `/diff` `/init`；也没有自定义命令机制 | `cli.py:600` |
| P1 | 输入面缺 `@file` 引用、路径补全、图片粘贴、真多行编辑；任何以 `/` 开头的输入（比如绝对路径）都被当成未知命令吞掉 | `cli.py:659-660` |
| P1 | **system prompt 是能力宣传册，不是操作规范**：它甚至在教模型啰嗦（"Explain your approach before tool execution"、"Summarize accomplishments when complete"），与所有生产 coding agent 的 prompt 方向相反；没有 git 策略、没有并行工具指引、没有"不确定就去查"的硬约束 | `system_prompt.md:62-66` |
| P1 | 打包漏了 `config/*.md` 和 `config-example.yaml`：pip 装出来的 agent **静默退化成一句话 system prompt** | `pyproject.toml:50-51`、`cli.py:551-553` |
| P1 | ACP 无视 config 的 `provider` 永远建 Anthropic client；`{SKILLS_METADATA}` 占位符原样发给模型；ACP 从不清理 MCP 连接（每次重启泄漏一批 stdio 子进程） | `acp/__init__.py:186`、`:181-184`、`:174-189` |
| P2 | 无 diff 渲染 / 无语法高亮 / 结果 300 字符截断；ANSI 无条件输出（管道和重定向也带色）；启动 15 行 ✅ 噪音且无 `--quiet` | `utils/colors.py:4-35` |

### 2.8 工程质量与评测

| 级别 | 问题 | 证据（我本地实跑） |
|---|---|---|
| P0 | **测试结构性无法失败**：`tests/test_agent.py` **0 个 assert**，文件没创建也 `return True`；多个 live 测试 `except Exception → return False`（pytest 眼里仍是 PASS） | `tests/test_agent.py:88-94` |
| P0 | `pytest tests/` **会真花你的 API 额度**：`test_llm.py` / `test_llm_clients.py` / `test_integration.py` 直接读 `config.yaml` 打真实 API，我这边 4 分钟没跑完；干净 clone 上 `test_mcp.py` 的 fixture 读一个 gitignored 文件直接 error | 实测 `timeout 240 pytest -q` → exit 124 |
| P0 | **main 是红的**：`tests/test_acp.py::test_acp_invalid_session` 失败，且暴露真实崩溃——`NewSessionRequest(cwd=None)` 触发 pydantic ValidationError | 实测 `1 failed, 1 passed` |
| P0 | **没有任何 CI**：无 `.github/`，无 workflow | `ls -a` |
| P1 | **没有 eval harness**：没有任何东西度量"这个 agent 到底能不能完成任务、花多少钱、多久" | — |
| P1 | 最大最要害的两个模块 `cli.py` / `agent.py` 基本无测试，包括招牌功能上下文压缩 | — |
| P1 | 无 linter / formatter / type checker（`pyproject.toml` 里 ruff/black/mypy 全无），CONTRIBUTING 指向的风格指南不存在 | `grep ruff\|black\|mypy pyproject.toml` → 空 |
| P1 | 可观测性只有一个非结构化文本日志，每轮一个文件、无上限、缺全部你想要的数字；`pytest`/`pip`/`pipx` 被写进**运行时依赖**；`httpx` / `requests` 声明了但**全仓库没 import** | `logger.py`、`pyproject.toml:11-24` |
| P2 | 版本号散落三处、零 tag、无 changelog；README 有假承诺（"configurable token limit" 实际是硬编码；`README.md:290` 让用户去改 `mini_agent/llm.py` 第 50 行——这个文件不存在） | — |

---

## 3. 可以直接开 issue 的确认级 bug

| # | 位置 | 现象 | 成本 |
|---|---|---|---|
| 1 | `file_tools.py:280` | `edit_file` 全局替换，description 却承诺唯一 | S |
| 2 | `file_tools.py:273` | 空 `old_str` 通过校验，打散整个文件 | S |
| 3 | `file_tools.py:111-114,198-201,259-262` | 绝对路径 / `..` / symlink 逃逸 workspace | M |
| 4 | `agent.py:186-233` | 压缩不幂等，压过一次后永远压不动 | M |
| 5 | `agent.py:249-283` | 摘要 prompt 用未截断工具输出（`:259` 的 `...` 是假的） | S |
| 6 | `agent.py:289-292` | 摘要失败返回原文，压缩反而变长 | S |
| 7 | `agent.py:73-94` | 取消删掉已落盘的工具记录 | S |
| 8 | `agent.py:358-360` + `cli.py:245-246` | "API Tokens Used" 报的是最后一次调用 | S |
| 9 | `bash_tool.py:401` / `:96-106` | kill 只杀 `/bin/sh`，进程树存活却报 success | M |
| 10 | `bash_tool.py:398-409` | 前台超时丢弃已产出的输出 | S |
| 11 | `bash_tool.py:517-523` | `bash_output` 不报状态，运行中返回 exit_code=0 | S |
| 12 | `cli.py:756-763` | 方向键触发取消 | S |
| 13 | `cli.py` 全文 | 无 `sys.exit`，所有失败路径退出码 0 | S |
| 14 | `cli.py:38,431` | `RecallNoteTool` 从不注册，记忆只写不读 | S |
| 15 | `acp/__init__.py:111` | 未知 sessionId → ValidationError 崩溃（红测试） | S |
| 16 | `acp/__init__.py:175/181/186` | stdout 污染 JSON-RPC / 占位符原样下发 / 无视 provider | S |
| 17 | `openai_client.py:221-224` | `hasattr(dict, "text")` 恒 False，OpenAI 路径 thinking 永远丢 | S |
| 18 | `openai_client.py:231` | 畸形 tool-call 参数 → JSONDecodeError 崩掉整个 run | S |
| 19 | `mcp_loader.py:219-228` | 一个 server 失败连累全部；`CancelledError` 杀死启动 | M |
| 20 | `pyproject.toml:50-51` | 未打包 system_prompt.md → pip 安装版静默降级 | S |

> 建议：**不要**把这 20 个全修完再谈架构。信号在 15 个左右饱和；剩下的写进 `KNOWN_ISSUES.md` 做好分诊——一份分诊清晰的 issue 列表本身就是正面信号。

---

## 4. 与 Claude Code / Codex 的能力对照

| 维度 | Mini-Agent 现状 | Claude Code / Codex 的做法 | 差距性质 |
|---|---|---|---|
| 内核 | `run()` 里 print，返回 str | 事件流（`AsyncIterator[Event]`），CLI/IDE/SDK/eval 都是消费者 | **架构** |
| 上下文 | 一次性散文摘要，不幂等 | 分层压缩 + 逐字尾巴 + 结构化状态；server-side context editing | **架构** |
| 缓存 | 无 | system/tools/rolling 三个 cache breakpoint，命中率 >80%（厂商公开材料的量级参考，非本端点实测；本端点能不能做见 C1/C2，待测） | 成本 |
| 权限 | 无 | 解析 argv 的规则引擎 + ask/allow/deny + 会话内记忆 + sandbox 降级 | **安全** |
| 沙箱 | 无 | macOS seatbelt / Linux landlock+seccomp；沙箱内自动放行 | **安全** |
| 回滚 | 无 | 每轮 checkpoint + `/rewind`；aider 用 auto-commit | **安全** |
| 检索 | 无 glob / 无 grep，靠 bash | Glob/Grep 工具 + 子 agent 扇出（aider 用 tree-sitter repo map） | 能力 |
| 编辑 | 单文件 str.replace | MultiEdit / apply_patch，跨文件事务性、按上下文行匹配 | 能力 |
| 反馈 | 编辑后无校验 | LSP / `ruff --output-format=json` 诊断自动回灌 | 能力 |
| 委派 | 无 | Task/subagent，独立上下文，只回结论 | 能力 |
| 计划 | 无 | plan mode 是状态机：禁写工具 + `exit_plan_mode` 审批 + 计划钉在上下文 | 能力 |
| 打断 | Esc 杀整轮 | 边跑边排队用户输入（steering），下一步注入 | 交互 |
| 会话 | 无 | JSONL session store，`--continue` / `--resume` | 交互 |
| 记忆 | 只写不读的 note | CLAUDE.md / AGENTS.md 分层项目记忆 | 能力 |
| 扩展 | skills + MCP tools | skills + MCP + slash command（.md 文件）+ subagent + hooks + plugins | 生态 |
| 网络 | 无 | WebFetch / WebSearch（受限、可截断、可小模型预压缩） | 能力 |
| 多模态 | 无 | 截图粘贴 / 图片工具结果 | 能力 |
| 观测 | 一个文本日志 | 结构化事件 + OTel span + cost/latency | 工程 |
| 度量 | 无 | 内部 eval + SWE-bench 类基准 | **可信度** |

---

## 5. 关键缺失 Top 10（按"解锁多少后续能力"排序，不是按疼痛排序）

1. **事件化内核**（`run()` → `AsyncIterator[AgentEvent]`，单一 tool dispatch 点）
   → 一次性解锁：流式、权限门、hooks、检查点、session 持久化、SDK、ACP 正确性、可测试性。**上面 6 个 P0 塌缩成这一个。**
2. **上下文管理器**（分层驱逐/摘要 + 逐字尾巴 + 文件账本 + prompt cache）→ 长任务能不能做完的分水岭，也是最好的简历故事。（prompt cache 那一半依赖 C1–C3，待测；不支持时前三层照做，阈值改由纯上下文预算推导。）
3. **FileTracker / 文件状态账本**（`path → (sha, mtime, last_message_idx)`）→ 一个对象喂四个消费者：新鲜度校验、上下文去重、检查点打标、diff 渲染。
4. **权限引擎 + 沙箱降级**（沙箱不是权限的补充，**沙箱是让自动放行可行的前提**，否则用户第二天就 `--dangerously-skip-permissions`）。
5. **检查点 / rewind**（shadow git：`git --git-dir=~/.mini-agent/checkpoints/<sid> --work-tree=<ws>`，不碰用户仓库）→ 比权限更便宜，因为不需要任何 UI 决策。
6. **eval harness + 失败分类**（没有它，后面每一条改进都只是"我写了代码"）。
7. **Session store + resume**（同时解锁 eval 复现、`--continue`、ACP `loadSession`）。
8. **glob / grep 工具 + bash 输出截断**（这两个是一体的：没有前者就会用后者，而后者无上限）。
9. **项目记忆（AGENTS.md/CLAUDE.md）+ 环境上下文注入**（cwd / git 分支 / 平台 / 日期）——成本极低，效果立竿见影。
10. **子 agent（只读探索者）+ 并行工具执行**——但**必须带数字**才有意义（"父上下文增长 -62%，pass@1 不变"）。

**依赖顺序**（这是审计里最容易被忽略的一点）：

```
事件化内核 ──┬─► 流式 / 打断
             ├─► 权限门 + hooks ──► 沙箱降级
             ├─► 检查点 ──► /rewind
             ├─► session store ──► resume / ACP loadSession
             └─► SDK 门面 ──► eval harness ──► 所有数字
FileTracker ─┴─► 上下文账本 / 去重 / diff
```

---

## 6. 演进路线（每个阶段都以"可演示 + 可量化"收尾）

### Phase 0 — 可信度分诊（一个周末，无简历 bullet，是入场券）

- 修红测试（`acp/__init__.py:111`）；删掉 `try/except → return False` 让测试**能失败**；
- `conftest.py` 加 `live` marker + `pytest-timeout`，让干净 clone **离线 15 秒内跑绿**（现在是烧 API 额度 5 分钟）；
- 补 `sys.exit` 退出码；`--task` 模式把结果和错误吐出来；
- 修 README 假承诺（token limit 可配、不存在的 `mini_agent/llm.py:50`）；
- 加 GitHub Actions（ruff + pytest offline）；
- 写归属段落 + `ARCHITECTURE.md` 骨架。

**验收**：`git clone && pytest` 绿、离线、<15s；CI badge 绿。

### Phase 1 — 让一切可度量（2 周）

- `ScriptedLLM` 假客户端（可以从 `tests/test_acp.py:21-40` 泛化），把 `agent.py` 覆盖率从 ~34% 提到 ~75%，重点测循环、压缩、重试，**不要去测 banner**；
- `evals/`：20~24 个手写任务（修一个失败的 pytest / 加一个 CLI flag / 跨 4 文件重命名 API / 按 stack trace 定位 bug），每个任务带 `setup`（repo + SHA）和 `verify`（shell 断言），在 git worktree 里跑；
- 每个任务输出 `{task_id, passed, steps, prompt_tokens, completion_tokens, usd, wall_clock_s, tool_error_rate, edit_precision, compaction_count}`；
- 发布基线，**连失败一起发**：`evals/FAILURES.md` 做根因分类。

> **Resume bullet**: *Built a 24-task evaluation harness for a coding agent (git-worktree isolation, shell-verified assertions, per-task token/cost/latency capture) and used it to gate every subsequent change; established a X% pass@1 baseline and a root-cause taxonomy of all N failures.*

### Phase 2 — 事件化内核 + 流式 + 会话恢复（2~3 周，地基）

- `run()` 改成 `AsyncIterator[AgentEvent]`（`StepStart / TextDelta / ThinkingDelta / ToolStart / ToolEnd / Compaction / TurnEnd`）；
- 所有 `print()` 迁到 `ConsoleRenderer`；`AgentLogger` 和新的 JSONL `SessionStore` 变成事件消费者；
- **删掉 ACP 那份复制的循环**，`_run_turn` 塌缩成 ~40 行 event → `session_notification` 映射（顺带把它丢掉的压缩/日志/取消都拿回来）；
- `client.messages.stream()`，Esc 走 `loop.call_soon_threadsafe` + `task.cancel()`，并把用户输入排队（steering）而不是丢弃；
- `--resume` / `--continue`，ACP 打开 `loadSession=True`；
- 顺手加 `MiniAgent(workspace=...)` 门面 + `async for ev in agent.run(prompt)` —— SDK 面几乎是白送的。

> **Resume bullet**: *Refactored a 4k-LOC agent from a print-coupled monolith into an async event-stream core, collapsing two divergent copies of the execution loop into one engine (−N LOC; the editor backend regained context management and per-tool cancellation it had silently lost); added token streaming, sub-100ms interrupt, mid-run steering, and JSONL session resume.*

### Phase 3 — 上下文工程（3 周，**旗舰**）

- `mini_agent/context.py`：`ContextManager.maybe_compact(messages, usage)` 三层
  1. **tool 结果驱逐**：把旧 tool 消息 content 改写成 `[tool result cleared: 12.4KB, re-read if needed]`，**保留 role / tool_call_id**，pairing 不破；
  2. **窗口摘要**：只摘 `messages[1:-K]`，保留最近 K 条逐字，边界对齐到不会孤立 `tool_use`；摘要用固定小节（files touched / established facts / current step / next actions）；
  3. **FileLedger** 逐字重注入，文件状态永不经过 LLM 有损压缩；
- 触发条件改用 `usage.prompt_tokens`（真实值），不再用不含工具 schema 的 tiktoken 估算；`token_limit` 接进 config，按模型注册表取窗口；
- prompt cache breakpoints（system / tools / rolling），把已经解析出来却被丢掉的 `cache_read_input_tokens` 显示出来 —— 依赖 C1–C3（待测）；若不支持：断点代码保留但空转，阈值改由纯上下文预算推导，报告只报 token 数并写明本端点不支持；
- 不变量测试：**任何压缩路径都不能产生孤立的 tool_use**；
- eval 加长任务集，出 before/after。

> **Resume bullet**: *Rebuilt context management from single-pass prose summarization into a three-tier manager (tool-result eviction → windowed summarization with a verbatim tail → structured file-state ledger), lifting long-horizon pass@1 from X%→Y% and cutting tokens/task Z% via prompt-cache breakpoints at an N% cache-read rate.*
>
> （X/Y/Z/N 全部待本端点实测；prompt-cache 那半句要等 C1–C3 验证通过才能写，不通过就把后半句删掉。）

### Phase 4 — 安全：权限 + 沙箱 + 检查点（2~3 周）

- `mini_agent/permissions/`：**按解析后的 argv 结构匹配**（不是正则匹配命令字符串），复合命令取最严判定，无法解析则 fail-closed，会话内 "always allow" 记忆，项目级 YAML 规则；
- 单一门位置（Phase 2 已统一 dispatch），CLI 渲染成 y/n/always，ACP 用 `session/request_permission`（连接对象在 `acp/__init__.py:81` 已经拿着，一直没用）；
- `sandbox.py`：macOS `sandbox-exec` / Linux `bwrap`，**沙箱内的命令判定从 ask 降级为 allow**；
- 文件工具路径收敛（`realpath` + workspace 前缀 + symlink 检查），config 路径加入 deny list；
- `checkpoint.py`：shadow git 仓库，每次 mutating 工具前 commit，`/rewind [n]` 恢复文件并截断消息；
- 40 条对抗用例（`r''m -rf`、`$(...)`、`curl|sh`、`../../`、symlink 逃逸），报告拦截率。

> **Resume bullet**: *Designed a fail-closed permission engine matching on parsed command structure rather than raw-string regex — blocking 40/40 adversarial bypasses that a regex allowlist admits — paired with OS-level sandboxing that downgrades ask→allow for workspace-confined commands, and per-turn shadow-git checkpoints with one-command rewind.*

### Phase 5 — 吞吐与检索（2 周）

- `Tool` 上加 `concurrency_safe` 元数据；**先**把文件工具的阻塞 IO 塞进 `asyncio.to_thread`（否则 `gather` 是装饰性的——`file_tools.py:124/271` 只是名义 async），再并行执行只读工具；
- 加**带上限**的 `glob` / `grep` 工具 + bash 输出截断（头尾保留 + 中间省略 + 提示总行数）；
- 只读探索子 agent（独立上下文，只回结论）；
- 可选加分项、也是最出彩的 150 行：**tree-sitter repo map**（按符号引用排序，预算内渲染 `path: class Foo / def bar()`），它比一次糟糕的 `grep -rn` 还省 token。

> **Resume bullet**: *Added concurrency metadata and non-blocking I/O so model-requested parallel tool calls actually execute concurrently, plus capped search tools and read-only exploration subagents: median task wall-clock −X%, parent-context growth on exploration-heavy tasks −Y%, pass@1 unchanged.*

### 如果只有 4 周，只做三件事

**Phase 0（周末）→ Phase 1（2 周）→ Phase 3 的第 1、2 层（1.5 周）**。
理由：可信度 + 一个数字 + 一个真正的架构故事。事件化内核可以留到之后，但**必须在简历里说清楚它是下一步以及为什么**——能讲清依赖顺序本身就是信号。

---

## 7. 陷阱（看起来像进步，实际不是）

1. **把整个仓库说成自己的**。最高代价的错误，没有之一。
2. **没有 baseline 的数字**。"68% pass rate" 单独出现是不可读的——每个数字都要有 before、任务数、git SHA。
3. **跑完整 SWE-bench**。几周 Docker 管道 + 几百刀，换一个没人信的数字。24 个你能逐条讲清的自建任务 > 一个你答不上来的 SWE-bench-lite 分数。
4. **做漂亮 TUI**（Rich/Textual、live diff、语法高亮）。几周工作量，零面试信号。**事件流后面一个朴素 renderer，比焊死在引擎里的华丽 renderer 得分更高。**
5. **补齐 MCP 协议全集**（resources/prompts/sampling/roots/OAuth+DCR）。读起来像集成工作，不像 agent 工作。只修命名空间冲突和 `CancelledError` 杀启动这两个（2 天），其余搁置。
6. **迁移到 LangGraph / pydantic-ai**。这会删掉这个项目唯一有意思的地方：**你拥有那个循环**。
7. **把覆盖率当目标**。`cli.py` 12% 不该靠测 banner 来解决。测循环、压缩器、权限引擎、重试策略。
8. **修完全部 60 个 bug**。信号在 15 个左右饱和，之后是在给别人的 demo 做免费 QA。
9. **Phase 1 之前调 prompt**。改 `system_prompt.md` 感觉很有产出，但在没有 eval 之前是不可证伪的。放到 Phase 3 当作一行 ablation：`prompt rewrite: +7pp` 才叫结论。
10. **把 multi-agent 当项目卖点**。没有上下文缩减数字支撑的"多智能体编排"读起来就是 hype。

---

## 8. Day-1 清单（今天就能动，全是 S 成本）

- [ ] `tests/test_acp.py` 修红 + `NewSessionRequest` 崩溃修掉
- [ ] `conftest.py` + `live` marker，让默认 `pytest` 离线可跑
- [ ] `file_tools.py:280` → 唯一性检查 + `replace_all` 显式参数；空 `old_str` 拒绝
- [ ] `cli.py` 全线补 `sys.exit(1)`；`--task` 打印/返回真实结果
- [ ] `cli.py:431` 注册 `RecallNoteTool`（一行，让"记忆"名副其实）
- [ ] `agent.py:358-360` 改成累加，修掉 CLI 里那个错的 token 数字
- [ ] `bash_tool.py`：输出截断（头 200 行 + 尾 200 行 + 省略提示）
- [ ] `acp/__init__.py`：日志改 stderr、替换 `{SKILLS_METADATA}`、按 config 选 provider
- [ ] system prompt 注入环境块（cwd / 平台 / 日期 / git 分支 / `git status` 前 20 行）
- [ ] `pyproject.toml`：把 `pytest`/`pip`/`pipx` 移出运行时依赖；补 `config/*.md` 打包；加 ruff 配置
- [ ] 新建 `KNOWN_ISSUES.md`，把本文第 3 节里你不打算马上修的条目分诊进去

---

## 附：判断标准

每做完一个阶段，问三个问题：

1. **它能被一句带数字的话描述吗？** 不能 → 说明缺 eval，或者做的是 Tier-C 的事。
2. **它是解锁了后续能力，还是只是修好了一个东西？** 前者优先。
3. **面试官问"为什么这么设计、你否决了什么方案"，我能答吗？** 答不上来的设计不要写进 `ARCHITECTURE.md`——被否决的备选方案那一段，是你会写下的信号密度最高的文字。
