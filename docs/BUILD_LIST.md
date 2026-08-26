# Mini-Agent 当前工作与研究主题

## 使用方式

这个文件是项目唯一的选题清单，不是固定路线图。任何时候只展开一项当前工作；其余条目只是可以单独选择的研究问题，不代表已经采用某个方案或承诺实现。

选择一个主题时，先为它找到当前代码中的失败证据和一分钟内可运行的离线验证，再写改动前简报。具体接口、类名和文件布局到实现时再决定，不为候选主题提前创建规格或 ADR。

## 当前工作：待选择

本轮代码与依赖收口已经完成。剩余候选要么只有两三行收益却改变日志 contract，要么属于明确排除的 CLI 与工具范围；下一项回到研究主题选择，不再为行数继续重构。

## 可选研究主题

以下主题的定义没有按实现顺序排列。“选择条件”只说明什么时候值得把它提升为当前工作；没有对应证据时，可以继续留在这里。跨主题的参考顺序见下一节。

| 主题 | 当前证据与研究问题 | 什么时候值得选择 |
|---|---|---|
| 会话事实记录与模型请求视图 | Session 当前只持有一份模型可见 `_messages`，每个 Step 把它完整复制给模型（`mini_agent/core/agent.py:122-127,329-349`），没有把已发生事实、请求视图、持久化和恢复分开；append-only session log（仅追加的会话日志）只是候选方案之一。 | 准备替换消息状态或处理恢复、审计问题时。 |
| 模型调用扩展 | 基础 `ModelClient` contract、显式注册表和两种 wire adapter 已落地，但仍没有流式响应、统一错误分类或真实端点能力记录（`mini_agent/llm/protocol.py:9-25`；`mini_agent/llm/factory.py:11-50`；`docs/PROVIDER_CAPABILITIES.md:20`）。 | 出现流式消费或可复现错误语义需求，或当前端点的新能力必须进入请求时。 |
| 上下文管理 | 旧本地压缩已由 [ADR-0006](decisions/0006-remove-legacy-local-compaction.md) 删除；当前完整历史无预算直传，`usage` 仅供观察。研究 token 预算、选择、压缩和失败行为需要哪些不变量。 | 事实记录与请求视图边界稳定、工具输出有预算后；依赖 vendor 能力前先做端点探测。 |
| 任务模式与意图边界 | 研究“讨论、规划、修改、审查”等意图应由显式模式、确定性规则还是模型判断，以及能力边界是否要通过工具和权限强制。这里不预设需要单独的意图分类模型。 | 先复现仅靠提示词导致错误动作的案例；没有案例就继续保留为问题。 |
| 代码搜索与上下文选择 | 项目没有专用 Glob/Grep；搜索依赖 bash，结果只有通用单条 64 KiB 首尾截断，没有忽略规则、结构化匹配或可恢复分页（`mini_agent/core/tool_output.py:6-69`；`docs/UPSTREAM_AUDIT.md:61-65`）。研究搜索、截断信息和 token 预算；直接使用 `rg` 与仓库地图都只是候选。 | 出现可复现的代码定位失败或搜索结果挤占上下文时。 |
| 工具执行与输出边界 | core 已统一结构预检、串行执行和单条模型消息预算（`mini_agent/core/tool_execution.py:90-195`；`mini_agent/core/tool_output.py:6-69`），但没有 JSON Schema 参数校验、长运行控制、并行执行、整批合计预算或可恢复的超限正文。 | 从一个具体参数、长运行、批次总量或恢复失败案例进入。 |
| 可靠的代码编辑与恢复 | 当前已完成唯一精确匹配、单文件原子替换和提交前失败不改目标（`mini_agent/tools/file_tools.py:81-126,468-573`）；仍没有读取版本回执或并发覆盖检测，也不能一起恢复多个文件、工具副作用和模型状态。 | 先复现读取后并发变化造成的丢失更新，或需要跨文件恢复的具体失败；检查点只有在能完成恢复验证时才进入实现。 |
| 权限与执行隔离 | 工具当前直接执行，文件与 shell 没有统一策略或强制隔离（`mini_agent/core/agent.py:413-485`；`docs/UPSTREAM_AUDIT.md:49-53`）。研究用户批准负责什么、操作系统沙箱负责什么，以及工作区、网络、进程和资源边界怎样验证。 | 能够一次完成真实拒绝测试矩阵时。 |
| 指令、skills 与 MCP 的信任边界 | 系统提示词、skills 元数据和 MCP 工具分别在 `mini_agent/cli.py:304-398,547-567` 组装。研究加入仓库指令后，各来源如何确定优先级，外部内容怎样避免获得额外权限。 | 出现可复现的指令冲突、提示词注入或外部工具数据污染案例时。 |
| subagent 隔离与并发 | 研究 subagent 是否真的减少父级上下文，以及预算、工具权限、结果大小和并发文件修改怎样隔离。 | 能先定义可量化收益和冲突案例时；仅仅“能启动另一个 agent”不算完成。 |
| 轨迹、回放与任务级评测 | core 已提供带 Session、Turn、Step 身份的进程内同步事件，但没有持久化或回放语义（`mini_agent/core/events.py:20-103`）。研究怎样从执行事件生成调试轨迹和回放，并在模块回归之外度量任务完成、错误修改、调用次数、token、延迟与成本。 | 积累了单模块测试覆盖不了的真实回归，并能先定义任务结果判定时。 |

本轮选题范围参考了 Codex、Gemini CLI、OpenHands、aider、SWE-agent 和 goose 的一手资料；外部项目拥有某项能力不构成本项目实现它的理由，真正选择主题时重新核对当时源码。

## 研究优先级与必要前置

这份排序是 2026-08-25 的研究判断：价值看它能否暴露 coding agent 的核心不变量、解锁后续模块并形成有区分度的项目证据；前沿程度看领先项目是否仍在快速探索、尚未形成稳定 contract。`S / A / B` 是定性分档，不是本项目能力或效果的测量结果。

| 参考顺序 | 主题 | 研究价值 | 前沿程度 | 进入该主题前必须具备的条件 |
|---:|---|:---:|:---:|---|
| 1 | 轨迹、回放与任务级评测 | S | S | 先定义可离线判定的任务结果；持久化轨迹依赖稳定的事实身份，副作用回放还依赖工具与恢复语义。最小任务级评测可以先基于现有生命周期事件落地。 |
| 2 | 会话事实记录与模型请求视图 | S | S | 先复现恢复、审计或历史重写问题，并明确哪些已发生事实不能被后续上下文策略改写。 |
| 3 | 上下文管理 | S | S | 事实记录与模型请求视图的边界已经稳定；工具输出已有预算；依赖 vendor 能力时已有实测记录或明确降级方案。 |
| 4 | 工具执行与输出边界 | S | A | 先复现具体的工具失败、长运行失控或无界输出进入模型消息，并定义一分钟内的离线断言。 |
| 5 | 权限与执行隔离 | S | A | 已有统一的工具执行强制点，并能一次完成文件、网络、进程和资源边界的真实拒绝测试矩阵；不能只实现提示词批准。 |
| 6 | 指令、skills 与 MCP 的信任边界 | S | S | 已有权限强制点，并复现来源冲突、提示词注入或外部数据污染；信任等级不能只存在于提示词。 |
| 7 | 代码搜索与上下文选择 | A | A | 已有工具输出与上下文预算，并复现定位失败或搜索结果挤占上下文；任务级评测能比较结果。 |
| 8 | 可靠的代码编辑与恢复 | A | A | 先复现读取后并发变化造成的丢失更新；跨文件检查点只有在工具副作用、权限和 agent 状态都能完整恢复时才进入实现。 |
| 9 | subagent 隔离与并发 | A | S | 任务级评测、事实与上下文边界、权限强制和文件冲突处理均已可验证，并能量化它相对单 agent 的收益。 |
| 10 | 任务模式与意图边界 | A | B | 已有可强制的权限能力集合，并复现仅靠提示词会越权的案例；模式必须改变可用能力而不只是提示词措辞。 |
| 11 | 模型调用扩展 | A | B | 基础 adapter contract 已完成；出现流式响应、错误分类或新 vendor 能力需求时，只扩展能解锁当前主题的最小部分，并先补本地协议测试或端点探测。 |

选择下一项时，先排除未满足本表前置条件、没有当前失败证据或没有一分钟离线验证的主题，再从剩余主题中选择参考顺序最靠前的一项。前置本身只实现解锁当前主题所需的最小闭环，不能借机展开整项；沙箱和检查点仍必须等完整验证条件具备后一次进入。新证据可以改变排序，但要在本表写清原因。任何时候仍只有一项“当前工作”，本表本身不代表已经选择或实现。

## 最近完成

- **删除重复终端场景测试**：删除 4 项只把已覆盖的 ASCII、emoji、中文宽字符重新拼成 CLI 文案的测试，其中一项原来只断言结果大于零；保留 9 项宽度分支回归和 1 项公开面回归。测试净减 36 行，定向集合实测 `10 passed in 0.29s`，完整离线集合实测 `253 passed, 5 deselected in 12.85s`。CLI 实现、宽度算法和工具未改；ADR-0036 回头看已更新。
- **分开直接运行依赖与开发依赖**：生产区只保留代码直接导入的 6 个库，`dev` 开发组单一持有 pytest 与 pytest-asyncio；删除零消费者的直接 httpx/requests/pip/pipx、重复 `dev` extra、pytest-cov/xdist 和 pylint 配置。锁图从 59 个包降到 47 个，12 个独占包被删除，所有保留版本、原镜像与 `revision` 不变；`pyproject.toml` 净减 19 行，`uv.lock` 净减 330 行。`uv lock --check`、生产/开发依赖树和完整离线集合 `257 passed, 5 deselected in 13.62s` 均通过。Python 实现、CLI 与全部 agent 工具未改，取舍见 [`decisions/0038-separate-runtime-and-dev-dependencies.md`](decisions/0038-separate-runtime-and-dev-dependencies.md)。
- **只保留一个 core-facing 模型 contract**：删除与 `ModelClient` Protocol 重复的 53 行 `LLMAdapter` ABC、公开导出及无消费者的 `api_key/api_base` 镜像；两个具体 adapter 各自保留正数输出上限守卫、模型、预算和 SDK 客户端，factory 返回 `ModelClient`。生产代码净减 49 行，测试净增 20 行；定向集合实测 `82 passed in 0.88s`，临时删除守卫并恢复旧导出时 5 项关键回归转红；完整离线集合实测 `257 passed, 5 deselected in 13.34s`。CLI 与全部工具未改，取舍见 [`decisions/0037-one-core-facing-model-contract.md`](decisions/0037-one-core-facing-model-contract.md)。
- **内联 adapter 单次请求 seam**：两个 `generate()` 直接组装现有 SDK 参数、各调用一次 `create()` 并解析响应，删除只被这里消费的 `_make_api_request()`；一次调用与原异常对象回归改在 SDK 边界注入失败。生产代码净减 55 行，测试净增 6 行，公开 contract 与 wire 请求不变。定向集合实测 `77 passed in 0.75s`；临时重建 SDK 异常时两种 adapter 回归均转红；完整离线集合实测 `252 passed, 5 deselected in 13.54s`。CLI 与全部工具未改，ADR-0027 的回头看已更新。
- **拉直 adapter 私有请求组装**：两个 `generate()` 直接把消息转换结果交给单次 SDK 请求并解析响应，删除两个单消费者 `_prepare_request()` 和 OpenAI `_convert_messages()` 恒定返回的 `None`；tools/messages/response 的协议转换边界保留。生产代码净减 62 行，公开 contract 与 wire 请求不变。定向集合实测 `77 passed in 0.64s`；临时绕过两种消息转换时两项完整 SDK 请求断言均转红；完整离线集合实测 `252 passed, 5 deselected in 13.01s`。CLI 与全部工具未改。
- **删除无消费者的终端辅助 API**：CLI 只使用 `Colors` 与 `calculate_display_width()`；现删除只由专用测试消费的截断、填充函数，以及 10 个全仓零引用的颜色常量。生产代码净减 106 行，测试净减 110 行并删除 17 项死代码自测；CLI、宽度算法和全部工具未改。定向集合实测 `54 passed in 0.69s`，临时恢复公开名字时负向回归实测转红；完整离线集合实测 `252 passed, 5 deselected in 13.63s`。取舍见 [`decisions/0036-remove-unused-terminal-helpers.md`](decisions/0036-remove-unused-terminal-helpers.md)。
- **合并 Session 重复测试**：删除用 MagicMock、文件工具和临时目录重复多轮历史的 `tests/test_session_integration.py`；两次默认构造身份不同和历史 Message 深层快照迁入脚本化 LLM 的 core 回归，其余行为已有更强生命周期断言。测试净减 111 行；定向集合实测 `42 passed in 0.79s`，把 `get_history()` 退化为浅列表复制时关键断言实测转红；完整离线集合实测 `269 passed, 5 deselected in 13.41s`。生产代码和工具测试未改。
- **归档已完成规格**：`docs/specs/` 自身规定完成后由代码、测试和 ADR 接管事实，现将 16 份共 496 行的完成副本删除，只保留实现前规则页。模型 adapter 的历史探针迁入 ADR-0005，shell 登记失败回收不变量迁入 ADR-0009，Step 定义改引 ADR-0004；仓库没有残留文件级入链。文档净减 484 行；迁入探针实测输出保持 `https://api.minimax.io.evilproxy/anthropic`、`configured typo`、`routed openai`，Markdown 回归 `1 passed in 0.32s`，完整离线集合 `271 passed, 5 deselected in 13.44s`。
- **删除真实模型伪测试**：删除两份共 352 行、默认排除且不能稳定判错的模型演示测试；真实端点继续由 CLI 手动体验，脚本化 LLM 继续验证 agent loop。外部收集回归只保留 5 项 MCP/网络探测，定向实测 `2 passed in 3.67s`，显式收集实测 `5/32 tests collected (27 deselected) in 0.44s`；测试代码合计净减 359 行，完整离线集合实测 `271 passed, 5 deselected in 13.50s`。P-008 已改用提交归档保持原 `33` 项/默认排除 `9` 项的复现证据。
- **删除无消费者的 Session 配置别名**：删除只返回私有字段的 `llm`、`max_steps` 属性，并让 runner 的 `finally` 单一负责释放活动句柄；`tools`、`session_id`、`active_turn`、历史和构造参数保留。活动 Turn 回归直接替换私有模型、预算和执行器，仍证明使用接纳时快照。生产代码净减 10 行，测试净增 2 行；定向集合实测 `49 passed in 0.80s`，临时恢复 `llm` 属性时公开面回归转红；完整离线集合实测 `271 passed, 8 deselected in 14.02s`。取舍见 [`decisions/0035-remove-unused-session-config-aliases.md`](decisions/0035-remove-unused-session-config-aliases.md)。
- **工作区事实由 CLI 组装**：删除 `AgentSession.workspace_dir` 参数与状态、目录副作用、提示词改写和无消费者的 `system_prompt` 别名；CLI 在配置与 Skill 处理后追加同一准确事实块，core 原样保存完整提示词。生产代码净减 3 行，测试净减 61 行；定向集合实测 `73 passed in 0.79s`，临时移除 runtime 组装挂钩时三个来源场景转红；完整离线集合实测 `271 passed, 8 deselected in 13.81s`。工作区选择、CLI 能力和全部工具未改，取舍见 [`decisions/0034-cli-composes-workspace-fact.md`](decisions/0034-cli-composes-workspace-fact.md)。
- **usage 只保留在响应事件**：删除只保存“最近一次非空 `total_tokens`”却命名为 Session 总计的 `_api_total_tokens`、公开属性和 CLI `API Tokens Used` 显示；adapter 映射与每个 `ModelResponse.response.usage` 不变。定向集合实测 `36 passed in 0.62s`，临时恢复公开镜像时关键回归实测转红；完整离线集合实测 `270 passed, 8 deselected in 12.93s`。取舍见 [`decisions/0033-keep-usage-on-model-response-events.md`](decisions/0033-keep-usage-on-model-response-events.md)。
- **observer 不控制 Turn**：同步接收器首个普通异常后只停用自身，工具批次、历史和 Turn 结果继续由真实执行原因决定；删除 `observer_error` 主因/次因矩阵和 CLI 二次 fallback。生产代码净减 87 行，测试净减 77 行；定向集合实测 `66 passed in 0.74s`，恢复异常传播时关键回归实测转红；完整离线集合实测 `270 passed, 8 deselected in 13.36s`。取舍见 [`decisions/0032-observers-do-not-control-turns.md`](decisions/0032-observers-do-not-control-turns.md)。
- **调用标识符只约束未完成批次**：删除执行器中永不清空、不可恢复的 Session 隐藏集合；同批空 ID、非法类型和重复 ID 继续在零副作用前失败，已完成批次则可跨 Step/Turn 复用 ID。生产代码净减 11 行，测试净减 108 行；一个正向回归同时检出生产账本和测试替身的全历史唯一规则。相关集合实测 `67 passed in 0.73s`，完整离线集合实测 `273 passed, 8 deselected in 13.76s`。取舍见 [`decisions/0031-scope-tool-call-ids-to-pending-batches.md`](decisions/0031-scope-tool-call-ids-to-pending-batches.md)。
- **删除不可读取的 Note 半能力**：删除只在运行时注册写入端的 Note 模块、配置开关、CLI 接线、导出、专用离线测试与外部演示；真正的记忆能力留给“会话事实记录与模型请求视图”topic，不以补注册扩大当前范围。生产代码净减 222 行，测试净减 300 行；定向离线集合实测 `75 passed in 4.52s`，完整离线集合实测 `277 passed, 8 deselected in 14.11s`。取舍见 [`decisions/0030-remove-incomplete-note-memory.md`](decisions/0030-remove-incomplete-note-memory.md)。
- **删除未探测的 `thinking` 半能力**：从共享消息/响应 schema、core、CLI 与日志删除始终为空且无法往返的字段；Anthropic-compatible 未知 thinking block 继续忽略，两种 adapter 的可见 assistant/tool 历史映射不变。定向集合实测 `111 passed in 0.82s`，完整离线集合实测 `285 passed, 9 deselected in 13.46s`。取舍见 [`decisions/0029-remove-unprobed-thinking-field.md`](decisions/0029-remove-unprobed-thinking-field.md)。
- **配置文件与运行时模型同构**：YAML 根级直接使用 `llm`、`agent`、`tools`；删除无调用的 `Config.load()`、扁平字段分片、必填项扫描和四类定向迁移分支，加载后只做一次严格模型校验。配置、来源与 CLI 接线集合实测 `43 passed in 0.70s`，完整离线集合实测 `285 passed, 9 deselected in 13.90s`。取舍见 [`decisions/0028-config-file-matches-runtime-model.md`](decisions/0028-config-file-matches-runtime-model.md)。
- **模型错误分类前不做项目级 retry**：删除文件与运行时 retry 配置、退避模块、CLI 专用回调及两个 adapter 的包装层；SDK 继续显式 `max_retries=0`，每个 adapter 只直接调用一次并透传原异常。2 个协议参数场景锁定一次调用和对象身份；定向集合实测 `113 passed in 0.61s`，完整离线集合实测 `286 passed, 9 deselected in 13.68s`。取舍见 [`decisions/0027-no-project-retry-before-error-classification.md`](decisions/0027-no-project-retry-before-error-classification.md)。
- **前台 shell 中断时回收直接子进程**：正常 `communicate()` 语义不变；超时与取消在返回或传播前共用直接子进程的 `kill()` 后 `wait()` 清理，前台进程不进入 `BackgroundShellManager`。2 项故障注入在删除各自清理挂钩时转红；shell 定向集合实测 `42 passed in 9.72s`，完整离线集合实测 `317 passed, 9 deselected in 13.23s`。取舍见 [`decisions/0026-foreground-shell-reaps-on-interruption.md`](decisions/0026-foreground-shell-reaps-on-interruption.md)。
- **工具返回值的接纳所有权**：合法 `ToolResult` 在执行器接纳处立即深拷贝；工具保留的返回别名即使在同步 `ToolFinished` 观察期间被修改，也不能让事件事实与模型历史分叉。1 项新回归在删除接纳复制时转红；定向集合实测 `47 passed in 0.62s`，完整离线集合实测 `315 passed, 9 deselected in 13.33s`。取舍见 [`decisions/0025-executor-owns-admitted-tool-results.md`](decisions/0025-executor-owns-admitted-tool-results.md)。
- **运行时工作区的单一所有权**：配置模型不再持有从未被消费的 `workspace_dir`；CLI 的 `--workspace` / 当前目录继续作为唯一来源，旧字段由共享严格模型拒绝。ADR-0034 后续把提示词组装也收回 CLI，工作区选择、目录创建和工具路径行为没有改变；取舍见 [`decisions/0024-cli-owns-runtime-workspace.md`](decisions/0024-cli-owns-runtime-workspace.md)。
- **后台 shell 输出完成边界**：自然完成的 monitor 持续读取到 stdout EOF，再等待进程并发布 `completed` 或 `failed`；进程已退出但仍缓冲的行不再丢失。确定性 fake 回归在恢复旧退出码条件时转红；既有真实 bash 定向集合保持通过。定向集合实测 `40 passed in 9.77s`，完整离线集合实测 `312 passed, 9 deselected in 13.07s`。主动终止尾部输出仍不在保证内，取舍见 [`decisions/0023-background-shell-completes-after-stdout-eof.md`](decisions/0023-background-shell-completes-after-stdout-eof.md)。
- **core 模型失败语义**：模型异常统一形成 `LLM call failed: {error}`，原对象继续进入事件，事件结果与 Turn 错误使用同一文本；core 不解释具体 adapter 的错误类型。普通 `OSError` 回归覆盖对象身份和两处文本；恢复旧特判时该项转红。取舍见 [`decisions/0022-core-preserves-model-error-semantics.md`](decisions/0022-core-preserves-model-error-semantics.md)。
- **Skill 发现注册表快照**：递归路径先稳定排序，成功解析的 Skill 在局部名称索引中完成；重名会报告两个来源并让本次扫描失败，上一完整快照不变，成功重扫则一次替换并清除已删除条目。2 项新回归覆盖删除后重扫、重名诊断与失败状态；退化为累加更新或移除重名守卫时各有 1 项转红。完整离线集合实测 `309 passed, 9 deselected in 13.21s`，取舍见 [`decisions/0020-transactional-skill-discovery.md`](decisions/0020-transactional-skill-discovery.md)。
- **Turn 日志排他分配**：同一秒的独立 logger 通过文件系统排他创建获得基础名与确定性后缀，不再截断已有事实；文件名与表头共用一次时钟采样，默认目录不变且支持显式注入。2 项离线回归覆盖同名保留与跨秒一致性；恢复覆写模式或第二次采样时各有 1 项转红。完整离线集合实测 `307 passed, 9 deselected in 13.20s`，取舍见 [`decisions/0019-exclusive-turn-log-allocation.md`](decisions/0019-exclusive-turn-log-allocation.md)。
- **MCP transport 显式校验**：只有完全缺少 `type` 时才按 URL 推断；四个已知名称保持大小写不敏感，未知、空、`null` 与非字符串会在连接构造前隔离当前 server，并让合法后续项继续加载。6 项新增或扩展回归覆盖显式值、连接入口和 loader 隔离；恢复宽松推断与移除连接守卫时分别有 5 项和 1 项转红。完整离线集合实测 `274 passed, 9 deselected in 13.12s`，取舍见 [`decisions/0016-reject-explicit-invalid-mcp-transports.md`](decisions/0016-reject-explicit-invalid-mcp-transports.md)。
- **配置伴随文件来源绑定**：CLI 把已选 `config.yaml` 路径显式传入同一次 runtime；相对系统提示词与 MCP 配置只从其词法父目录解析，绝对路径原样保留，缺失时使用内置提示词或保持 MCP 未加载，不再借用其他来源的同名文件。5 项新回归覆盖纯路径函数、相对存在、相对缺失与绝对路径；恢复旧全局搜索时 3 项转红。完整离线集合实测 `269 passed, 9 deselected in 13.12s`，取舍见 [`decisions/0015-bind-config-companions-to-selected-source.md`](decisions/0015-bind-config-companions-to-selected-source.md)。
- **工作区事实精确注入**：准确块匹配最初在 Session 落地；ADR-0034 后续把同一行为移到 CLI。调用者提示词中的偶然 `Current Workspace` 文字和旧路径继续作为前缀保留，但不能阻止真实路径进入模型请求；准确块不重复。
- **正数 Step 预算双边界**：`AgentConfig` 在 CLI runtime 组装前要求 `max_steps > 0`，公开 `AgentSession` 在身份生成、工具注册和 Turn 接纳前独立执行同一守卫；`0/-1` 不能产生零模型请求的伪 Turn。配置与 core 各 2 项回归固定字段错误和无模型请求；移除任一层时对应测试转红。完整离线集合实测 `261 passed, 9 deselected in 13.85s`，取舍见 [`decisions/0014-positive-step-budget-at-config-and-core.md`](decisions/0014-positive-step-budget-at-config-and-core.md)。
- **MCP 错误正文归一化**：MCP `isError` 的非空正文现在写入内部 `ToolResult.error`，成功正文仍写入 `content`，空错误保留通用兜底；现有批次执行器因此把同一诊断交给原始 `ToolFinished`、模型消息、CLI 和日志。4 项纯离线回归使用真实 MCP SDK 结果类型覆盖成功、多段正文、错误、空错误和 executor 集成；恢复通用错误时集成回归转红。完整离线集合实测 `257 passed, 9 deselected in 13.18s`。
- **MCP 状态与连接运行时所有权**：不可变超时快照、连接接纳与关闭现在由一次 CLI runtime 的 `MCPManager` 持有；server 覆盖仍优先，连接在 `await connect()` 前登记，关闭串行化并只移除成功项，取消后的 transport 句柄可重试。6 项新增所有权回归与 MCP/CLI 定向集合实测 `55 passed, 5 deselected in 0.71s`；显式排除 `external` 的完整集合实测 `237 passed, 9 deselected in 13.10s`。取舍见 [`decisions/0011-runtime-owned-mcp-connections.md`](decisions/0011-runtime-owned-mcp-connections.md)。
- **模型可见工具输出预算**：原始 `ToolResult` 与 `ToolFinished` 保持完整，批次执行器只把模型消息投影限制为每条 64 KiB UTF-8 字节；精确边界不变，超限保留首尾并报告原始、保留、省略和上限字节数，失败前缀计入预算。3 项新增回归覆盖 UTF-8、事件所有权、观察者变异、历史、下一次请求与批次顺序；删除挂钩时集成回归实测转红。显式排除 `external` 的完整集合实测 `230 passed, 9 deselected in 13.57s`，取舍见 [`decisions/0010-model-facing-tool-output-budget.md`](decisions/0010-model-facing-tool-output-budget.md)。
- **后台 shell 状态与资源所有权**：配置和模型客户端成功后，一次 CLI runtime 持有显式注入三个 shell 工具的 manager；`/clear` 保留它，退出才按 shell、MCP 顺序关闭。manager 隔离状态，拒绝重复与关闭后登记，串行化并发 `close()`，等待 monitor 和强杀后 subprocess，并保留失败项供重试。25 项定向回归与显式排除 `external` 的完整集合实测为 `227 passed, 9 deselected in 14.00s`，取舍见 [`decisions/0009-runtime-owned-background-shells.md`](decisions/0009-runtime-owned-background-shells.md)。
- **模型工具批次强制点**：Session 持有冻结注册与批次执行器；agent loop 在接纳边界深拷贝整个模型响应，批次在首个副作用前完整预检，再串行执行。结构错误使用 `tool_protocol_error`，工具自身失败保留为同序结果，模型客户端、参数、事件和历史使用独立快照，assistant 调用与全部结果成组提交。原 Session 级调用标识符账本后来由 ADR-0031 删除，其余强制点不变；取舍见 [`decisions/0008-session-owned-tool-batch-executor.md`](decisions/0008-session-owned-tool-batch-executor.md)。
- **默认测试入口安全、离线**：外部能力统一使用 `external` marker；默认配置与根级收集门双层排除，只有显式 `--run-external` 才放行。入口回归能检出普通 `-m` 绕过和 marker 拼错；当前保留 MCP 模块的 27 项离线测试与 5 项显式外部探测，真实模型伪测试已删除。原实现测量与取舍见 [`decisions/0007-explicit-opt-in-for-external-tests.md`](decisions/0007-explicit-opt-in-for-external-tests.md)。
- **删除旧本地压缩**：Session 暂以完整模型历史直传；本地 token 估算、摘要替换、四类压缩事件、配置字段、摘要用途标签和 `tiktoken`/`regex` 依赖已经删除，旧配置键明确失败。64 项定向回归与锁文件校验通过，标准离线集合共 `157 passed`。取舍见 [`decisions/0006-remove-legacy-local-compaction.md`](decisions/0006-remove-legacy-local-compaction.md)。
- **模型 API adapter 边界**：core 通过 `ModelClient` 调用模型并使用中性 `ToolDefinition`；显式注册表选择具体 wire adapter，端点、模型和输出上限不再由域名或 vendor 默认值推断。adapter 离线测试覆盖配置迁移、逐字端点传递、SDK 请求、工具历史、基础响应、SDK retry 关闭与单次项目调用；未探测 `usage` 只供观察。取舍见 [`decisions/0005-explicit-model-api-adapters.md`](decisions/0005-explicit-model-api-adapters.md)。
- **显式执行生命周期**：`AgentSession` 表示一段逻辑对话，`TurnHandle` 表示一次控制权交接，Step 表示一次 agent 模型请求及其完整工具批次；结构化停止原因不判断任务成功。原 observer 错误仲裁后来由 ADR-0032 删除，身份、接纳、快照、中断和成组提交边界不变；取舍见 [`decisions/0004-session-turn-step-lifecycle.md`](decisions/0004-session-turn-step-lifecycle.md)。
- **core agent loop 边界**：模型—工具控制流与消息状态移入 `mini_agent/core/`，CLI 通过同步事件完成终端渲染和原有日志；没有真实客户端与端到端协议测试的 ACP 已删除。122 项离线测试覆盖事件顺序、无 UI 运行和 CLI 适配。取舍见 [`decisions/0003-remove-acp-and-extract-core-loop.md`](decisions/0003-remove-acp-and-extract-core-loop.md)。
- **文件工具重写**：`read_file` 采用有界完整行窗口，`edit_file` 始终要求唯一精确匹配，写入以同目录原子替换提交；27 项定向离线测试覆盖预算、续读、歧义、CRLF、权限位和故障注入。取舍见 [`decisions/0002-bounded-and-atomic-file-tools.md`](decisions/0002-bounded-and-atomic-file-tools.md)。
- **LLM 测试替身**：`tests/llm_test_double.py` 与 `tests/test_agent_loop_offline.py` 已提供确定、离线的真实 agent loop 测试入口；严格 FIFO 保留，用途标签已由 [`ADR-0006`](decisions/0006-remove-legacy-local-compaction.md) 随摘要调用删除。

## 一项改造的完成标准

1. 有一个来自当前代码或实现过程的可复现失败；
2. 离线回归测试会在关键实现被删除后转红；
3. 实现边界由代码和真实测试说明；
4. 过程中发生的取舍才写 ADR，亲历的错误假设才写 PITFALL；
5. 最后更新 README 的真实能力与限制。
