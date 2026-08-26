# ADR-0027：在模型错误分类前不做项目级重试

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/llm/anthropic_client.py`、`mini_agent/llm/openai_client.py`、`tests/test_llm_adapters.py`、[P-006](../PITFALLS.md#p-006--关闭项目重试不等于-sdk-不会重试)

## 背景

项目级 retry 同时包含文件配置、运行时配置、退避计算、耗尽异常、CLI 回调与两套 adapter 接线；其中专用模块与测试共 308 行（`git show c7f6255:mini_agent/retry.py | wc -l`；`git show c7f6255:tests/test_retry.py | wc -l`）。CLI 又把 `retryable_exceptions` 固定成所有 `Exception`（`git show c7f6255:mini_agent/cli.py | nl -ba | sed -n '592,624p'`）。当前项目没有统一模型错误分类，无法区分认证、协议、永久参数错误与可安全重试的瞬时错误，能力清单也把错误分类列为待研究问题（`docs/BUILD_LIST.md:20`）。

因此实现已经能精确处理 `nan`、浮点溢出和开关所有权，却不能回答最先决定是否应重试的问题。P-006 还证明 SDK 默认策略是另一层独立所有权；它必须继续显式关闭，不能用删除项目层来默许 SDK 接管。

## 选项

1. **保留现有 retry，并先建立模型错误分类**：可以继续复用退避代码，但会把当前精简工作扩成未经端点证据支持的新模型调用 topic。
2. **保留固定的全异常 retry**：代码可以更短，但认证、参数和协议错误仍会被重复调用，错误语义没有变正确。
3. **删除项目级 retry，adapter 只调用一次**：SDK 保持 `max_retries=0`；模型错误原样进入 core，等错误分类有独立证据时再设计策略。

## 决定

采用选项 3。删除 `retry` 配置、运行时模块、CLI 专用回调与标签，以及 adapter 的 retry 参数；两个 adapter 的 `generate()` 各自直接等待一次 SDK `create()`，两个 SDK 客户端继续显式设置 `max_retries=0`。

不保留禁用壳或旧配置兼容分支。未知的 `retry` 字段由现有严格配置边界拒绝；模型异常继续遵守 ADR-0022 的中性文本和原对象 contract。本项不建立跨协议错误分类，也不改变 wire 编解码。

## 为什么否决其他的

**否决先做错误分类**：当前没有真实端点错误矩阵，也没有当前 topic 需要用它解锁；为了保留 146 行 retry 实现而展开错误体系，会继续让配套机制主导学习主线。若真实端点探测和任务级评测已经证明某类瞬时错误需要恢复，选项 1 反而是正确的独立研究项。

**否决全异常 retry**：短实现仍然把“调用失败”误当成“可安全重复调用”，并可能放大永久错误。若上游 contract 能保证进入该边界的错误全部是瞬时、请求可幂等重放且尝试次数可观察，固定策略才可能足够。

**不把策略交给 SDK**：SDK 的默认次数已经实测独立存在，但当前没有采用或探测其错误分类与退避语义。只有在项目明确采用并验证某个传输实现的策略后，SDK 才适合作为唯一所有者。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_llm_adapters.py tests/test_agent_session_offline.py tests/test_agent_loop_offline.py tests/test_background_shell_lifecycle.py` 实测 `113 passed in 0.61s`。
- `.venv/bin/python -m pytest -q` 实测 `286 passed, 9 deselected in 13.68s`；真实模型、用户 MCP 配置和网络测试未运行。
- 参数化 adapter 回归在两个 SDK `create()` 边界抛出同一个 `OSError`，分别断言只调用一次且抛出的对象身份不变（`tests/test_llm_adapters.py::test_adapters_attempt_once_and_preserve_model_error`）。
- SDK 构造断言继续锁定 `max_retries=0`，示例配置回归锁定不再公开 `retry` 字段（`tests/test_llm_adapters.py:210-268`）。

## 回头看

实现删除 308 行 retry 实现和专用测试，新增的关键回归只验证两个当前不变量：项目恰好调用一次，SDK 不在背后重试。失败语义仍由普通 `OSError` 证明，没有为已删除的异常类型保留兼容层。ADR-0017、ADR-0018 与 ADR-0021 因模块整体删除而被推翻；它们对当时既有模块的局部判断仍保留为历史证据。

2026-08-26：后续把两个单消费者 `_make_api_request()` 内联进 `generate()`，失败替身同步下沉到 SDK `create()`，上述一次调用与异常对象身份不变量不变；临时重建异常时两种 adapter 回归均转红。该私有结构精简没有改变本 ADR 的重试所有权决定。
