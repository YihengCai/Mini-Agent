# core 的模型失败语义

> 状态：已实现。模型异常边界位于 `mini_agent/core/agent.py`，事件原对象位于 `mini_agent/core/events.py`，离线回归位于 `tests/test_agent_session_offline.py:516-540`；取舍见 [ADR-0022](../decisions/0022-core-preserves-model-error-semantics.md)。

## 问题证据

旧 core 把 `RetryExhaustedError.attempts` 的总调用次数重新标成重试次数；一次首次调用、零次附加重试会被记录成 “1 retries”（`git show 11a0bcf^:mini_agent/core/agent.py | nl -ba | sed -n '346,380p'`）。

## 本轮不变量

1. core 对所有 `ModelClient.generate()` 异常使用同一中性格式。
2. 异常自己的字符串逐字保留在 `LLM call failed: ` 前缀之后。
3. `ModelCallFailed.error` 保留同一个原异常对象。
4. `ModelCallFailed.result` 与 `TurnError.message` 使用同一文本。
5. core 不导入具体调用策略的错误类型，也不重新解释其字段。

## 不在范围

不改变事件结构、`TurnErrorKind`、停止原因、CLI 普通错误显示、adapter 或跨协议模型错误分类。项目级 retry 的删除由 [ADR-0027](../decisions/0027-no-project-retry-before-error-classification.md) 单独记录。

## 离线验证

- 构造普通 `OSError("endpoint down")`，事件和 outcome 都保留异常自身文本；
- 事件中的异常与测试构造对象身份相同；
- 恢复 core 对具体错误类型的特判时，对应回归转红；
- 普通模型错误与 CLI 生命周期定向集合保持通过。
