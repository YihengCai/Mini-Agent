# 假 LLM + 可失败测试底座

> 状态：待实现。这里只定义第一阶段；任务级 eval 等机制落地后再建。

## 要解决的问题

现有 agent 测试无法区分正常结束、模型错误和 harness 错误。`tests/test_agent.py` 没有有效断言，异常分支返回布尔值也不会让 pytest 失败；详见 [P-004](../PITFALLS.md)。此外，`Agent._create_summary()` 也会调用 `llm.generate()`（`mini_agent/agent.py:275-283`），所以单一 FIFO fake 会在压缩触发时错消费主循环响应。

## 本阶段边界

只做三件事：

1. `tests/fakes.py`：按请求形状路由的 `FakeLLM`。
2. `assert_history_valid(messages)`：在每次 fake 请求前检查 tool-call 配对。
3. 两个离线测试文件：验证主循环与压缩路径能被确定性地打红。

明确后置：git worktree runner、并发 worker、价格表、Markdown 报告、12-task suite、真实 API 基准。它们要用已经落地的事件流和机制指标，而不是先造一套以后会重接的 eval 平台。

## FakeLLM 契约

采用 ADR-0005 的两条有序队列：

- `tools is None` → `compact` 队列；
- `tools is not None` → `agent` 队列；
- 队列耗尽抛 `FakeLLMExhausted`；
- 每次请求深拷贝进 `requests`；
- 测试结束必须调用 `assert_consumed()`，多写、少写脚本都失败。

暂不做任意 matcher。出现第三类稳定调用方时再增加 route；当前提前开放规则语言只会把 fake 变成第二套 prompt 实现。

## 全局历史不变量

`assert_history_valid()` 至少检查：

- 每个 `assistant.tool_calls[*].id` 在下一条 assistant 之前恰好有一个对应 `role="tool"`；
- 每条 tool 消息引用已出现的 id；
- id 不重复；
- 发给 provider 的 content block 不为空。

检查器放在 fake 的 `generate()` 入口，而不是只在某个专用测试里调用；这样后续每个离线循环测试都会顺带覆盖历史形状。

## 第一批工件

`tests/test_loop_scripted.py`：

- 一个 assistant 返回两个 tool call，结果按 id 配对并进入下一次请求；
- 未知工具和工具异常成为失败 tool result，循环继续；
- 少一条 fake 响应时测试失败，而不是返回默认答案；
- 达到 `max_steps` 与正常结束能被测试区分。

`tests/test_compactor_scripted.py`：

- 强制触发一次摘要调用，证明 `compact` 队列不会消费 `agent` 队列；
- 摘要失败路径仍满足历史不变量；
- 同一个 fixture 人为删除 tool result 后，检查器必须变红。

验收命令必须离线运行，且不得导入或构造真实 provider client。

## 后续扩展条件

事件缝落地后，`JsonlSink` 成为唯一运行记录器；那时再增加结构化结束原因、usage 和工具耗时。至少两个机制已经落地并有真实 before/after 问题之后，才建立小型任务回归套件。
