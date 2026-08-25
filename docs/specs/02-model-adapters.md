# 模型调用 contract 与协议 adapter

> 状态：实现中。当前问题位于 `mini_agent/llm/`、`mini_agent/config.py` 与 `mini_agent/cli.py`；离线回归将位于 `tests/test_llm_adapters.py`。

## 问题证据

`LLMClient` 会用域名子串判断端点，再对整段 URL 删除 `/anthropic` 和 `/v1`。下面的离线复现没有网络请求，却把用户配置的 host 与 path 一起改写：

```bash
.venv/bin/python -c 'from mini_agent.llm import LLMClient; from mini_agent.schema import LLMProvider; c=LLMClient(api_key="test-key", provider=LLMProvider.ANTHROPIC, api_base="https://api.minimax.io.evil/v1proxy", model="test-model"); print(c.api_base)'
```

2026-08-25 实测输出为 `https://api.minimax.io.evilproxy/anthropic`。另一个离线复现表明 `provider="typo"` 能通过配置解析，并被 CLI 的二分分支路由为 `openai`（`mini_agent/config.py:22-29`；`mini_agent/cli.py:567-568`）。

具体客户端还默认注入 Bearer `Authorization`、固定输出上限、`reasoning_split` 与丢失签名后的推理文本回传；当前能力记录没有任何真实端点探测（`mini_agent/llm/anthropic_client.py:24-80,133-140,234-247`；`mini_agent/llm/openai_client.py:25-76,160-225`；`docs/PROVIDER_CAPABILITIES.md:18`）。

## 本轮边界

- `AgentSession` 只依赖 `ModelClient` Protocol；Session 继续持有模型引用，adapter 不取得消息、Turn 或 Step 的所有权。
- core 传递 vendor-neutral 工具定义；每个 adapter 独占 SDK、认证、工具与消息的 wire 编码、响应和用量归一化。
- 组装点通过静态 registry 选择 adapter。新增 vendor 偏差时新增 concrete adapter，不在已有 adapter 中加入域名条件。
- `adapter`、`api_base`、`model` 与 `max_output_tokens` 显式配置；未知名称立即失败，`api_base` 原样交给 adapter。
- Anthropic-compatible 与 OpenAI-compatible 表示本地 wire adapter，不表示已采用或验证对应 vendor 服务；未经探测的推理 continuation、缓存用量和服务端上下文扩展不进入默认请求。

不在本轮范围：流式输出、错误分类、动态插件发现、真实端点能力探测、上下文策略、重写 agent loop，或切换本地 `config.yaml` 中的服务。

## 离线验证

1. 缺少显式 adapter 字段或使用未知名称时，配置解析失败；删除校验会使测试转红。
2. 历史上会触发 MiniMax 分支的 URL 逐字传给捕获型假 adapter；恢复域名推断或删除 registry 委托会使测试转红。
3. 假 SDK 捕获两个现有 adapter 的初始化与请求，验证中立消息和工具定义的 wire 编码、响应归一化，以及默认请求不含未探测扩展。
4. 现有 agent loop 离线集合继续验证同一注入模型被摘要与 Step 共用，Session、Turn、Step 及消息隔离不变量不变。
