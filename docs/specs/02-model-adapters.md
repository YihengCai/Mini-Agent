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
- `llm` 分组显式配置 `api_key`、`adapter`、`api_base`、`model` 与 `max_output_tokens`；未知名称和旧 `provider` 字段失败，`api_base` 原样交给 adapter。
- Anthropic-compatible 与 OpenAI-compatible 表示本地 wire adapter，不表示已采用或验证对应 vendor 服务；未经探测的推理状态续传、缓存用量和服务端上下文扩展不进入默认请求。

不在本轮范围：流式输出、错误分类、动态插件发现、真实端点能力探测、设计新的上下文策略、重写 agent loop，或切换本地 `config.yaml` 中的服务。本轮只让 `usage` 保持观察数据，并在当时默认关闭旧压缩；该实现后续由 [ADR-0006](../decisions/0006-remove-legacy-local-compaction.md) 完全删除。

## 实际实现

- `ModelClient` 只定义 core 需要的 `generate(messages, tools)`；`ToolDefinition` 只含名称、说明和 JSON Schema 参数。现有 `Message` 与 `LLMResponse` 是共享内部结构，不宣称已经完全 vendor 中性（`mini_agent/llm/protocol.py:9-25`；`mini_agent/schema/schema.py:13-47`）。
- core 为模型与事件接收器分别创建独立工具定义，观察隔离不变量不变（`mini_agent/core/agent.py:608-646`）。
- `AdapterName` 与静态注册表只列出已实现的两种基础 wire adapter；工厂不检查域名，也不拼接路径（`mini_agent/llm/factory.py:11-50`）。
- 共享 adapter 基类只保留当前两种实现共用的设置与 `generate()` contract，不规定 system message、请求结构或调用策略（`mini_agent/llm/base.py`）。
- 配置不再提供 vendor 端点、模型或输出上限默认值；YAML 直接使用 `llm` 运行时结构，旧 `provider` 字段与其他未知字段由严格模型拒绝（`mini_agent/config.py:26-108`；`tests/test_llm_adapters.py:32-185`）。
- 两个 adapter 显式关闭 SDK 内建 retry，并各自只发起一次项目级调用；项目级 retry 后续由 [ADR-0027](../decisions/0027-no-project-retry-before-error-classification.md) 删除，产生 SDK 边界修正的实测见 [P-006](../PITFALLS.md#p-006--关闭项目重试不等于-sdk-不会重试)。
- adapter 保留可空的原生 `finish_reason`，不在字段缺失时伪造 `stop`；基础 `usage` 只供观察，不在未经探测时控制任何上下文策略（`mini_agent/schema/schema.py:40-47`；`mini_agent/core/agent.py:125-127,380-383`）。core 当前没有自动上下文预算或压缩。

## 离线验证

```bash
.venv/bin/python -m pytest -q tests/test_llm_adapters.py
```

2026-08-26 删除项目级 retry 后，adapter 与 core 定向集合实测 `113 passed in 0.61s`，标准离线集合实测 `286 passed, 9 deselected in 13.68s`。缺少字段、旧字段迁移、未知名称、端点逐字传递、SDK retry 关闭、单次项目调用、wire 工具包装、历史工具调用、未探测推理状态回传、可空终止原因和基础用量映射都由离线断言覆盖。
