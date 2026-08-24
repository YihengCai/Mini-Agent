# ADR-0003：删除 ACP，并用同步事件连接 core 与 CLI

- 日期：2026-08-24
- 状态：已采纳
- 关联：`mini_agent/core/`、`mini_agent/cli_events.py`、`tests/test_agent_loop_offline.py`、提交 `fe6a682`、`cd9ae14`

## 背景

改造前，`Agent.run()` 同时持有消息、模型—工具控制流、终端渲染和 `AgentLogger`；ACP 又实现一份模型调用、工具执行与消息追加。可用 `git show fe6a682^:mini_agent/agent.py` 和 `git show cd9ae14^:mini_agent/acp/__init__.py` 复查两条控制流。

ACP 的测试也没有跨协议边界：`git show cd9ae14^:tests/test_acp.py | nl -ba | sed -n '61,89p'` 显示它用 `DummyConn` 和 `SimpleNamespace` 直接调用 Python 对象，没有启动 stdio 服务端，也没有验证 JSON-RPC 编解码、连接生命周期或真实客户端。对以执行框架为研究核心的项目，这些证据不足以证明 ACP 是当前能力，却要求维护公开命令、依赖和第二种交互 contract。

## 选项

1. **保留两份循环**：CLI 与 ACP 各自控制模型和工具，改动最少，但状态、压缩、取消与日志会继续分叉。
2. **保留 ACP，但让它调用共享 core**：消除重复循环，同时继续维护协议入口、依赖与适配测试。
3. **让 CLI 成为 ACP 客户端**：core 放在 ACP 服务端后面，所有交互统一经过进程间协议。
4. **删除 ACP，CLI 直接调用 core**：core 发出进程内事件，CLI 消费事件完成显示和日志；需要真实协议客户端时再建立协议层。

## 决定

采用选项 4。模型—工具循环、消息、压缩和终止判断由 `mini_agent/core/agent.py:33-589` 持有；`mini_agent/agent.py` 只保留原导入路径的兼容转发。CLI 通过 `add_user_message()`、`clear_history()` 和 `run(event_sink=...)` 调用 core，不再拥有另一份控制流（`mini_agent/core/agent.py:63-84,374-589`；`mini_agent/cli.py:569-589,675-680,779`）。

观察边界采用可选的同步判别联合事件，定义在 `mini_agent/core/events.py:1-160`。事件数据只在回调期间借用；需要留存的接收器当场复制或序列化。接收器异常沿调用栈传播，不在 core 中静默吞掉。`Agent.run()` 的字符串返回保持不变，CLI 的 ANSI 文案、截断长度、日志路径和 `AgentLogger` 留在 `mini_agent/cli_events.py:1-233`。

同时删除 `mini_agent/acp/`、`tests/test_acp.py`、`mini-agent-acp` 命令和 `agent-client-protocol` 依赖；当前发行面只保留 `mini-agent`（`pyproject.toml:11-27`）。本次不设计持久化事件格式、结构化运行结果、统计、基准评测、回放或新的取消语义。

## 为什么否决其他的

**否决保留两份循环**：同一工具结果和终止条件会有两个状态所有者，任何执行框架改造都必须同步修改并验证两条路径；改造前 ACP 已经没有走主循环的压缩与日志路径。只有当两个入口故意采用不同的执行语义、各自拥有明确 contract 和独立端到端测试时，两份循环才可能是对的。

**暂不保留 ACP 适配器**：共享 core 可以修复代码重复，但不能替一个尚不存在的使用者证明协议必要性；原测试只证明 Python 方法可调用。出现真实 ACP 客户端、明确互操作需求，并有从客户端到 stdio 服务端的端到端回归时，这个选项反而合适。

**否决 CLI 经 ACP 调用 core**：它会给本地单进程交互增加序列化、进程生命周期和连接失败边界，还会让 ACP 的字段提前塑造内部事件与终止语义。若将来必须隔离进程、支持远程或多个独立客户端，而且能量化这种边界的收益与故障行为，CLI 作为协议客户端才更合理。

同步回调而不是 `async generator`（异步生成器），是为了保留现有 `await Agent.run()` 与字符串返回 contract，并避免在本轮引入拉取、背压和流关闭语义。等到流式模型输出或异步事件消费者出现可复现需求时，异步生成器或有界队列可能更合适；当前同步接收器不是持久化轨迹 contract。

## 怎么验证它是对的

- `tests/test_agent_loop_offline.py:290-352` 用真实 core 循环断言模型、工具、步骤和唯一终止事件的完整顺序；删掉关键事件发射会使测试转红。
- `tests/test_agent_loop_offline.py:355-412` 证明无 `event_sink` 时 core 不输出终端也不构造 `AgentLogger`，同时 CLI 接收器仍渲染步骤、工具、回复并调用原日志接口。
- `tests/test_agent_loop_offline.py:457-521` 证明摘要模型调用和主循环调用位于同一事件序列。
- `uv lock --check` 实测通过；`.venv/bin/python -c "import importlib.util; assert importlib.util.find_spec('mini_agent.acp') is None; assert importlib.util.find_spec('acp') is None"` 实测通过。
- README 所列离线命令在 2026-08-24 实测为 `122 passed`，只有一条既有的 `cache_dir` 配置警告。

## 回头看

原计划是让 CLI 与 ACP 共用一条 loop；实现前重新检查研究目标和测试后，实际边界收敛为“core + 一个真实 CLI 适配器”，而不是为没有真实使用者的协议保留位置。这减少了一份生产循环、一个公开命令和一个直接依赖，同时没有改变 `Agent.run()` 的返回字符串或 CLI 的主要显示与文本日志行为。

本轮事件只证明可以观察当前执行，不证明可持久化、可回放或可作为基准评测轨迹。`AgentLogger` 仍由 CLI 接收器调用，统计仍读取当前消息状态；等它们成为当前工作时，再依据真实消费需求决定是否拆成独立接收器。没有出现重新引入 ACP 的证据。

后续审查删除了 `tests/test_architecture_boundaries.py`：它检查文件布局、禁止导入列表和 ACP 的缺席，固化的是当前实现形状而不是业务 contract。core 静默运行、事件顺序和 CLI 行为仍由 `tests/test_agent_loop_offline.py` 验证；ACP 的去留由本 ADR、发行配置和 Git 历史表达。
