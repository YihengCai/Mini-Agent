# ADR-0021：重试模块单一持有 enabled 开关

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/retry.py`、`mini_agent/llm/anthropic_client.py`、`mini_agent/llm/openai_client.py`、`tests/test_retry.py`、提交 `262761f`

## 背景

运行时 `RetryConfig` 声明 `enabled`，但旧 `async_retry()` 完全不读取它；Anthropic-compatible 与 OpenAI-compatible adapter 各自在 `generate()` 复制一份启停分支，只有启用时才调用装饰器（`git show 262761f^:mini_agent/retry.py | nl -ba | sed -n '24,58p;87,143p'`；`git show 262761f^:mini_agent/llm/anthropic_client.py | nl -ba | sed -n '240,277p'`；`git show 262761f^:mini_agent/llm/openai_client.py | nl -ba | sed -n '233,268p'`）。

改动前用 `enabled=False,max_retries=2,initial_delay=0` 直接包装失败函数，实测仍调用 3 次、触发两次回调，并把原 `RuntimeError` 改写为 `RetryExhaustedError(attempts=3)`。同一个配置对象因入口不同而有两套执行语义。

## 选项

1. **继续由每个 adapter 解释开关**：保持当前模型路径行为，但公开重试工具继续违反自己的配置说明，每个新调用者都要复制同一分支。
2. **由重试 wrapper 单一解释开关**：禁用时在重试捕获边界外直接调用一次，adapter 始终使用同一入口。
3. **删除开关，以 `max_retries=0` 表示禁用**：字段更少，但启用且零次附加重试仍会包装耗尽错误，与当前禁用语义不同。

## 决定

采用选项 2。`async_retry()` 的 wrapper 在每次调用开始时检查 `config.enabled`；禁用时直接等待原函数并返回或抛出，不进入重试 `try/except`，所以不计算 delay、不等待、不调用回调，也不改写异常（`mini_agent/retry.py:87-146`）。

两个 wire adapter 无条件用该装饰器调用各自的 `_make_api_request()`，删除重复 `if/else`；协议参数与响应解析仍由各 adapter 持有（`mini_agent/llm/anthropic_client.py:240-269`；`mini_agent/llm/openai_client.py:233-261`）。CLI 仍只在启用时安装终端回调并显示提示，SDK 自带重试仍显式为零；本轮不改变这些观察与传输边界。

## 为什么否决其他的

**否决 adapter 各自解释**：重试策略应在执行重试的模块内完成，wire adapter 不应同时拥有协议编解码和通用控制流。若不同协议确实需要不同的启停语义或错误转换，各 adapter 持有分支才合理；当前两份逻辑逐项相同。

**否决用零次附加重试代替禁用**：`enabled=True,max_retries=0` 仍由重试层捕获第一次失败并产生 `RetryExhaustedError(attempts=1)`；`enabled=False` 的既有 adapter 行为是透传原异常。若公开 contract 将来统一规定所有单次调用失败也必须转换成耗尽错误，就可以删除 `enabled` 并仅保留次数。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_retry.py tests/test_llm_adapters.py` 实测 `65 passed in 0.33s`。
- 禁用回归断言恰好调用一次、原异常对象不变、回调与 `asyncio.sleep` 都未发生；现有零次附加重试回归继续要求 `RetryExhaustedError(attempts=1)`（`tests/test_retry.py:98-162`）。
- 两个 adapter 的禁用协议回归在删除各自分支后仍通过（`tests/test_llm_adapters.py:402-590`）。
- 临时移除 wrapper 的禁用短路时直接回归 1 项转红，实测 `1 failed in 0.30s`。
- 显式排除 `external` 的完整集合实测 `310 passed, 9 deselected in 13.12s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现用 3 行公共守卫净删两个 adapter 的重复控制流，并保持启用、禁用和启用但零次附加重试三者的既有模型路径语义。开关在每次 wrapper 调用时读取，没有把可变配置提前冻结在装饰时。
