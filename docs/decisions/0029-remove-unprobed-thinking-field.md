# ADR-0029：未探测推理状态不进入共享 schema

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/schema/schema.py`、`mini_agent/llm/anthropic_client.py`、`mini_agent/llm/openai_client.py`、`tests/test_llm_adapters.py`

## 背景

`Message` 与 `LLMResponse` 都公开可选 `thinking`，core、CLI 和 logger 继续复制、显示和序列化它；提交 `fc91f84` 的生产代码与测试共有 28 处引用（`git grep -n thinking fc91f84 -- mini_agent tests ':(exclude)mini_agent/skills' | wc -l`）。但两个 adapter 都固定返回 `thinking=None`，Anthropic-compatible 解析器会忽略响应中的 thinking block，历史编码也从不发送该字段（`git show fc91f84:mini_agent/llm/anthropic_client.py | nl -ba | sed -n '184,231p'`；`git show fc91f84:tests/test_llm_adapters.py | nl -ba | sed -n '278,374p;485,556p'`）。

这不是可往返能力：公开字段暗示 core 能保存推理状态，实际既没有端点探测，也没有 opaque 签名、跨请求续传或两种 wire 的共同 contract。

## 选项

1. **保留永远为空的字段**：未来扩展无需再次改 schema，但当前每层仍要维护没有生产者的数据。
2. **只保存可见推理文本**：演示时能显示内容，却会把 vendor 文本误当成可重放状态，并丢失可能必需的签名或块结构。
3. **从共享 schema 删除**：只保留已验证的文本、工具调用、终止原因和用量；探测完成后按真实 contract 重新引入。

## 决定

采用选项 3。从 `Message`、`LLMResponse`、core 消息构造、CLI 渲染和 logger 中删除 `thinking`；两个 adapter 不再填充 `None`。Anthropic-compatible 解析器继续忽略未知 thinking block，基础文本与工具历史编码保持不变。

本项不宣称目标端点不支持推理，也不禁止未来实现。重新引入需要先在 `docs/PROVIDER_CAPABILITIES.md` 记录当前端点的响应形状、签名往返与降级方案，再决定它属于 adapter 私有状态还是共享 schema。

## 为什么否决其他的

**否决空占位字段**：空字段不能验证字段类型、生命周期或安全往返，却让所有构造器、日志和 UI 都承担维护成本。若已经有下一阶段的已测 contract，且短期迁移兼容确实重要，保留占位才可能降低改动成本。

**否决只保存可见文本**：可读文本不等于可继续发送的协议状态；把它放回历史可能静默破坏签名或语义。若端点实测证明纯文本就是完整、可重放且跨协议一致的 contract，这个简单方案反而足够。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_llm_adapters.py tests/test_agent_loop_offline.py tests/test_agent_session_offline.py tests/test_tool_execution.py tests/test_tool_output_budget.py tests/test_session_integration.py -m 'not external'` 实测 `111 passed in 0.82s`。
- Anthropic fake 响应继续包含未探测 thinking block；结果 `model_dump()` 不含该字段，两个 adapter 的 assistant/tool 历史 wire 映射仍逐值断言（`tests/test_llm_adapters.py:278-374,484-554`）。
- `rg -n thinking mini_agent --glob '*.py' --glob '!mini_agent/skills/**'` 无输出。
- `.venv/bin/python -m pytest -q` 实测 `285 passed, 9 deselected in 13.46s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

生产链路净减 17 行，测试构造与断言净减 8 行；没有删除可见文本、工具历史或未知响应块的 wire 回归。实现没有遇到需要保留空字段的消费者，也没有把可见推理文本误升格成可重放状态。
