# ADR-0005：以显式协议 adapter 隔离模型 API 差异

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/llm/protocol.py`、`mini_agent/llm/factory.py`、`mini_agent/llm/anthropic_client.py`、`mini_agent/llm/openai_client.py`、`tests/test_llm_adapters.py`、提交 `e92b28c`、`204c022`、`23e0521`、`fe04ae8`、`39f7cab`、`ad2f12f`、[P-006](../PITFALLS.md#p-006--关闭项目重试不等于-sdk-不会重试)

> 后续修订：[ADR-0006](0006-remove-legacy-local-compaction.md) 从“默认关闭”推进为完全删除 `local_compaction_token_limit` 与旧本地压缩；`usage` 仅供观察和 adapter 隔离决定继续有效。以下正文保留当时决定，不作追写。

## 背景

实现前的 `LLMClient` 同时负责协议选择、MiniMax 域名识别、URL 改写和客户端构造。2026-08-25 离线实测把用户输入的 `https://api.minimax.io.evil/v1proxy` 改成了 `https://api.minimax.io.evilproxy/anthropic`；`provider="typo"` 还会被 CLI 的二分分支当成 OpenAI-compatible。可重复运行的历史代码提取命令与输出见 [`docs/specs/02-model-adapters.md`](../specs/02-model-adapters.md#问题证据)；当前回归用同一历史 URL 验证两个注册表项都逐字传递端点（`tests/test_llm_adapters.py:153-206`）。

core 实际只需要 `generate(messages, tools) -> LLMResponse`，却由工具对象生成不同协议的工具结构；具体客户端还默认加入额外 Bearer 认证、推理状态续传和未经验证的用量语义。能力记录至今没有真实端点探测（`mini_agent/core/agent.py:608-646`；`docs/PROVIDER_CAPABILITIES.md:20`）。

## 选项

1. **只删除 MiniMax 分支**：保留一个转发客户端和既有工具格式，只移除域名判断与默认值；改动最小，但 core 与 vendor wire 格式仍相互知道。
2. **单个兼容客户端加配置开关**：用一个类容纳两种基础协议和后续 vendor 差异；入口集中，但每增加一家都会累积条件分支。
3. **中性调用 contract + 静态注册表 + 具体 adapter**：core 只使用统一调用方法和中性工具定义，组装点显式选择一个拥有完整 wire 编解码的 adapter。
4. **动态 adapter 插件框架**：支持运行时发现第三方 adapter；扩展性最高，也同时引入版本、加载失败和信任边界。

## 决定

采用选项 3。`ModelClient` Protocol 定义 core 唯一需要的模型行为，`ToolDefinition` 是不含协议包装的工具结构；现有 `Message` 与 `LLMResponse` 仍是共享内部 schema，不在本 ADR 中宣称完全 vendor 中性（`mini_agent/llm/protocol.py:9-25`；`mini_agent/schema/schema.py:13-47`）。`create_model_client()` 通过静态注册表选择 Anthropic-compatible 或 OpenAI-compatible adapter（`mini_agent/llm/factory.py:11-50`）。adapter 名称只表示 wire 格式，不表示选择、推荐或验证同名 vendor 服务。

配置必须显式给出 `api_key`、`adapter`、`api_base`、`model` 和 `max_output_tokens`，未知 adapter 与旧 `provider` 字段立即失败，端点逐字交给具体 adapter（`mini_agent/config.py:24-42,112-163`）。每个具体 adapter 独占 SDK 客户端、认证头行为、消息与工具编码及基础响应映射；共享基类不规定任何 wire 方法（`mini_agent/llm/base.py:10-60`）。未经端点探测的推理状态续传、缓存计量和服务端扩展默认不进入请求。SDK 自带重试固定为零，由项目重试层单独持有策略。

`finish_reason` 保留为可空的 adapter 原生元数据，core 不按它分支；基础 `usage` 映射只用于观察，不控制上下文策略。自动压缩没有显式 `local_compaction_token_limit` 时关闭，显式启用的估算也不宣称等于模型 tokenizer 或上下文上限（`mini_agent/schema/schema.py:40-47`；`mini_agent/core/agent.py:107-111,136-140,239-326,678-681`）。流式输出、统一错误分类、动态发现、认证方式扩展和真实端点能力不在本次范围。

## 为什么否决其他的

- **只删除 MiniMax 分支**没有解决 contract 泄漏：工具仍需知道两种 wire 包装，未探测扩展和客户端默认行为也会继续进入通用路径。如果项目永久只连接一个固定端点、无需替换协议且公开边界不再演进，它反而是成本最低的修复。
- **单个兼容客户端加配置开关**会让“兼容”掩盖真实差异，重现按域名或 vendor 名称猜行为的问题。如果所有目标端点经过测试证明 wire contract 完全相同，差异也只需数据配置而无需代码分支，单客户端反而更简单。
- **动态插件框架**当前没有第三方运行时 adapter、版本协商或独立权限需求，无法用现有失败证据证明额外机制。如果以后允许仓库外 adapter 自主安装，并能一起验证加载生命周期、版本和信任边界，动态发现才是合适选择。

## 怎么验证它是对的

```bash
.venv/bin/python -m pytest -q tests/test_llm_adapters.py
```

2026-08-25 实测为 `19 passed in 0.38s`。测试覆盖显式配置、旧字段迁移、未知名称、两个注册表项的端点逐字传递、SDK 初始化参数、输出上限、工具与历史消息编码、可空终止原因、基础响应与 `TokenUsage` 三字段映射，并断言默认请求不重建未探测的推理状态（`tests/test_llm_adapters.py:41-523`）。标准离线集合实测为 `162 passed in 9.87s`。

## 回头看

实现没有改变 Session 对模型引用和消息状态的所有权，core 的 agent loop 与生命周期测试保持绿色。审查发现示例配置仍使用旧字段，促使同一轮增加模板解析测试并清理安装脚本；这证明配置样例也是公开 contract 的一部分。

实现时还发现项目层关闭重试后，两个 SDK 仍各自默认重试两次。最终显式设置 `max_retries=0`，避免重试层相乘；可复现过程见 [P-006](../PITFALLS.md#p-006--关闭项目重试不等于-sdk-不会重试)。终审又发现共享基类泄漏 Anthropic 请求形状、固定模型预算和未探测 `usage` 仍影响控制流；最终删除共享 wire 抽象、默认关闭本地压缩，并用离线回归锁住“报告用量不触发压缩”。

没有运行真实端点，因此当前只宣称两种基础 wire adapter 通过离线 contract 测试，不宣称任何 vendor 服务兼容性。统一认证输入目前仍是 `api_key`；无认证、OAuth 或签名认证需要出现实际目标 API 后再扩展 factory contract。

ADR-0006 删除压缩后，adapter contract、静态注册表、端点逐字传递和单一重试所有权均未变化；只移除了 core 对本地估算的可选控制路径。基础 `usage` 映射仍由 adapter 离线测试覆盖，Session 只把它保存为观察数据。

2026-08-26：[ADR-0027](0027-no-project-retry-before-error-classification.md) 删除项目级 retry，推翻了本决策中“由项目重试层持有策略”的局部选择；SDK 仍显式 `max_retries=0`，adapter contract、静态注册表、逐字端点与 wire 编解码边界不变。

同日，[ADR-0028](0028-config-file-matches-runtime-model.md) 把模型字段移入 YAML 的 `llm` 分组，并删除 `provider` 的专用迁移文案；旧字段仍由严格模型拒绝。显式 adapter、必填端点与逐字传递 contract 不变。

同日，[ADR-0029](0029-remove-unprobed-thinking-field.md) 删除两个 adapter 永远返回 `None`、又无法往返的共享 `thinking` 字段。未经探测的推理状态仍不进入默认请求，本决策的基础 wire 边界因此收窄但未被推翻。
