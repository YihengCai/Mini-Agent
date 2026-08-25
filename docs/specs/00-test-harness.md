# LLM 测试替身与模型请求结构检查

> 状态：已实现并通过离线回归验证。实现位于 `tests/llm_test_double.py`，回归测试位于 `tests/test_agent_loop_offline.py`；用途标签的后续删除见 [ADR-0006](../decisions/0006-remove-legacy-local-compaction.md)。

## 问题证据

现有 agent loop 测试会访问真实 API，部分测试还以返回布尔值代替断言，因此不能提供快速、便宜、稳定的回归反馈，见 [`UPSTREAM_AUDIT.md`](../UPSTREAM_AUDIT.md)。

每个 agent Step 都会调用一次 `llm.generate()`，模型响应又决定是否执行工具并进入下一 Step（`mini_agent/core/agent.py:294-410`）。测试替身必须用同一个严格序列检查实际调用数量和顺序，并让被业务错误处理捕获的测试违规在结束校验时仍然可见。

## 已实现范围

- `ScriptedCall` 和 `ScriptedLLM` 定义一条严格 FIFO 的全局调用序列；
- 深拷贝每次请求的消息，并保存稳定的工具定义快照；
- 意外调用、未消费脚本和首个请求结构违规都会由结束校验报告；
- 在消费脚本响应前检查工具调用与工具结果的配对结构；
- 用测试替身驱动真实 `AgentSession.start_turn()` 和内部 Step，不在测试中手工模拟消息追加。

不在本轮范围：本地 HTTP/SSE 假服务、真实端点评测、录制回放、事件层、任务基准测试。

## 已验证行为

1. 所有模型调用按一个 FIFO 消费脚本，第一次违规后不会继续消费预设响应；
2. 预设响应不足或测试结束时仍有剩余响应都会失败；
3. 测试可以断言模型实际收到的消息和工具定义；
4. 空或重复的工具调用标识符，以及缺失、未知或重复的工具结果，会在模型请求边界失败；
5. 未知工具、工具异常、达到最大步数和正常结束已有独立回归覆盖；Turn 通过 `TurnOutcome` 返回结构化停止原因，不把它命名为任务成功；
6. 即使 agent loop 捕获了测试替身异常，结束校验仍会使测试失败。

实际接口为 `ScriptedCall(result)` 与 `ScriptedLLM(calls)`（`tests/llm_test_double.py:12-27,73-119`）。请求快照仍区分 `tools=None` 与空工具列表，但不再从它们推断调用用途。带用途标签的旧决定及其适用条件见已推翻的 [`ADR-0001`](../decisions/0001-strict-global-llm-call-script.md)；删除原因见 [`ADR-0006`](../decisions/0006-remove-legacy-local-compaction.md)。

配对检查只验证内部 `Message.tool_calls` 与 `Message.tool_call_id` 的标识符账本，不检查 adapter wire 格式、角色邻接或消息编码；这些由 `tests/test_llm_adapters.py` 的协议边界测试覆盖。
