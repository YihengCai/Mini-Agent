# 开源 coding agent 的 LLM 测试替身

> 调查日期：2026-08-24。这里只记录外部项目源码证据，不替 Mini-Agent 做设计决定。仓库会变化，实施前应重新核对链接。

## 名称并不统一

“LLM 测试替身”是本项目对这一类测试手段的统称，不是外部项目共享的类名：

- Gemini CLI 使用 [`FakeContentGenerator`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/core/fakeContentGenerator.ts)；
- OpenHands SDK 使用 [`TestLLM`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/testing/test_llm.py)；
- Codex 主要使用 [`ResponseMock` 和本地模型服务](https://github.com/openai/codex/blob/main/codex-rs/core/tests/common/responses.rs)。

因此概念文档应描述“测试替身解决什么问题”，具体标识符由本项目实现决定。

## 进程内脚本化响应

Gemini CLI 的测试实现与正式 `ContentGenerator` 使用同一接口。默认模式按调用顺序消费预设响应，调用方法不匹配或响应耗尽时失败；`nonStrict` 模式允许后台任务以不确定顺序消费同类响应。流式响应以 async generator 逐块返回。

OpenHands 的 `TestLLM` 按顺序返回 SDK `Message` 或指定异常，耗尽时抛错，并公开调用次数和剩余响应。它可以注入真实 `Agent`、`Conversation` 和工具执行器；[并行工具测试](https://github.com/OpenHands/software-agent-sdk/blob/main/tests/sdk/agent/test_parallel_execution_integration.py)通过事件轨迹断言工具调用顺序和失败隔离。

这一层适合验证 agent loop 的状态迁移、工具调度和消息结构。它不经过真实模型服务协议，不能证明 HTTP/SSE 兼容性。

## 本地协议服务

Codex 的核心测试运行真实 harness 和模型客户端，只把远端 Responses API 换成本地服务。测试辅助代码构造带类型的 SSE 事件、捕获出站请求，并检查工具调用与结果的配对结构。专门的 [`StreamingSseServer`](https://github.com/openai/codex/blob/main/codex-rs/core/tests/common/streaming_sse.rs)可以逐块放行数据，用来验证流式时序和并发边界。

OpenHands 也把 `TestLLM` 包成 [OpenAI 兼容的本地假服务](https://github.com/OpenHands/OpenHands/blob/main/tests/e2e/mock-llm/scripts/mock-llm-server.py)，再由 [Playwright 配置](https://github.com/OpenHands/OpenHands/blob/main/playwright.mock-llm.config.ts)启动完整前后端。这个服务支持脚本化工具调用、SSE、错误状态和请求记录。

这一层适合验证模型客户端的协议编解码、流式分片、重试和错误分类；它比进程内替身更接近生产路径，但不应取代更小的 agent loop 测试。

## 录制回放与真实端点

Gemini CLI 的生成器组装支持录制真实响应并在后续测试中回放，见 [`contentGenerator.ts`](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/core/contentGenerator.ts)。录制文件适合稳定的完整 CLI 场景，但提示词和动态字段变化会带来维护成本。

真实模型测试用于验证提示词效果、模型行为和 vendor 能力，不适合证明确定性的 agent loop 不变量。代表性项目都把这类测试与默认快速回归分开：Codex 的 [live CLI 测试](https://github.com/openai/codex/blob/main/codex-rs/core/tests/suite/live_cli.rs)默认忽略，OpenHands 的[集成测试说明](https://github.com/OpenHands/software-agent-sdk/blob/main/tests/integration/README.md)将真实模型任务单独管理。

## 可迁移的测试分层

外部源码共同展示了三个不同证据边界：

1. 进程内测试替身证明 agent loop 的确定性行为；
2. 本地协议服务证明模型客户端和失败生命周期；
3. 显式启用的真实端点测试证明 vendor 能力和模型行为。

三层不能互相替代。选择哪一层，应由当前要证明的不变量决定。
