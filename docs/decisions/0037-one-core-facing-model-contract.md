# ADR-0037：只保留一个 core-facing 模型 contract

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/llm/protocol.py`、`mini_agent/llm/factory.py`、`mini_agent/llm/anthropic_client.py`、`mini_agent/llm/openai_client.py`、`tests/test_llm_adapters.py`

## 背景

`ModelClient` Protocol 已经定义 core 唯一需要的 `generate(messages, tools)` 行为，离线 `ScriptedLLM` 直接结构化满足它而不继承 SDK 类型（`mini_agent/llm/protocol.py:18-25`；`tests/llm_test_double.py`）。改动前提交 `9a2f9d6` 的 `mini_agent/llm/base.py` 又用 53 行 `LLMAdapter` ABC 重复同一方法；执行 `rg -n 'LLMAdapter' mini_agent tests --glob '*.py'`，消费者只有两个具体 adapter 的继承、factory 类型标注和包导出，没有构造、类型检查或第三方实现。

基类另外保存 `api_key` 与 `api_base`，全仓只有赋值、没有读取；两个具体 adapter 实际只共享正数输出上限校验以及 `model`、`max_output_tokens` 两个状态。

## 选项

1. **保留 Protocol 与 ABC**：继续提供继承扩展点和共享初始化，但维护两份相同调用 contract 与无消费者状态。
2. **删除 Protocol，让 core 依赖 ABC**：只剩一个名义 contract，却要求离线替身和所有新模型实现继承 SDK 侧基类。
3. **删除 ABC，保留 Protocol**：两个具体 adapter 各自保留三行校验/状态，factory 返回结构化 `ModelClient`。
4. **新增配置 mixin 或数据类**：集中三行初始化，但再引入一层没有独立状态所有权的抽象。

## 决定

采用选项 3。删除 `mini_agent/llm/base.py` 与公开 `mini_agent.llm.LLMAdapter`；Anthropic-compatible 和 OpenAI-compatible adapter 不再共享继承层，各自在 SDK 构造前拒绝非正 `max_output_tokens`，并持有自身请求需要的模型与预算。`create_model_client()` 的返回标注改为 core 实际依赖的 `ModelClient`。

具体 adapter 的构造参数、静态注册表、wire 编解码、SDK `max_retries=0`、core 调用和测试替身均不变。本轮不增加动态 adapter、vendor 能力、CLI 或工具改动。

## 为什么否决其他的

**否决同时保留 Protocol 与 ABC**：当前继承点没有第二类共享机制，认证与端点已直接交给各自 SDK，再保存一份只会扩大密钥驻留和公开面。若项目作为版本化库发布，已有仓库外 adapter 继承 `LLMAdapter`，保留并明确支持该扩展 contract 才是正确选择。

**否决让 core 依赖 ABC**：core 的价值在于只要求行为，脚本化 LLM 因此能离线验证 agent loop 而不继承传输实现。若所有模型客户端都由项目封闭控制，并且它们确实共享必须统一执行的生命周期或资源所有权，名义基类才可能比 Protocol 更合适。

**否决新增 mixin 或数据类**：为三行相同代码制造新模块会重新得到本次要删除的间接层。若第三个 adapter 出现后共享校验扩展为多个字段、错误类型和不可变配置快照，抽取一个不含 wire 方法的配置对象才值得重新评估。

## 怎么验证它是对的

- `.venv/bin/python -m pytest tests/test_llm_adapters.py tests/test_agent_loop_offline.py tests/test_agent_session_offline.py -q` 实测 `82 passed in 0.88s`。
- 两个具体 adapter 的 `0/-1` 回归固定原正数守卫，包导出回归固定 `LLMAdapter` 不再存在；临时删除守卫并恢复公开名字时 5 项实测转红。
- `.venv/bin/python -m pytest -q` 实测 `257 passed, 5 deselected in 13.34s`；外部 MCP 与网络测试本次未运行。

## 回头看

最终生产代码净减 49 行，测试净增 20 行。删除的是重复 contract、无消费者认证镜像和公开继承点；具体 adapter 仍通过相同 factory 参数构造，完整 SDK 请求断言与 core 脚本化 LLM 集合保持绿色，没有出现需要共享生命周期基类的证据。
