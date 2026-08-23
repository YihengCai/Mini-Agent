# ADR-0001：LLM 测试替身使用带用途标签的全局调用序列

- 日期：2026-08-24
- 状态：已采纳
- 关联：`tests/llm_test_double.py:11-31,77-146`、`tests/test_agent_loop_offline.py:286-355`

## 背景

主 agent loop 与摘要都调用同一个 `generate()`；前者传工具列表，后者省略 `tools`，见 `mini_agent/agent.py:275-283,338-345`。两处还都会捕获模型异常，见 `mini_agent/agent.py:289-292,344-356`。因此测试替身既要给两类调用提供不同响应，也要在异常被捕获后保留违规证据。

实现第一条工具循环测试后，出现了三个都能落地的脚本组织方式：无标签 FIFO、按用途分队列、带用途标签的全局序列。它们对调用错序的可观察性不同，会影响后续所有 agent loop 回归测试。

## 选项

1. **无标签 FIFO**：接口最小，但主调用与摘要调用交换顺序后仍可能把响应全部消费完。
2. **按用途分队列**：两类响应不会互相消费，但只能检查各自内部顺序，可能漏掉两类调用的全局错序。
3. **带用途标签的全局序列**：每个脚本项同时声明用途和结果，检查调用数量、用途与全局顺序。
4. **修改生产 `generate()` contract，显式传用途**：语义最强，但仅为测试修改运行时接口和所有客户端。

## 决定

选择带用途标签的全局序列。`ScriptedCall` 按唯一序列保存 `agent` 或 `summary`；测试替身根据当前调用约定将 `tools is None` 识别为摘要，其余识别为主调用。用途错位时不消费当前脚本项，并把首个违规保存到结束校验，见 `tests/llm_test_double.py:80-134`。

这个分类只属于当前测试边界，不是生产模型 contract。如果将来出现不带工具的普通模型调用，必须重新评估分类依据。

## 为什么否决其他的

- **无标签 FIFO**：当前要检查摘要与主循环交错顺序，仅检查响应总数不够。如果被测对象只有一种模型调用，或调用用途不影响状态，无标签 FIFO 反而更简单。
- **按用途分队列**：当前 loop 是串行状态机，全局顺序本身就是待验证行为；分队列会放弃这部分证据。如果以后出现并发后台模型任务，而且不同用途间的完成顺序明确不属于 contract，分队列或匹配器反而更合适。
- **修改生产 contract**：现有 `Agent` 已能通过鸭子类型注入替身，见 `mini_agent/agent.py:21-30`，无需扩大改动。如果运行时需要统一的调用追踪、计费或模型路由，显式用途参数反而可能成为正确的生产设计，但应由对应需求推动。

## 怎么验证它是对的

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_agent_loop_offline.py
```

`test_summary_and_agent_calls_follow_one_global_sequence` 运行两轮真实 `Agent.run()`，验证实际序列为 `agent → agent → summary → agent`。`test_call_purpose_mismatch_remains_visible_after_caller_catches_error` 与 `test_first_violation_prevents_later_script_consumption` 验证错序不会被异常处理隐藏。

最小变异实测：临时删除 `tests/llm_test_double.py:115-121` 的用途比较后，前一个错序测试以 `DID NOT RAISE` 转红；恢复后通过。

## 回头看

实现没有修改 `mini_agent/**` 或新增依赖。专属测试本次实测为 `20 passed`，推荐离线集合加专属测试为 `97 passed`。当前尚未出现并发或不确定顺序，因此没有引入非严格模式或任意请求匹配器。
