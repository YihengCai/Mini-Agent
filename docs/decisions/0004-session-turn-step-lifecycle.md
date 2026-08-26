# ADR-0004：以 Session、Turn、Step 表达执行生命周期

- 日期：2026-08-24
- 状态：已采纳
- 关联：`mini_agent/core/agent.py`、`mini_agent/core/turn.py`、`mini_agent/core/events.py`、`tests/test_agent_session_offline.py`、提交 `fdcd945`

> 后续修订：[ADR-0006](0006-remove-legacy-local-compaction.md) 删除了 token 限额快照、摘要维护调用及相应事件；Session、Turn、Step 的生命周期和所有权 contract 继续有效。以下正文保留当时决定，不作追写。

## 背景

ADR-0003 把唯一模型—工具循环移入 core，但当时为了缩小迁移范围，保留了“长期消息对象 + `await Agent.run() -> str`”的公开 contract。可用 `git show fdcd945^:mini_agent/core/agent.py | rg -n "add_user_message|async def run|RunFinished"` 复查旧实现。旧 CLI 丢弃字符串，只消费事件；模型不再请求工具又被命名为 `completed`。这只能证明一次执行交回控制权，不能证明代码任务正确，也没有表达活动执行的身份和所有权。

实现新 contract 后，审查又复现了三个不能只靠命名解决的问题：任务工厂可在 `create_task()` 内重入，晚占用活动槽会接纳两个 Turn；事件数据类的 `frozen=True` 不能冻结内部消息、字典和工具对象；接收器在工具事件中抛错会让旧写入顺序留下缺少工具结果的历史。对应回归位于 `tests/test_agent_session_offline.py:153-231,353-479`。

## 选项

1. **只把字符串换成结构化 `RunResult`**：改动最小，但仍没有会话、活动执行、输入接纳和中断的所有权边界。
2. **公开 `AgentLoop.run_turn()`**：让调用者直接驱动循环，但必须公开内部上下文，并允许绕过单活动 Turn 不变量。
3. **`AgentSession.start_turn() -> TurnHandle`**：Session 持有对话和唯一活动 Turn；句柄负责寻址、等待与请求中断；Step 留作 Turn 内部执行单位。
4. **同时建立 supervisor、事件存储和基准评测服务**：能覆盖无人值守任务判定和回放，但会把尚无消费证据的 contract 一并固化。

## 决定

采用选项 3。一个 `AgentSession` 就是一段逻辑对话；上下文压缩不改变 Session，CLI 的 `/clear` 则创建新 Session。`start_turn()` 把用户输入与活动槽作为一次接纳提交，只允许一个活动 Turn，并返回带稳定 Session/Turn 标识的 `TurnHandle`（`mini_agent/core/agent.py:98-235`；`mini_agent/core/turn.py:51-90`）。`Agent` 假兼容别名、公开 `AgentLoop`、`add_user_message()`、`clear_history()` 和 `run() -> str` 全部删除。

Turn 是客户端交出控制权到 core 交还控制权的一段执行。`TurnOutcome.stop_reason` 只报告 `end_turn`、`interrupted`、`max_steps` 或 `failed`；失败再区分模型、内部和观察错误，但没有 `success` 或 `completed`（`mini_agent/core/turn.py:10-48`）。任务是否完成由交互用户或未来 supervisor 判断，基准评测结果由 harness 外的 evaluator 判断。

Step 是一次 agent 用途的模型请求、该响应中的整批工具执行及结果写入。摘要模型调用属于 Turn 的上下文维护，事件 `step=None`，不消耗 Step 预算（`mini_agent/core/agent.py:294-402,506-825`）。一次响应的 assistant 工具调用和全部工具结果成组写入；中断只在完整 Step 边界生效，所以不会留下缺少结果的工具调用，但单个长工具当前不能立即停止。

Session 身份、模型引用、工具映射、步数和 token 上限在接纳时固化；事件通过带 Session、Turn 和可选 Step 标识的信封发送，模型请求、响应、工具定义、调用和结果使用独立快照（`mini_agent/core/agent.py:174-230,609-681`；`mini_agent/core/events.py:20-146`）。同步接收器仍是进程内观察边界：首个异常会禁用该接收器，core 在安全边界返回结构化错误；若模型或内部失败已经发生，它保留为主因，观察失败只作次级信息。已发布的 `TurnFinished.outcome` 不会因回调随后抛错而被改写（`mini_agent/core/agent.py:773-885`）。

本 ADR 只替换 ADR-0003 中“保留 `Agent.run() -> str`、事件借用 core 对象、接收器异常直接传播”的决定；删除 ACP、CLI 直连 core、同步进程内事件不是持久化轨迹的决定继续有效。本轮不实现 steering、supervisor、BenchmarkEvaluator、事件持久化或回放。

## 为什么否决其他的

**否决只包装 `RunResult`**：它能区分错误文本与正常文本，却不能原子接纳输入，也不能说明哪个调用拥有中断权。若程序永远是一次性批处理、没有多轮对话和运行中控制，这个更小的方案反而正确。

**否决公开 `AgentLoop`**：当前没有合法的手工 Step 消费者，公开它只会暴露私有状态并允许绕过 Session 的单活动 Turn。等出现需要逐步调度、暂停或恢复的真实客户端，并能定义状态移交 contract 时，公开 step controller 才可能合适。

**暂不实现完整 supervisor、评测器和事件存储**：它们分别判断任务继续、外部测试结果和持久化事实，不应被塞进 core 的停止原因。无人值守长任务、SWE-bench 子集或跨进程多客户端成为当前工作并有端到端测试时，这些层反而必要。

**否决在工具批次中途删除或回滚消息**：工具可能已有不可逆副作用，删除记录不能撤销副作用，还会造成工具调用与结果不配对。若工具本身支持取消、事务回滚，而且协议能表示“未执行”的结果，中途停止会比当前完整 Step 边界更安全；现阶段的代价是中断延迟受最长工具调用限制。

**否决继续让接收器异常直接穿透**：接收器是观察者，不应把可复用 Session 留在半个 Step。若回调本身就是业务事务的一部分，并有与工具副作用一致的原子回滚机制，直接传播反而可以作为强一致策略；当前 CLI 日志不满足这个条件。

## 怎么验证它是对的

- `tests/test_agent_session_offline.py:119-231` 验证跨 Turn 历史、唯一身份、原子拒绝、任务工厂重入和创建失败回滚。
- `tests/test_agent_session_offline.py:234-350` 验证等待者取消不取消 runner、CLI 取消先收敛、接纳时配置快照和多 Step 工具循环。
- `tests/test_agent_session_offline.py:353-607` 验证观察数据隔离、接收器失败后的消息配对、主次错误、内部失败和停止原因不包含任务成功。
- `tests/test_agent_session_offline.py:610-724` 验证完整 Step 中断、终止优先级，以及摘要调用不计为 Step。
- `tests/test_agent_loop_offline.py:386-525` 验证 CLI 分别显示 Turn 和 Step、`end_turn` 不暗示任务成功、模型错误不重复，以及帮助文案使用相同语义。
- README 所列离线命令在 2026-08-25 实测为 `144 passed`，只有一条既有的 `cache_dir` 配置警告；`git diff --check`、模块编译和公开导入检查均通过。

## 回头看

实现起点原本只是把 `run() -> str` 换成 Session/Turn/Step；实际审查迫使边界进一步收紧：公开可变工具表会让“告诉模型的工具”和实际调度的工具分叉，晚设置活动句柄会被任务工厂重入，而同步观察者也能通过嵌套对象或异常影响执行。最终实现因此增加了接纳预占、Turn 配置快照、事件数据快照和整批工具结果提交；这些不是预写设计，而是失败复现后的修正。

2026-08-25 的 CLI 复查又证明事件类型接线正确不等于用户可见语义正确：初版适配器突出显示 Step，却不显示正常 Turn 结束，还把 Step 写成 `completed`。提交 `6147f7d` 因此把事件作为唯一生命周期来源，分别显示 Turn 与 Step，并用中性符号表达 `end_turn`；这没有改变本 ADR 的 core contract。

当前结果建立了可消费的执行生命周期，但没有建立仅追加的会话事实、真正取消正在等待的模型或工具、任务完成判定、持久化轨迹或基准评测分数。下一项研究仍需从 `docs/BUILD_LIST.md` 单独选择，不能把这些缺口宣称为本轮能力。

ADR-0006 删除摘要维护后，Step 现在覆盖全部模型调用，不再需要 `step=None` 的模型事件；Turn 事件仍使用可选 Step 身份。跨 Turn 完整历史、配置快照、工具批次提交和中断边界回归保持绿色，因此生命周期主 contract 没有随压缩删除而变化。

[ADR-0008](0008-session-owned-tool-batch-executor.md) 随后把模型响应触发的工具执行移入 Session 持有的批次执行器，并增加注册冻结、完整预检、调用标识符账本和 `tool_protocol_error`。assistant 调用与全部结果仍由 Step 成组提交，中断仍在完整批次后生效，因此本 ADR 的生命周期边界没有改变；新 ADR 只细化了工具批次内部的状态所有权和结构失败分类。

ADR-0031 后来删除其中的 Session 级调用标识符账本，但没有改变 Step 的批次执行与成组提交边界。
