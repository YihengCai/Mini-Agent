# Mini-Agent：从演示项目到现代 coding agent

这是我的 coding agent 学习项目。

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供了约 4.1k LOC 的 agent loop、文件与 shell 工具、CLI、MCP/skills 加载器、日志和 ACP 适配器；我会在这条真实循环上研究现代 coding agent 的核心设计问题。

目标不是堆功能，也不在实现前宣称 SOTA。每个模块都要回答三个问题：当前代码怎样失败，正确边界在哪里，哪个一分钟内可运行的离线回归测试能检出关键实现缺失。

## 当前状态

项目已在上游 baseline 上完成一组可离线验证的核心改造：agent loop 已移入不依赖终端的 `mini_agent/core/`，执行生命周期拆成 Session、Turn 与 Step；模型调用通过中性 contract 和显式 wire adapter 隔离协议差异；模型工具调用统一经过冻结注册、批次预检、串行执行和输出预算；后台 shell 与 MCP 连接由一次 CLI runtime 持有并回收。旧本地压缩、ACP 和没有错误分类依据的项目级 retry 已删除。文件工具、工作区事实、配置来源、事件快照和资源关闭等已完成边界，以代码、测试和 ADR 为准，不把项目包装成 product-ready coding agent。

`read_file` 现在返回 1-based 行窗口，编号正文最多 2000 个完整行或 50 KiB，并给出下一次 `offset`；`edit_file` 仅把 LF/CRLF 视为等价，其他文本必须精确匹配且只能出现一次。写入和编辑通过同目录临时文件和 `os.replace()` 提交，已有文件保留 CRLF 约定与权限位。代码可以运行，但仍保留重要限制：

- 两份上游真实模型演示仍会吞异常或用返回值代替断言；它们和 5 项外部 MCP/网络测试已从默认集合排除，显式运行也不构成稳定回归；
- `TurnOutcome` 只解释 core 为什么交还控制权，不判断用户任务是否完成；目前没有 TaskSupervisor、BenchmarkEvaluator 或 SWE-bench 接入；
- core 事件目前只是带 Session、Turn、Step 身份的进程内同步通知，不是可持久化、可回放的轨迹格式，也还没有独立统计或基准评测消费者；
- Esc 没有真正取消正在运行的模型或工具任务；中断只在完整 Step 边界生效，延迟可能覆盖一次模型调用和整批工具执行；
- 工具批次目前只做结构预检、串行执行和单条模型消息预算，没有 JSON Schema 参数校验、权限、自动重试或整批合计预算；调用标识符账本只存在于进程内，取消后不生成可恢复的模型可见事实；
- 后台 shell 会在 CLI 运行时退出时收敛监控任务，前台 shell 会在超时或取消时终止并等待直接子进程；两者仍没有进程组或后代进程清理，后台输出缓冲、原始事件与日志也无容量预算，不构成权限或沙箱边界；
- MCP 超时与连接现在按 CLI runtime 隔离并串行关闭，但仍没有重连、并行连接、权限或真实网络能力验证；
- 当前只实现 Anthropic-compatible messages 与 OpenAI-compatible chat completions 的非流式基础 adapter；名称只表示 wire 格式，没有运行真实端点验证，也没有统一错误分类，`finish_reason` 是可空的 adapter 原生元数据；
- `usage` 只作为观察数据，不参与上下文控制；当前没有自动上下文预算或压缩，完整历史会持续增长并可能触及配置端点的上限；
- 文件工具仍接受绝对路径和解析到工作区外的路径，也没有读取版本回执或并发覆盖检测；
- Note 读写现在共享最小 JSON 结构校验，但保存仍是无锁的整文件直接写入，没有原子提交、并发更新或容量预算；`recall_notes` 也尚未进入 CLI 工具表；
- 没有权限引擎、工作区边界限制、操作系统沙箱、跨文件回滚或检查点。

不要把当前版本用于重要仓库或无人监督执行。当前已实现范围以本节、代码和离线测试为准。

## 当前学习重点

LLM 测试替身已经落地：所有模型调用共用一条严格 FIFO 脚本；响应不足、响应剩余、首个违规被捕获和工具调用配对错误都会使测试失败。测试会记录模型实际收到的消息和工具定义，并已覆盖真实工具循环、工具失败和最大步数。删除摘要调用后，用途标签已经随 ADR-0001 一起由 [ADR-0006](docs/decisions/0006-remove-legacy-local-compaction.md) 推翻。

默认测试入口也已经收拢：真实模型测试使用模块级 `external` marker，MCP 混合模块只标记 5 项会读取用户配置、连接服务或访问网络的测试；默认配置与根级收集门共同排除它们，普通 marker 过滤不能绕过，拼错 marker 会直接使收集失败。当前该模块的 27 项纯离线测试仍在默认集合中；取舍见 [ADR-0007](docs/decisions/0007-explicit-opt-in-for-external-tests.md)。

Turn 日志也不再以秒级名称覆写已有证据：普通文件名保持不变，同名时由排他创建和 `_1`、`_2` 等后缀分配新文件；文件名与表头来自同一次时钟采样。它仍只是人类可读的追加日志，没有结构化回放、保留策略、脱敏或崩溃刷盘保证；取舍见 [ADR-0019](docs/decisions/0019-exclusive-turn-log-allocation.md)。

文件工具改造也已经落地：读取预算、续读提示、歧义拒绝、CRLF、权限位和原子替换失败都有离线回归；删除唯一匹配判断、`os.replace()` 或超长行早停时，对应测试会转红。取舍见 [ADR-0002](docs/decisions/0002-bounded-and-atomic-file-tools.md)。

核心边界改造已经落地：消息、模型调用和终止判断位于 `mini_agent/core/agent.py`，模型响应触发的工具调用位于 `mini_agent/core/tool_execution.py`；`mini_agent/cli_events.py` 消费同一条事件序列完成终端渲染与原有文本日志。不给 `event_sink` 时，真实 agent loop 可以无终端输出、无日志副作用地运行。事件顺序、CLI 输出与日志调用都有离线回归；core 没有自动压缩状态，也不导入 UI、日志或传输模块。

执行生命周期改造也已经落地：`AgentSession.start_turn()` 原子接纳输入并返回 `TurnHandle`，同一 Session 只允许一个活动 Turn；`TurnOutcome` 区分模型交回控制权、用户中断、Step 上限和失败，但没有 `success` 或 `completed`。工具调用继续同一 Turn；事件载荷使用独立快照，Turn 配置在接纳时固化，接收器失败也不会留下缺少工具结果的历史。配置与公开 Session 构造入口都要求 `max_steps > 0`，并在 runtime 资源或工作区副作用之前失败，因此不存在零模型请求的合法 Turn。Session 还会在调用者提示词后附加本次绝对工作区的完整事实块；普通标题或旧路径不能抑制它，已含准确块时不重复。CLI 分别显示 Turn 的控制权边界和内部 Step，把 `end_turn` 写成中性的“交还控制权”，不显示任务成功标记。生命周期取舍与中断延迟见 [ADR-0004](docs/decisions/0004-session-turn-step-lifecycle.md)，预算边界见 [ADR-0014](docs/decisions/0014-positive-step-budget-at-config-and-core.md)。

工具批次强制点已经落地：Session 创建时拒绝空名、重名和不合 contract 的工具元数据，并冻结模型定义与调度键；agent loop 在 `ModelClient` 返回处先深拷贝整个响应，再让事件、预检、执行器和历史消费。每个模型批次在首个副作用前完成结构预检，再按模型顺序串行执行。跨 Step/Turn 重复调用标识符会以 `tool_protocol_error` 拒绝，未知工具、普通异常和非法返回则各自产生同序失败结果；合法 `ToolResult` 在接纳处立即深拷贝，工具保留的返回别名不能改写事件或历史。assistant 工具调用与全部结果仍一次性提交。模型客户端、工具参数、工具结果、事件和历史互不共享可变批次对象。这个入口只约束模型响应触发的调用；可信宿主仍持有原始 Tool 引用，因此它不是权限或沙箱。批次取舍见 [ADR-0008](docs/decisions/0008-session-owned-tool-batch-executor.md)，返回值所有权见 [ADR-0025](docs/decisions/0025-executor-owns-admitted-tool-results.md)。

模型可见工具输出预算已经落地：批次执行器在完整 `ToolFinished` 发出后，才把成功内容或带 `Error: ` 前缀的失败文本投影成最多 64 KiB 的模型消息；超限时保留合法 UTF-8 首尾，并报告原始、保留、省略和上限字节数。Session 历史与下一次模型请求使用同一个有界视图，原始工具结果、事件和日志不被改写。该预算逐条生效，不限制整批合计，也没有让省略正文可恢复；取舍见 [ADR-0010](docs/decisions/0010-model-facing-tool-output-budget.md)。

shell 所有权已经落地：配置与模型客户端构造成功后，CLI 创建一个管理器并显式注入 `bash`、`bash_output` 和 `bash_kill`；`/clear` 只替换逻辑对话，退出时才按 shell、MCP 顺序清理。管理器拒绝重复标识符和关闭后登记，等待监控任务取消与强杀后的后台子进程，并保留失败项供后续关闭重试。自然完成的监控任务会继续读取到 stdout EOF，再等待进程并发布状态，短进程退出后仍在管道中的行不会丢失。前台调用则局部持有自己的子进程：超时或取消在返回或传播前统一终止并等待，不把它登记成后台资源。主动终止期间的尾部输出、内部容量和进程组仍无保证。后台所有权取舍见 [ADR-0009](docs/decisions/0009-runtime-owned-background-shells.md)，输出完成边界见 [ADR-0023](docs/decisions/0023-background-shell-completes-after-stdout-eof.md)，前台中断回收见 [ADR-0026](docs/decisions/0026-foreground-shell-reaps-on-interruption.md)。

MCP 运行时所有权也已经落地：CLI 用配置构造不可变超时快照，并把同一个 `MCPManager` 交给 loader 与最终清理；不同 runtime 不共享超时或连接。manager 在连接建立前登记所有权，串行化加载与关闭，尝试全部连接并保留失败项；叶子连接只有在 transport 关闭成功后才丢弃句柄，所以取消后的关闭可以真实重试。`isError` 的非空 server 正文现在映射到内部 `error`，因此 `ToolFinished`、模型消息、CLI 和日志使用同一诊断；空正文仍使用通用兜底。MCP `type` 只有在字段完全缺失时才自动推断；显式非法值在构造任何连接前隔离当前 server，合法后续项继续加载。运行时所有权取舍见 [ADR-0011](docs/decisions/0011-runtime-owned-mcp-connections.md)，transport 校验取舍见 [ADR-0016](docs/decisions/0016-reject-explicit-invalid-mcp-transports.md)。

配置文件现在直接使用 `llm`、`agent`、`tools` 三个运行时分组；加载器只检查 YAML 根是映射，随后交给同一套严格模型。未知字段在任意层级都会失败，`agent` 与 `tools` 的缺省值也只存在于模型中，不再维护根级字段分片或逐字段迁移分支。取舍见 [ADR-0028](docs/decisions/0028-config-file-matches-runtime-model.md)。

运行时工作区现在由 CLI 单一持有：显式 `--workspace` 优先，否则使用当前目录，再把同一路径交给工具和 `AgentSession`。配置不再声明 `workspace_dir`，旧字段由严格模型拒绝；工作区仍通过 `--workspace` 选择。这没有新增工作区越界限制或沙箱；取舍见 [ADR-0024](docs/decisions/0024-cli-owns-runtime-workspace.md)。

配置伴随文件的来源也已经统一：CLI 选择 `config.yaml` 后把路径显式传给同一次 runtime，相对 `system_prompt_path` 与 `mcp_config_path` 只从该目录解析，显式绝对路径保持不变。相对文件缺失时不会跨目录借用同名文件，而是使用内置提示词或保持 MCP 未加载；`skills_dir` 没有随本项改变，工作区配置的后续删除见 ADR-0024。取舍见 [ADR-0015](docs/decisions/0015-bind-config-companions-to-selected-source.md)。

Skill 发现的状态边界也已经收紧：每次递归扫描先按路径排序并构建局部注册表，重名会明确报告两个来源，只有完整成功后才替换当前快照；删除文件后的重扫不会留下陈旧能力，失败扫描也不会发布部分结果。更严格的 YAML frontmatter（文件头元数据）结构校验、`allowed-tools` 强制、动态监视、来源优先级和信任/权限模型仍未实现；取舍见 [ADR-0020](docs/decisions/0020-transactional-skill-discovery.md)。

模型调用当前不做项目级 retry：两个 SDK 都显式设置 `max_retries=0`，每个 adapter 只发起一次项目级调用；异常对象和文本原样进入 core。只有在跨协议错误分类、端点证据和离线评测能说明哪些失败可安全恢复后，retry 才会作为独立 topic 重新进入。删除取舍见 [ADR-0027](docs/decisions/0027-no-project-retry-before-error-classification.md)，core 错误边界见 [ADR-0022](docs/decisions/0022-core-preserves-model-error-semantics.md)。

Note 存储失败关闭也已经落地：`record_note` 与 `recall_notes` 共用同一个读取入口，只有文件不存在才表示空状态；已有文件必须是 JSON 对象数组。读取、解码、解析或最小结构校验失败时，两个工具都返回失败，写入不会开始，原始字节保持不变。它还没有解决直接整文件写入、并发更新、容量预算或读取工具注册；取舍见 [ADR-0013](docs/decisions/0013-fail-closed-note-storage.md)。

模型 API 边界改造已经落地：core 只通过 `ModelClient` 调用模型，并把中性 `ToolDefinition` 与现有内部消息结构交给 adapter；静态注册表依据 `llm.adapter` 选择具体 wire 编解码。配置必须提供 API key、原样端点、模型和输出上限，未知 adapter 或旧 `provider` 字段都会失败；项目不会根据域名拼接路径，也不默认启用未经探测的推理状态续传、缓存计量或服务端扩展。共享 schema 已删除永远为空且无法往返的 `thinking` 字段。SDK 自带 retry 已关闭，项目在统一错误分类前也不自动重试。取舍见 [ADR-0005](docs/decisions/0005-explicit-model-api-adapters.md)、[ADR-0027](docs/decisions/0027-no-project-retry-before-error-classification.md) 与 [ADR-0029](docs/decisions/0029-remove-unprobed-thinking-field.md)。

ACP 没有真实外部客户端，也没有覆盖 JSON-RPC、stdio 或连接生命周期的端到端测试；继续维护它只会让协议层提前塑造执行框架。因此当前版本主动删除 ACP，而不是把 CLI 改成 ACP 客户端。重新引入协议层的条件见 [ADR-0003](docs/decisions/0003-remove-acp-and-extract-core-loop.md)。下一项工作尚未选择；必须先按 [BUILD_LIST](docs/BUILD_LIST.md) 的条件找到当前失败证据和一分钟内的离线验证。

## 设计原则

### 自己掌握循环

不引入 LangGraph、pydantic-ai 等 agent 框架。这个项目最值得学习的部分，就是状态怎样经过模型调用、工具调用、中断、上下文选择和恢复。

### 每项改造都要证明测试敏感性

“实现了沙箱”不是结论，真实的操作系统拒绝测试才是。对普通改造，也要证明删除关键实现会让对应离线回归测试转红。

### 模型服务能力先探测再依赖

项目只使用本地配置明确指定的端点与 wire adapter，不预设模型服务。协议格式兼容不代表支持同名 vendor 或其扩展；当前实现需要依赖某项能力时，必须先把实测结果记录到[模型服务能力记录](docs/PROVIDER_CAPABILITIES.md)。

### 安全功能不发布半成品

普通学习模块可以逐步演进；沙箱和检查点不行。只有通过真实的平台与文件测试矩阵后才能标为“已验证”，否则只保留设计与限制。

### 记录原因

代码和测试证明改造能工作；ADR 记录实现过程中真正做出的取舍，PITFALL 记录亲历且可复现的错误假设。它们不会为未来阶段预写。

## 仓库结构

```text
mini_agent/core/             UI 无关的 agent loop 与进程内事件 contract
mini_agent/core/tool_execution.py 模型工具的冻结注册、批次预检与串行执行
mini_agent/core/tool_output.py 模型可见工具结果的 UTF-8 字节预算
mini_agent/cli.py            终端输入、中断轮询和运行时组装
mini_agent/cli_events.py     终端渲染与原有文本日志的事件适配器
mini_agent/agent.py          AgentSession 的公开导入层
mini_agent/llm/protocol.py   core 使用的中性模型 contract 与工具定义
mini_agent/llm/factory.py    显式 wire adapter 注册表与组装入口
mini_agent/llm/              具体协议的 SDK 传输与 wire 编解码
mini_agent/                  工具、MCP/skills 与配置
tests/                       上游测试、LLM 测试替身与离线回归
docs/BUILD_LIST.md           当前工作与可选研究主题
docs/UPSTREAM_AUDIT.md       上游代码审计
docs/specs/                  仅当前实现的短规格
docs/decisions/              ADR
docs/PITFALLS.md             实现过程中亲历的踩坑
docs/reference/              外部 coding agent 源码调研
docs/PROVIDER_CAPABILITIES.md 端点能力探测结果
```

## 运行项目

要求 Python 3.10+，推荐使用 `uv`：

```bash
git submodule update --init --recursive
uv sync
cp mini_agent/config/config-example.yaml mini_agent/config/config.yaml
```

配置文件根级直接使用 `llm`、`agent`、`tools`。旧扁平配置需要把 `adapter`、`api_key`、`api_base`、`model`、`max_output_tokens` 移入 `llm`，把 `max_steps`、`system_prompt_path` 移入 `agent`，并删除 `provider`、`local_compaction_token_limit`、`workspace_dir` 与 `retry`；其他未知字段也会拒绝加载。工作区请使用 CLI 的 `--workspace`。`llm.adapter` 当前可选 `anthropic` 或 `openai`，只选择 wire 格式；`api_base` 会逐字交给对应 adapter。模板中的占位值故意不能直接运行。`config.yaml` 与 `mcp.json` 含密钥，已被 `.gitignore` 排除，不要提交。

### 交互式手动体验

从项目根目录准备一个固定、可丢弃的手动测试区：

```bash
mkdir -p workspace
```

根目录下的 `workspace/` 已被 `.gitignore` 排除，其中的实验文件不会进入提交。以后统一从项目根目录启动，并显式传入这个工作区，避免把仓库本身当作 agent 的操作目录：

```bash
uv run mini-agent --workspace ./workspace
```

只做启动冒烟测试时，看到交互提示符后立即输入 `/exit`；没有提交任务就不会发起模型生成请求。启动过程仍会读取本地配置、初始化工具，并连接已经配置的 MCP server。完整手动体验则直接在提示符中输入任务，agent 会使用当前配置的端点。

提交一次非交互 Turn：

```bash
uv run mini-agent --workspace ./workspace --task "inspect the project and explain its agent loop"
```

非交互 Turn 会访问当前配置的真实端点，可能产生费用；它在 core 交还控制权后退出，但不据此判断任务成功。离线回归请使用下方测试命令。

查看日志：

```bash
uv run mini-agent log
```

## 离线测试

默认入口只运行离线集合，不读取用户模型/MCP 配置，也不访问真实端点或网络：

```bash
.venv/bin/python -m pytest -q
```

显式排除 `external` 的完整集合在 2026-08-26 最近一次实测为 `285 passed, 9 deselected in 13.46s`，没有产生警告。显式外部入口是 `.venv/bin/python -m pytest --run-external -m external -q`；它可能访问真实端点、启动已配置的 MCP server、修改外部状态并产生费用，本次没有执行。只写 `-m external` 不会绕过收集门。

## 文档入口

- [BUILD_LIST](docs/BUILD_LIST.md)：当前工作、可选研究主题及其选择条件
- [上游审计](docs/UPSTREAM_AUDIT.md)：上游代码的已知问题
- [实现规格](docs/specs/README.md)：当前实现边界
- [决策记录](docs/decisions/README.md)：ADR 索引
- [PITFALLS](docs/PITFALLS.md)：实现过程中亲历且可复现的错误假设
- [coding agent 测试框架调查](docs/reference/agent-testing-survey.md)：外部项目证据
- [主流开源 coding agent 文件工具调查](docs/reference/file-tools-survey.md)：本轮借鉴与未借鉴的源码依据
- [coding agent 执行生命周期调查](docs/reference/agent-loop-lifecycle-survey.md)：Session、Turn、Step 与任务评测的外部证据

## 来源与许可证

上游 Mini-Agent 的 agent loop、工具、CLI、MCP/skills 和 ACP baseline 来自 MiniMax-AI；本项目通过 Git 历史区分上游代码与我的改造。项目继续使用 [MIT License](LICENSE)。
