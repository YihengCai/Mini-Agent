# ADR-0022：core 保留模型异常自身语义

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/agent.py`、`mini_agent/core/events.py`、`tests/test_agent_session_offline.py`、提交 `11a0bcf`

## 背景

`RetryExhaustedError.attempts` 明确定义并存储实际调用次数，`max_retries=0` 的首次失败因此得到 `attempts=1`（`mini_agent/retry.py:78-84,117-126`；`tests/test_retry.py:134-162`）。旧 core 捕获所有模型异常后，却导入这个具体类型，把 `attempts` 改写成 “retries”，并自行重组最后异常（`git show 11a0bcf^:mini_agent/core/agent.py | nl -ba | sed -n '346,380p'`）。

改动前离线探针给 Session 注入 `RetryExhaustedError(OSError("endpoint down"), attempts=1)`：异常自身是 `Retry failed after 1 attempts`，但 `ModelCallFailed.result` 与 `TurnError.message` 都变成 `LLM call failed after 1 retries`。零次附加重试因而被报告为一次重试。

## 选项

1. **只把 core 文案中的 retries 改成 attempts**：修正数字名称，但 core 继续依赖重试实现并复制异常格式。
2. **core 对所有模型异常统一保留 `str(error)`**：只添加中性的模型调用上下文，结构化事件继续携带原异常对象。
3. **立即建立统一模型错误分类与结构化调用计数**：可以避免解析文本，但会扩大 ModelClient、adapter、事件和 CLI contract。

## 决定

采用选项 2。模型调用抛出的任何 `Exception` 都统一生成 `LLM call failed: {error}`；同一文本进入 `ModelCallFailed.result` 与 `TurnError.message`，原异常对象继续由 `ModelCallFailed.error` 持有（`mini_agent/core/agent.py:346-373`；`mini_agent/core/events.py:53-56`）。

core 不再导入或识别 `RetryExhaustedError`。CLI 仍可从事件的原异常对象区分重试耗尽与普通模型失败，本轮不改变显示标签、事件字段、停止原因、异常分类或 adapter。

## 为什么否决其他的

**否决只改名**：正确的单位仍会被 core 复制一次，未来重试异常增加字段或调整文案时仍可能再次分叉。若 core contract 本身要求暴露结构化 attempts，而且所有 ModelClient 都遵循同一字段，core 显式处理才合理。

**否决立即统一错误分类**：当前只证实了一个既有异常被错误重解释，没有证据支持跨协议的可重试性、状态码或错误类别 contract。若流式响应或真实端点错误评测需要确定性策略，方案 3 才应作为独立模型调用扩展进入。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_agent_session_offline.py tests/test_agent_loop_offline.py tests/test_retry.py` 实测 `66 passed in 0.49s`。
- 新回归用一次调用的耗尽异常检查事件原对象身份、事件文本、Turn 错误文本和 `attempts` 单位（`tests/test_agent_session_offline.py:517-544`）。
- 临时恢复 core 的重试特判时该回归 1 项转红，实测 `1 failed in 0.48s`。
- 显式排除 `external` 的完整集合实测 `311 passed, 9 deselected in 13.27s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现净删 core 的具体重试依赖，同时让重试异常自己的调用次数语义贯穿事件和 Turn outcome。它没有把异常字符串提升为可机器判断的错误分类；策略消费者仍应依赖结构化事件字段和原异常对象。
