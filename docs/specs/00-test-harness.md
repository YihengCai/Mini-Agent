# LLM 测试替身与模型请求结构检查

> 状态：已实现并通过离线回归验证。实现位于 `tests/llm_test_double.py`，回归测试位于 `tests/test_agent_loop_offline.py`。

## 问题证据

现有 agent loop 测试会访问真实 API，部分测试还以返回布尔值代替断言，因此不能提供快速、便宜、稳定的回归反馈，见 [`UPSTREAM_AUDIT.md`](../UPSTREAM_AUDIT.md)。

agent Step 会传入工具列表调用 `llm.generate()`（`mini_agent/core/agent.py:608-646`），上下文摘要也会调用同一方法但省略 `tools`（`mini_agent/core/agent.py:403-497`）。测试替身必须能区分主循环调用与摘要调用，并检查两者的全局顺序。

## 已实现范围

- `ScriptedCall` 和 `ScriptedLLM` 定义一条带用途标签的全局调用序列；
- 深拷贝每次请求的消息，并保存稳定的工具定义快照；
- 意外调用、用途错位和未消费脚本都会由结束校验报告；
- 在消费脚本响应前检查工具调用与工具结果的配对结构；
- 用测试替身驱动真实 `AgentSession.start_turn()` 和内部 Step，不在测试中手工模拟消息追加。

不在本轮范围：本地 HTTP/SSE 假服务、真实端点评测、录制回放、事件层、任务基准测试。

## 已验证行为

1. 主循环和摘要调用按 `agent → agent → summary → agent` 的真实交错顺序消费脚本；
2. 预设响应不足或测试结束时仍有剩余响应都会失败；
3. 测试可以断言模型实际收到的消息和工具定义；
4. 空或重复的工具调用标识符，以及缺失、未知或重复的工具结果，会在模型请求边界失败；
5. 未知工具、工具异常、达到最大步数和正常结束已有独立回归覆盖；Turn 通过 `TurnOutcome` 返回结构化停止原因，不把它命名为任务成功；
6. 即使主调用或摘要调用捕获了测试替身异常，结束校验仍会使测试失败。

实际接口为 `ScriptedCall(purpose, result)` 与 `ScriptedLLM(calls)`。当前以 `tools is None` 识别 `summary`，否则识别 `agent`（`tests/llm_test_double.py:11-31,77-126`）。这是当前调用图的测试侧判定，不是生产 LLM contract；如果主循环将来允许 `tools=None`，必须改为显式用途信号。取舍见 [`ADR-0001`](../decisions/0001-strict-global-llm-call-script.md)。

配对检查只验证内部 `Message.tool_calls` 与 `Message.tool_call_id` 的标识符账本，不检查 adapter wire 格式、角色邻接或消息编码；这些由 `tests/test_llm_adapters.py` 的协议边界测试覆盖。
