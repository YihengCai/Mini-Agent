# coding agent 测试框架调查

> 调查日期：2026-08-24。结论来自 OpenAI Codex、gemini-cli、OpenHands、DeepSeek Harness、goose、pi、aider、cline、Roo Code、SWE-agent 等项目的源码。仓库会变化，引用前应重新核实路径。

这份文档只保留会影响 Mini-Agent 设计的结论。逐项目原始笔记已经删除；路径、测试名和提交是可复查证据，不是本项目必须照抄的方案。

## 1. 模拟模型的四种做法

### 有序脚本

每次模型调用从队列取下一条响应，不检查请求。

- OpenHands：`openhands.sdk.testing.TestLLM` 使用 `deque.popleft()`，队列耗尽时抛出 `TestLLMExhaustedError`。
- mini-swe-agent：`DeterministicModel`、`DeterministicToolcallModel` 和 `DeterministicResponseAPIToolcallModel` 覆盖三种工具调用协议格式。
- gemini-cli：`packages/core/src/core/fakeContentGenerator.ts` 提供严格顺序，并为不确定的后台任务增加 `nonStrict` 模式。
- pi：`packages/ai/src/providers/faux.ts` 把模拟模型注册成正式模型服务，并生成完整的流式事件序列。

优点是实现小、运行快、失败容易读。缺点是循环外的压缩等额外模型调用会使队列错位。

### 请求路由

根据请求结构或判断条件选择响应。

- DeepSeek Harness：`packages/core/agent-loop/tests/mock-adapter.ts` 的脚本项可以是响应、请求函数、`hang` 或 `{hangAfter: n}`，并记录 `requests`。
- goose：`dummy_api.rs` 用 enum 表达 `Reply`、`ToolCall`、`ContextLimitError`、`ReplyThenServerError` 等行为。

这种方式能容忍调用顺序变化，但判断条件不能复制生产环境的提示词逻辑，否则模拟模型会变成第二套实现。

### 以请求为键的录制文件

使用规范化后的请求作为 key，同时让请求变化直接导致测试失败。

- goose：`crates/goose/src/providers/testprovider.rs` 以 `Sha256(serde_json(messages))` 查找 `TestRecord`，计算哈希前删除不影响模型语义的元数据；CI 禁止录制。
- OpenHands 旧方案：`tests/integration/conftest.py` 用有序下标选择录制文件（cassette），再检查规范化后的提示词是否相等。该集成测试于 2024-10 从 CI 删除；主机名、路径、SHA256 和空白规范化展示了维护成本。

只有请求已经稳定、动态字段有明确规范化规则时，这种方案才值得使用。

### 本地协议服务

启动本地 HTTP/WebSocket 服务，使用真实的模型服务协议。

- Codex：`codex-rs/core/tests/common/responses.rs` 使用 `wiremock` 和带类型的 SSE 事件构造器；`streaming_sse.rs` 可逐块放行，验证工具是否在 `response.completed` 前启动。
- OpenHands：`tests/e2e/mock-llm/scripts/mock-llm-server.py` 提供 OpenAI 兼容端点，以及 `/admin/trajectory/*` 控制 API。
- DeepSeek Harness：`packages/test-support/llm-mock-server` 可制造 socket 重置、部分流、停顿、格式错误的载荷和带随机种子的故障。

它最适合验证协议解析、重试和流式生命周期，不应取代成本较低的进程内循环测试。

## 2. 对 Mini-Agent FakeLLM 的结论

Mini-Agent 的 `_summarize_messages()` 会额外调用 `llm.generate(tools=None)`，因此单个 FIFO 不可靠。第一版采用按请求路由的有序队列：

- `tools is None` 进入压缩队列；
- 其他请求进入主循环队列；
- 每次调用前检查全局消息历史不变量；
- 队列提前耗尽和测试结束后仍有未消费响应都会失败；
- 记录请求供测试断言，不在第一版实现提示词哈希或本地协议服务。

对应决定见 [ADR-0005](../decisions/0005-fake-llm-routed-queues.md)。

## 3. 中断与消息历史不变量

真实项目通常把中断看成消息历史一致性问题，而不只是捕获异常。

- gemini-cli：`packages/core/src/core/geminiChat.test.ts` 覆盖中止后回滚未响应的用户轮次、包含函数响应的多轮请求、恢复 `lastPromptTokenCount` 和同步记录服务。
- charmbracelet/crush：`internal/agent/dispatch_cancel_test.go` 与 `internal/server/agent_cancel_test.go` 检查取消/接受竞争、空闲时取消不污染下一次提示词、立即取消仍发布 `RunComplete`。
- Roo Code：`flushPendingToolResultsToHistory.spec.ts` 覆盖已中止任务不应写入待处理结果。
- mini-swe-agent：`tests/agents/test_interactive.py` 只检查插入一次中断消息，属于较弱的消息计数测试。

Mini-Agent 需要的不变量是：每个 assistant `tool_use` ID 都有且只有一个对应的 `tool_result`，并且已经执行的工具记录不能因中断被删除。

## 4. 上下文压缩测试

上下文压缩测试的重点不是摘要文案，而是结构和最坏路径。

- cline：`sdk/packages/core/src/extensions/context/compaction.test.ts` 覆盖切分点不落在工具调用与结果之间、token 估算、取消和降级方案。
- Codex：`core/tests/suite/compact.rs` 与 `compact_remote.rs` 检查请求体、摘要替换和远程压缩；部分已知错误行为用带原因的 `#[ignore]` 保留。
- pi：模拟模型服务能按 `sessionId` 估算公共前缀，允许离线检查提示词缓存统计。

本项目的最低要求：随机工具调用组下消息历史始终有效；重复 `build_view()` 的输出逐字节一致；摘要失败不得让输入变大。

## 5. 快照与故障注入

- Codex 使用 `insta` 快照；请求快照会排序 JSON key，并移除 UUID、时间戳、临时路径等动态值。
- DeepSeek Harness 的协议故障服务使用记录下来的 `u32 seed` 重放随机故障。
- goose 的 VCR 录制在 CI 中直接 panic，避免测试自动修改测试样例。

快照适合稳定的协议对象或 UI 输出，不适合把频繁变化的整段提示词当成唯一判据。故障注入必须记录随机种子或使用确定脚本。

## 6. 沙箱与编辑测试

- Codex 的 `linux-sandbox/tests` 和相关平台测试验证真实的操作系统拒绝，而不只检查策略解析器返回值。
- aider 常用方法 monkeypatch 测试编辑循环，很少模拟协议；这证明编辑算法测试不需要模拟完整的模型服务协议。
- coding agent 的编辑测试应覆盖有歧义匹配、空匹配、读取后文件变化、多文件预检和写入失败；成功返回值必须包含可验证信息。

因此 Mini-Agent 把权限命令集测试与真实沙箱探测分开，把编辑引擎测试与模型测试分开。

## 7. 任务级回归

较成熟的项目会同时保留三层：

1. 进程内确定性测试：验证循环不变量；
2. 本地协议测试：验证模型服务协议与失败生命周期；
3. 可选的真实测试与评测：验证真实模型行为。

任务测试集不能只报告通过率。每次运行至少区分模型失败、API 失败、超时、预算耗尽和测试框架崩溃，并保留原始 JSONL。Mini-Agent 要在两个机制落地后才建立小型回归测试集，避免先写一套无法证明任何改进的基准测试。

## 本项目采用与暂缓

采用：

- 按请求路由的 FakeLLM；
- 请求记录与全局消息历史不变量；
- 中断、上下文压缩、流式输出的确定性失败测试；
- 沙箱的真实操作系统探测；
- 任务结果的结构化失败分类。

暂缓：

- 提示词哈希录制文件：提示词还不稳定；
- 本地模型协议服务：第一阶段不需要验证协议；
- 大型评测集：当前没有已实现机制可比较；
- 自己录制标准答案的基准测试：无法独立证明检索或 agent 质量。
