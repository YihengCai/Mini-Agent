# Session、Turn、Step 生命周期与 CLI 观察边界

> 状态：已实现并通过离线回归验证。实现位于 `mini_agent/core/` 与 `mini_agent/cli_events.py`，取舍见 [ADR-0003](../decisions/0003-remove-acp-and-extract-core-loop.md) 和 [ADR-0004](../decisions/0004-session-turn-step-lifecycle.md)。

## 问题证据

第一次抽取 core 后，`Agent` 同时表示长期消息状态和一次性 `run() -> str`；正常回复、模型错误、中断和步数耗尽都压进字符串，只有可选事件保留停止原因。旧代码可用下列命令复查：

```bash
git show fdcd945^:mini_agent/core/agent.py
git show fdcd945^:mini_agent/cli.py
```

旧 CLI 两种入口都丢弃 `run()` 返回值，说明字符串不是实际控制边界；模型不再请求工具被称为 `completed`，又把 Turn 结束误写成任务完成。

## 已实现边界

- `AgentSession` 表示一段逻辑对话，持有模型可见历史、压缩状态、只读配置和至多一个活动 Turn（`mini_agent/core/agent.py:98-235`）。上下文压缩保留 Session 身份；CLI `/clear` 创建新 Session（`mini_agent/cli.py:722-730`）。
- `start_turn(user_input, event_sink=...)` 原子接纳输入并返回 `TurnHandle`。同一 Session 有活动 Turn 时拒绝新输入；等待调用者被取消不会取消私有 runner（`mini_agent/core/agent.py:174-230`；`mini_agent/core/turn.py:51-90`）。
- Turn 从客户端交出控制权开始，到 `end_turn`、`interrupted`、`max_steps` 或 `failed` 后交还控制权。`TurnOutcome` 可以带最后回复与结构化错误，但没有任务成功字段（`mini_agent/core/turn.py:10-48`）。
- Step 是一次 `purpose="agent"` 的模型请求、该响应中的全部工具调用及结果写入。工具调用会继续同一个 Turn；摘要请求是 Turn 维护，事件 `step=None`，不消耗 Step（`mini_agent/core/agent.py:294-402,506-825`）。
- 公开 core API 只有 Session、Turn 句柄、结果和观察事件；`_AgentLoop` 是 Session 内部实现，不能绕过接纳不变量（`mini_agent/core/agent.py:506-902`；`mini_agent/core/__init__.py:1-55`）。
- `AgentEventEnvelope` 为每个事件附加 Session、Turn 和可选 Step 身份；CLI 仍用同步接收器渲染终端并写原日志（`mini_agent/core/events.py:20-146`；`mini_agent/cli_events.py:26-168`）。

## 不变量

1. 一个 Session 同时至多一个活动 Turn；检查、预占、输入追加和 runner 创建失败回滚形成一个接纳边界。Turn 开始后使用接纳时的 Session 身份、模型引用、工具映射、步数与 token 上限。
2. 一个 agent Step 至多发起一次 agent 用途模型请求；如果响应含工具调用，同一 Step 执行完整批次，并把 assistant 工具调用与所有工具结果成组写入。下一次模型请求必定属于下一 Step。
3. `end_turn` 只表示模型没有继续请求工具；`max_steps` 只表示 Step 上限；两者都不代表用户任务正确。模型或内部失败使用 `failed`，且必须带错误细节。
4. 中断是合作式请求。完整工具批次、终止响应和模型失败优先完成当前 Step；下一 Step 不再启动。由此保留消息配对，但中断延迟可能等于一次模型调用加整批工具执行时间。
5. 模型请求消息与事件消息是两份快照；事件中的响应、工具定义、调用和结果也不能修改 Session 或真实模型输入。摘要请求遵守同一隔离规则。
6. 接收器首个异常会禁用该接收器。若它阻止循环继续，Turn 在安全边界以 `observer_error` 失败；若模型或内部失败已发生，则保留原主因并附观察错误。已成功交给接收器的 `TurnFinished.outcome` 与 `wait()` 返回同一个对象。
7. `TurnHandle.wait()` 屏蔽等待者取消，CLI 必须先请求中断并等待 runner 收敛，再传播应用取消或开始下一次输入（`mini_agent/cli.py:273-294,823-847`）。

## 未包含

- `TurnOutcome` 不是 TaskSupervisor 或 BenchmarkEvaluator；本轮没有 definition of done、SWE-bench 评分或自动继续策略。
- 同步事件不是 append-only 会话事实、持久化轨迹、回放格式或多客户端传输 contract。
- 当前中断不会取消正在等待的模型请求或工具协程，也不会回滚已经发生的工具副作用。
- steering 仍是独立研究主题；当前只保证活动 Turn 期间的新输入不会静默混入请求，未来可以在显式接纳边界扩展。
- 上下文压缩仍直接重写模型可见历史；事实记录与请求视图尚未分离。

## 已验证行为

- `tests/test_agent_session_offline.py:119-231`：跨 Turn 历史、唯一身份、单活动 Turn、可重入接纳和创建失败回滚；
- `tests/test_agent_session_offline.py:234-350`：等待者取消、CLI 收敛、配置快照和工具驱动的多 Step；
- `tests/test_agent_session_offline.py:353-607`：观察数据隔离、接收器错误、消息配对、内部错误和结构化停止原因；
- `tests/test_agent_session_offline.py:610-724`：完整 Step 中断、终止优先级与摘要不计 Step；
- README 的离线命令在 2026-08-24 实测为 `142 passed`，并有一条既有的 `cache_dir` 配置警告。
