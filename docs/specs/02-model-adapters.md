# 模型调用 contract 与协议 adapter

> 状态：已完成。实际边界见 `mini_agent/llm/protocol.py`、`mini_agent/llm/factory.py` 与 `tests/test_llm_adapters.py`；取舍见 [ADR-0005](../decisions/0005-explicit-model-api-adapters.md)。

## 问题证据

实现前的 `LLMClient` 会用域名子串判断端点，再对整段 URL 删除 `/anthropic` 和 `/v1`。下面的命令从提交 `157928f` 提取历史代码，不访问网络，却会改写用户配置的主机名与路径：

```bash
repro_python="$PWD/.venv/bin/python"
repro_dir="$(mktemp -d)"
git archive 157928f mini_agent | tar -x -C "$repro_dir"
(
  cd "$repro_dir"
  "$repro_python" -c 'from mini_agent.llm import LLMClient; from mini_agent.schema import LLMProvider; c=LLMClient(api_key="test-key", provider=LLMProvider.ANTHROPIC, api_base="https://api.minimax.io.evil/v1proxy", model="test-model"); print(c.api_base)'
  "$repro_python" -c 'from mini_agent.config import LLMConfig; from mini_agent.schema import LLMProvider; c=LLMConfig(api_key="test-key", provider="typo"); routed=LLMProvider.ANTHROPIC if c.provider.lower() == "anthropic" else LLMProvider.OPENAI; print("configured", c.provider); print("routed", routed.value)'
)
```

2026-08-25 实测输出为 `https://api.minimax.io.evilproxy/anthropic`、`configured typo` 和 `routed openai`。当前回归保留这个相似域名，删除逐字传递或恢复域名推断后会转红（`tests/test_llm_adapters.py:153-206`）。

当时的具体客户端还默认注入 Bearer `Authorization`、固定输出上限、`reasoning_split` 与丢失签名后的推理文本回传；能力记录没有任何真实端点探测。当前假 SDK 测试用完整参数等值断言防止这些行为重新进入默认请求（`tests/test_llm_adapters.py:220-523`）。

## 本轮边界

- `AgentSession` 只依赖 `ModelClient` Protocol；Session 继续持有模型引用，adapter 不取得消息、Turn 或 Step 的所有权。
- core 传递 vendor 中性的工具定义；每个 adapter 独占 SDK 客户端、认证头行为、工具与消息的 wire 编码，以及基础响应映射。
- 组装点通过静态注册表选择 adapter。新增 vendor 偏差时新增具体 adapter，不在已有 adapter 中加入域名条件。
- `api_key`、`adapter`、`api_base`、`model` 与 `max_output_tokens` 显式配置；未知名称和旧 `provider` 字段立即失败，`api_base` 原样交给 adapter。
- Anthropic-compatible 与 OpenAI-compatible 表示本地 wire adapter，不表示已采用或验证对应 vendor 服务；未经探测的推理状态续传、缓存用量和服务端上下文扩展不进入默认请求。

不在本轮范围：流式输出、错误分类、动态插件发现、真实端点能力探测、设计新的上下文策略、重写 agent loop，或切换本地 `config.yaml` 中的服务。本轮只从旧压缩路径移除隐藏模型预算和未探测 `usage` 控制，默认降级为不自动压缩。

## 实际实现

- `ModelClient` 只定义 core 需要的 `generate(messages, tools)`；`ToolDefinition` 只含名称、说明和 JSON Schema 参数。现有 `Message` 与 `LLMResponse` 是共享内部结构，不宣称已经完全 vendor 中性（`mini_agent/llm/protocol.py:9-25`；`mini_agent/schema/schema.py:13-47`）。
- core 为模型与事件接收器分别创建独立工具定义，观察隔离不变量不变（`mini_agent/core/agent.py:608-646`）。
- `AdapterName` 与静态注册表只列出已实现的两种基础 wire adapter；工厂不检查域名，也不拼接路径（`mini_agent/llm/factory.py:11-50`）。
- 共享 adapter 基类只保留当前两种实现共用的设置、重试回调与 `generate()`，不再规定 system message 或请求结构（`mini_agent/llm/base.py:10-60`）。
- 配置不再提供 vendor 端点、模型或输出上限默认值；旧 `provider` 字段在 YAML 与直接构造 `LLMConfig` 时都会失败，示例配置也已移除它并由离线测试同步验证（`mini_agent/config.py:24-42,112-163`；`tests/test_llm_adapters.py:41-150`）。
- 两个 adapter 显式关闭 SDK 内建重试，项目重试层是唯一策略所有者；产生这一修正的实测见 [P-006](../PITFALLS.md#p-006--关闭项目重试不等于-sdk-不会重试)。
- adapter 保留可空的原生 `finish_reason`，不在字段缺失时伪造 `stop`；基础 `usage` 只供观察，不在未经探测时控制压缩。没有显式 `local_compaction_token_limit` 时，旧本地压缩估算关闭（`mini_agent/schema/schema.py:40-47`；`mini_agent/core/agent.py:107-111,136-140,296-326,678-681`）。

## 离线验证

```bash
.venv/bin/python -m pytest -q tests/test_llm_adapters.py
```

2026-08-25 实测为 `19 passed in 0.38s`。缺少字段、旧字段迁移、未知名称、端点逐字传递（可检出改写）、SDK 隐式重试、wire 工具包装、历史工具调用、未探测推理状态回传、可空终止原因和基础用量映射都由离线断言覆盖。标准离线集合实测为 `162 passed in 9.87s`，继续覆盖未探测 `usage` 不控制压缩、摘要与 Step 共用同一模型、Session/Turn/Step 生命周期及消息隔离不变量。
