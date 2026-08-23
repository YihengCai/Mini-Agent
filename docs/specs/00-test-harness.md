# FakeLLM + 测试框架

> 状态：待实现。这里只定义第一阶段；任务级评测等机制实现后再建。

## 要解决的问题

现有 agent 测试无法区分正常结束、模型错误和测试框架错误。`tests/test_agent.py` 没有有效断言，异常路径返回 bool 也不会让 pytest 失败；详见 [P-004](../PITFALLS.md)。此外，`Agent._create_summary()` 也会调用 `llm.generate()`（`mini_agent/agent.py:275-283`），所以单个 FIFO 模拟模型会在压缩触发时消费错误的主循环响应。

## 范围

只做三件事：

1. `tests/fakes.py`：按请求路由的 `FakeLLM`。
2. `assert_history_valid(messages)`：在每次模拟请求前检查工具调用配对。
3. 两个离线测试文件：验证主循环与压缩路径能以确定的方式失败。

不在范围内：Git 工作树运行器、并行工作进程、价格表、Markdown 报告、12 项任务测试集、真实 API 基准测试。这些功能需要稳定的事件流和机制指标；现在不建立以后还要重新接线的评测平台。

## FakeLLM contract

采用 ADR-0005 的两条有序队列：

- `tools is None` → `compact` 队列；
- `tools is not None` → `agent` 队列；
- 队列耗尽时抛出 `FakeLLMExhausted`；
- 每次请求深拷贝后写入 `requests`；
- 测试结束时必须调用 `assert_consumed()`，预设响应多写或少写都失败。

暂不支持任意匹配规则。出现第三类稳定调用方时再增加路由；现在加入规则语言只会让模拟模型变成第二套提示词实现。

## 全局消息历史不变量

`assert_history_valid()` 至少检查：

- 每个 `assistant.tool_calls[*].id` 在下一条 assistant 之前恰好有一个对应 `role="tool"`；
- 每条工具消息引用已经出现的 ID；
- ID 不重复；
- 发给模型服务的内容块非空。

检查器放在模拟模型的 `generate()` 入口，而不是只在专用测试中调用；后续每个离线循环测试都会覆盖消息历史结构。

## 第一批测试

`tests/test_loop_scripted.py`：

- 一个 assistant 返回两个工具调用，结果按 ID 配对并进入下一次请求；
- 未知工具和工具异常成为错误工具结果，循环继续；
- 缺一条模拟响应时测试失败，而不是返回默认答案；
- 达到 `max_steps` 与正常结束能被测试区分。

`tests/test_compactor_scripted.py`：

- 强制触发一次摘要请求，证明 `compact` 队列不会消费 `agent` 队列；
- 摘要失败路径仍满足消息历史不变量；
- 从测试样例删除工具结果后，检查器必须失败。

验证命令必须离线运行，且不得导入或创建真实模型客户端。

## 后续条件

事件层完成后，`JsonlSink` 成为唯一的运行记录器；那时再增加结构化结束原因、token 用量和工具耗时。至少两个机制已有真实的修改前后对照后，才建立小型任务回归测试集。
