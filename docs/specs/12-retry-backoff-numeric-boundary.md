# 重试退避的数值边界

> 状态：已实现。文件配置约束位于 `mini_agent/config.py:32-39`，运行时校验与计算位于 `mini_agent/retry.py:24-75`，离线回归位于 `tests/test_llm_adapters.py:274-312` 与 `tests/test_retry.py:13-94`；取舍见 [ADR-0018](../decisions/0018-finite-and-saturating-retry-backoff.md)。

## 问题证据

负延迟会绕过退避，`nan` 等待不会正常收敛；有限但很大的底数会在 `min(..., max_delay)` 执行前溢出，使配置的有限上限失效。

## 本轮不变量

1. YAML 与程序化运行时都只接受有限、非负的 `initial_delay` 与 `max_delay`。
2. `exponential_base` 必须有限且为正；`0 < base < 1` 仍是合法的递减策略。
3. `max_delay` 可以小于 `initial_delay`，首次间隔也按上限截断。
4. 零初值不执行可能溢出的幂运算，任何 attempt 都返回零。
5. 其他有限输入的幂运算若溢出，结果按有限 `max_delay` 饱和；普通计算保持原公式。

## 不在范围

不改变重试次数、`enabled`、回调、adapter、异常分类、时间单位或 SDK 重试；不验证内部 attempt 参数，也不引入对数、高精度或随机抖动算法。

## 离线验证

- 两个入口分别拒绝负 delay、非正 base 以及三个字段的 `nan`/`inf`；
- 两个入口都接受零 delay 与 `base=0.5`；
- 计算回归覆盖递减、首次截断、零初值巨大底数和非零初值巨大底数；
- 删除任一输入边界、零初值短路或溢出饱和时，对应回归转红。
