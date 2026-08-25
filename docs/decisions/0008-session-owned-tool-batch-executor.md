# ADR-0008：以 Session 持有的执行器统一模型工具批次

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/core/tool_execution.py`、`mini_agent/core/agent.py`、`mini_agent/core/turn.py`、`tests/test_tool_execution.py`、提交 `528da1f` 与 `565e266`、[P-009](../PITFALLS.md)、[P-013](../PITFALLS.md)

## 背景

改动前，Session 用字典推导注册工具，同名项会被后项静默覆盖；每个 Step 又重新读取可变的名称、说明和参数。`_AgentLoop._run_step()` 同时负责工具查找、调用、异常归一化、事件与结果消息构造，却不在首个副作用前检查空、同批重复或跨 Step/Turn 重复的调用标识符。严格配对只在测试替身收到下一次模型请求时发生，已经太晚（`git show 528da1f^:mini_agent/core/agent.py | sed -n '90,490p'`；`tests/llm_test_double.py:30-70`）。

先写下的 12 项工具执行回归在旧实现上实测为 `8 failed in 0.54s`。实现期间又用阻塞工具和 `CancelledError` 发现：仅把代码搬出 agent loop 不足以证明历史成组提交、串行副作用与调用标识符所有权（`tests/test_tool_execution.py:446-648`）。

## 选项

1. **继续内联，只补条件判断**：在 `_run_step()` 增加重名与标识符检查；改动小，但注册快照、跨 Turn 账本和调用语义仍分散在 Session 与循环中。
2. **Session 持有冻结注册表与批次执行器**：构造时固定模型定义与调度键，完整预检后串行执行，并由同一对象持有跨 Step/Turn 的调用标识符账本。
3. **立即建立通用工具中间件链**：把权限、重试、参数校验、并行和输出预算都抽象成可插拔阶段；扩展性高，但当前没有这些阶段的已验证顺序或失败 contract。
4. **只从消息历史判断重复调用**：不增加隐藏账本；状态来源更少，但工具已开始而消息尚未成组提交时，取消会让副作用在历史中消失并允许相同标识符重放。

## 决定

采用选项 2。`AgentSession` 构造并持有一个 `ToolBatchExecutor`，Turn 接纳时固定该对象引用；执行器一次读取并深拷贝工具定义，拒绝空名、重名和不合 contract 的元数据（`mini_agent/core/tool_execution.py:31-88`；`mini_agent/core/agent.py:99-102,177-183`）。

每个模型响应批次先完整验证调用类型、非空标识符、批内重复和 Session 已认领标识符，再按模型顺序串行执行。每项只在自身 `ToolStarted` 与副作用前认领标识符；普通工具异常、未知工具和非法返回都归一为该调用的失败结果，结构错误则终止 Turn 并返回 `tool_protocol_error`（`mini_agent/core/tool_execution.py:90-193`；`mini_agent/core/agent.py:407-431`）。调用参数使用独立深快照，assistant 工具调用与全部结果仍由 agent loop 一次性提交。

“唯一执行入口”只约束模型响应触发的工具调用。`AgentSession.tools` 为既有可信宿主保留原始实现的只读映射，宿主本来就可能持有传入的 Tool 引用，因此它不是权限或安全边界（`mini_agent/core/agent.py:143-151`）。本轮不加入 JSON Schema 参数校验、并行、重试、权限、输出预算、取消恢复、MCP 生命周期或后台 shell 管理。

## 为什么否决其他的

**否决继续内联补判断**：跨 Turn 的标识符账本和冻结注册都属于 Session 生命周期，把判断继续塞进 Step 会让同一 contract 由多个字段共同维持。若工具只运行一次、没有多 Turn 历史和模型可变定义，这个最小方案反而足够。

**暂不建立通用中间件链**：权限、重试、参数校验和输出预算的顺序会改变副作用、错误与计量语义；没有对应失败证据时先抽象只会把猜测变成公开扩展点。等至少两个已实现策略需要共享稳定的前置/后置阶段，并有顺序回归时，中间件反而可能比单一执行器合适。

**否决只查消息历史**：成组提交保证模型不会看到半配对历史，却意味着正在执行的调用尚未写入消息；此时取消后重放会破坏最多执行一次的保守边界。若所有工具都幂等，或调用事实能在副作用前原子持久化并恢复，消息或持久事件日志反而可以成为唯一账本。

**否决在预检后一次认领整批**：它能保证整批标识符都不会重放，但首项取消会永久占用从未启动的后项。若批次本身是不可分割的事务，或协议明确把接纳整批等同于执行所有项，这种语义反而正确；当前工具是逐项产生副作用，必须区分“批次合法”和“调用已开始”。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_tool_execution.py` 实测 `20 passed in 0.32s`，覆盖注册冻结、结构预检、跨 Step/Turn 认领、结果归一、串行顺序、历史成组提交、观察失败、取消、参数所有权与模型返回值所有权。
- `.venv/bin/python -m pytest -q tests/test_tool_execution.py tests/test_agent_loop_offline.py tests/test_agent_session_offline.py` 实测 `61 passed in 0.78s`，覆盖执行器与既有生命周期、事件和 CLI contract 的组合。
- `.venv/bin/python -m pytest -q -m 'not external'` 实测 `231 passed, 9 deselected in 13.51s`；`.venv/bin/python -m compileall -q mini_agent tests` 与 `git diff --check` 均通过。
- `rg -n "\\.execute\\(" mini_agent/core mini_agent/cli.py --glob '*.py'` 只返回 `mini_agent/core/tool_execution.py` 中由模型路径触发的直接 `Tool.execute()`。

## 回头看

最初实现选择在首个副作用前认领整个合法批次，以为这对取消最安全。故障注入证明首项抛出 `CancelledError` 时，后项尚未产生 `ToolStarted` 却已经不能重试；最终改为完整预检、逐项开始前认领，并明确该账本是 executor 内部的保守最多执行一次状态，不是模型可见事实，见 [P-009](../PITFALLS.md)。

复审还发现 `**arguments` 只复制外层映射，工具可通过嵌套字典污染 `ToolFinished` 和 assistant 历史；最终在调度边界深拷贝参数，并用变异工具锁定三个观察面。这是 [P-003](../PITFALLS.md) 所述嵌套所有权问题在新边界上的再次出现。

后续故障注入又证明，只深拷贝批次内部的参数仍不等于拥有模型返回批次：测试替身保留 `LLMResponse`，在完整预检后改写尚未启动的第二项调用，可以绕过重复标识符检查并污染副作用与历史。agent loop 现在在 `ModelClient.generate()` 返回处立即深拷贝整个响应，再向事件、执行器和历史分发；删除这一行时对应回归实测为 `1 failed in 0.55s`，见 [P-013](../PITFALLS.md)。

2026-08-26 的后续复审把同一原则补到工具返回值：工具保留的 `ToolResult` 别名曾能在 `ToolFinished` 后改写模型历史。执行器现在在合法返回值接纳处立即取得深快照，取舍与回归见 [ADR-0025](0025-executor-owns-admitted-tool-results.md)。

最终实现没有引入通用中间件，也没有把可信宿主的 Tool 引用伪装成安全隔离。后续权限、输出预算与持久恢复可以复用这一模型调用强制点，但各自仍需新的失败证据和离线回归。
