# 重试次数的双入口边界

> 状态：已实现。文件配置约束位于 `mini_agent/config.py:32-39`，运行时守卫与循环位于 `mini_agent/retry.py:23-131`，离线回归位于 `tests/test_llm_adapters.py:255-271` 与 `tests/test_retry.py:1-41`；取舍见 [ADR-0017](../decisions/0017-nonnegative-retry-count-at-config-and-runtime.md)。

## 问题证据

负 `max_retries` 会让 `range(max_retries + 1)` 为空。改动前探针以 `-1` 包装失败调用，函数一次也没有执行，装饰器却抛出与原错误无关的 `Exception("Unknown error")`。

## 本轮不变量

1. YAML 与程序化 `RetryConfig` 都要求 `max_retries >= 0`。
2. `max_retries` 表示首次调用之外允许的附加次数；`0` 必须执行一次。
3. 每次耗尽都抛 `RetryExhaustedError`，`attempts` 等于实际调用次数，并保留最后异常对象。
4. 合法计数保证循环至少进入一次，不保留零次循环专用状态或通用错误尾分支。

## 不在范围

不约束 `initial_delay`、`max_delay` 或 `exponential_base`，不改变 `enabled` 开关、回调时机、可重试异常集合、adapter、SDK 重试或异常分类。

## 离线验证

- 两个入口分别拒绝 `-1`，删除任一守卫都会使自己的回归转红；
- `0` 恰好调用一次，`2` 恰好调用三次；
- 两种耗尽路径都核对尝试次数和最后异常对象。
