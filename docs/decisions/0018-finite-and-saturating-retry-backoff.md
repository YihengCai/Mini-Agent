# ADR-0018：退避数值有限且溢出时按上限饱和

- 日期：2026-08-25
- 状态：已推翻（见 ADR-0027）
- 关联：`mini_agent/config.py`、`mini_agent/retry.py`、`tests/test_llm_adapters.py`、`tests/test_retry.py`、提交 `1ce3dd6`

## 背景

文件配置与运行时 `RetryConfig` 原样接受负延迟、非正底数以及 `nan`/`inf`，`calculate_delay()` 则先计算幂和乘法、最后才取 `max_delay`（`git show 1ce3dd6^:mini_agent/config.py | nl -ba | sed -n '32,50p'`；`git show 1ce3dd6^:mini_agent/retry.py | nl -ba | sed -n '23,75p'`）。

改动前离线探针实测：文件配置接受 `initial_delay=nan`，运行时接受 `initial_delay=-1`，`asyncio.sleep(nan)` 在 0.05 秒内没有返回；`initial_delay=1, max_delay=60, exponential_base=1e308, attempt=2` 在应用上限前抛出 `OverflowError(34, 'Result too large')`。因此配置上限既不能保证等待有效，也不能保证计算结果有界。

## 选项

1. **只验证有限且常规递增的退避**：要求 delay 非负、`base >= 1`、`max_delay >= initial_delay`，规则直观但会拒绝已有的递减与首次即截断策略。
2. **验证数学定义域并保留既有合法形状**：delay 有限且非负、base 有限且为正；允许 `0 < base < 1` 与较小上限，零初值短路，有限幂溢出按上限饱和。
3. **改用对数或高精度计算预判上限**：可以避免异常控制流，但增加舍入、零值与递减底数的分支，当前没有精确大数结果需求。

## 决定

采用选项 2。Pydantic 字段与运行时构造入口都要求 `initial_delay`、`max_delay` 有限且 `>= 0`，`exponential_base` 有限且 `> 0`（`mini_agent/config.py:32-39`；`mini_agent/retry.py:24-58`）。`0 < base < 1` 继续表示递减间隔，`max_delay < initial_delay` 继续让首次间隔也受上限截断。

`initial_delay == 0` 在幂运算前直接返回零；其他有限输入若幂运算抛 `OverflowError`，返回有限 `max_delay`，普通结果仍使用既有 `min(delay, max_delay)`（`mini_agent/retry.py:60-75`）。本轮不改变重试次数、`enabled`、回调、adapter、异常分类、时间单位或真实模型调用。

## 为什么否决其他的

**否决强制常规递增关系**：当前公式明确定义了正的递减底数和首次截断，审计没有发现这两种合法形状会破坏 agent loop；额外拒绝只是在替用户选择策略。若产品 contract 明确要求单调不减的 exponential backoff，并依赖该性质计算请求压力，`base >= 1` 与上限关系校验反而应该加入。

**否决对数或高精度预判**：本项目只需要交给 `asyncio.sleep()` 的有界浮点秒数；溢出本身已经准确表示结果超过任何有限上限。若未来需要在不设有限上限时展示精确超大间隔，或对退避曲线做数值分析，对数/高精度方案才更合适。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_retry.py tests/test_llm_adapters.py` 实测 `64 passed in 0.43s`。
- 两层测试分别覆盖 10 类负数、零值与非有限输入；合法边界覆盖零 delay、`base=0.5`、首次截断、零初值巨大底数与非零初值巨大底数（`tests/test_llm_adapters.py:274-312`；`tests/test_retry.py:13-94`）。
- 临时移除文件或运行时约束时分别实测 10 项转红；移除零初值短路或溢出捕获时分别实测 1 项转红。
- 显式排除 `external` 的完整集合实测 `305 passed, 9 deselected in 13.17s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现只在两个输入边界增加相同定义域，并让原有 `max_delay` 在浮点溢出时真正成为上限。没有把“指数退避”的常见形状误当成必要不变量，也没有为最终只需有限秒数的计算引入新依赖或更复杂数值表示。

2026-08-26：项目级 retry 整体由 [ADR-0027](0027-no-project-retry-before-error-classification.md) 删除，本决策随之被推翻。数值边界对保留该算法的系统仍然有效；当前项目先删除尚无错误分类依据的策略，不再为退避计算保留代码。
