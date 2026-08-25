# 重试 enabled 开关的单一所有权

> 状态：已实现。配置与执行入口位于 `mini_agent/retry.py:24-58,87-146`，两个 wire adapter 的统一调用位于 `mini_agent/llm/anthropic_client.py:240-269` 与 `mini_agent/llm/openai_client.py:233-261`，离线回归位于 `tests/test_retry.py:98-162`；取舍见 [ADR-0021](../decisions/0021-retry-module-owns-enabled-switch.md)。

## 问题证据

旧 `async_retry()` 忽略自己配置对象的 `enabled`，只有两个 adapter 在外层复制分支；直接使用公开装饰器时，禁用配置仍会重试、回调并转换异常（`git show 262761f^:mini_agent/retry.py | nl -ba | sed -n '87,143p'`）。

## 本轮不变量

1. `enabled=False` 时被包装函数恰好调用一次。
2. 禁用时不计算或等待 delay，不调用重试回调。
3. 禁用时成功值原样返回，失败异常对象原样抛出。
4. `enabled=True,max_retries=0` 仍属于启用状态：调用一次，失败时产生 `RetryExhaustedError(attempts=1)`。
5. 两个 wire adapter 都经过同一个 `async_retry()` 入口，不再各自解释开关。
6. 开关在每次 wrapper 调用时读取，不在创建装饰器时冻结。

## 不在范围

不改变配置解析、数值边界、delay 算法、回调参数、可重试异常集合、耗尽错误、CLI 提示或 SDK `max_retries=0`。

## 离线验证

- 禁用失败函数只调用一次，原异常身份不变，回调和 sleep 记录为空；
- 启用且零次附加重试的既有回归继续产生一次尝试的耗尽错误；
- 两种 adapter 的禁用协议请求在删除分支后保持；
- 移除公共禁用短路时，对应回归转红。
