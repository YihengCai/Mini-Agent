# core 的模型失败语义

> 状态：已实现。模型异常边界位于 `mini_agent/core/agent.py:346-373`，事件原对象位于 `mini_agent/core/events.py:53-56`，离线回归位于 `tests/test_agent_session_offline.py:517-544`；取舍见 [ADR-0022](../decisions/0022-core-preserves-model-error-semantics.md)。

## 问题证据

旧 core 把 `RetryExhaustedError.attempts` 的总调用次数重新标成重试次数；一次首次调用、零次附加重试会被记录成 “1 retries”（`git show 11a0bcf^:mini_agent/core/agent.py | nl -ba | sed -n '346,380p'`）。

## 本轮不变量

1. core 对所有 `ModelClient.generate()` 异常使用同一中性格式。
2. 异常自己的字符串逐字保留在 `LLM call failed: ` 前缀之后。
3. `ModelCallFailed.error` 保留同一个原异常对象。
4. `ModelCallFailed.result` 与 `TurnError.message` 使用同一文本。
5. core 不导入具体 retry 类型，也不重新命名其字段。
6. CLI 可以继续依据事件中的原异常对象选择显示类别。

## 不在范围

不改变事件结构、`TurnErrorKind`、停止原因、CLI 标签、重试异常、adapter 或跨协议模型错误分类。

## 离线验证

- 构造 `attempts=1` 的耗尽异常，事件和 outcome 都保留异常自身的 “1 attempts”；
- 事件中的异常与测试构造对象身份相同；
- 恢复 core 的具体重试特判时，对应回归转红；
- 普通模型错误与 CLI 生命周期定向集合保持通过。
