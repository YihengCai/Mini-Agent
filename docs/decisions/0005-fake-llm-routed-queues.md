# ADR-0005：假 LLM 用「按请求路由的多条有序队列」，不用 FIFO，也不用 prompt 哈希

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：[docs/specs/00-measurement-rig_CN.md](../specs/00-measurement-rig_CN.md) · [docs/reference/agent-testing-survey.md](../reference/agent-testing-survey.md) · 机制表「事件缝与渲染解耦」「分层上下文管理」两行的回归测试列

## 背景

测试装置要先回答一个问题：**一条假响应怎么和一次请求对上号**。这个仓库的循环让这个问题不平凡——`agent.py:326` 在每步开头调 `_summarize_messages()`，它内部 `agent.py:275-283` 又调了一次 `self.llm.generate(...)`，而且传的是 `tools=None` 和一个 2 条消息的列表。也就是说**一个 step 可能发出 `1 + N` 次 `generate()`**，N 取决于有多少轮用户历史需要摘要，且只在超过 token 阈值时才发生。

调查了 13 个真实项目（见 [调查综述](../reference/agent-testing-survey.md)）后，这个坑不是我们独有的：gemini-cli 的 `fakeContentGenerator.ts` 最初就是严格顺序的，后来专门加了 `nonStrict` 模式，注释写的是「Useful for non-deterministic background tasks」——我们的压缩器正是那种 background task。

## 选项

1. **A —— FIFO 有序队列**：`deque.popleft()`，忽略请求内容。OpenHands `TestLLM`、SWE-agent `PredeterminedTestModel`、mini-swe-agent 的三个 stub 都是这种。
2. **B —— prompt 哈希录带**：`SHA256(messages)` 做 key 查表。goose `testprovider.rs` 这么做，并且刻意先归一化再哈希。
3. **C —— 本地 HTTP mock server**：起一个说 provider wire protocol 的服务。codex（wiremock + 1790 行 SSE 事件构造器）、goose、OpenHands 现役 e2e 都走这条。
4. **D —— 录制回放（VCR）**：真跑一次录下来。crush（go-vcr，13 份 cassette）、opencode（自研 recorder，7 场景 × 11 协议）、deepseek-harness（把 session.jsonl 投影成剧本）。
5. **E —— 按请求路由的多条有序队列**：队列按请求形状分流，每条队列内部仍然有序。deepseek-harness 的 `mock-adapter.ts` 的最小可用形态。

## 决定

选 **E**：`FakeLLM` 内部维护两条独立队列，用一个廉价谓词分流——`tools is None` 走 `compact` 队列，否则走 `agent` 队列；队列取空抛 `FakeLLMExhausted` 而不是返回默认值；每次调用把 `(deepcopy(messages), tools)` 记进 `self.requests`。

明确不做的部分：不做 HTTP 层（C）、不做录制回放（D）。它们要等到有了真正的 wire 层问题（重试、SSE 解析、超时、断流）才划算，而那时候只需要在 `FakeLLM` 旁边加一个，不必推翻它。

## 为什么否决其他的

**A（FIFO）** —— 对这个仓库是直接错的：压缩器那次带外调用会把队列错开一格，之后每个断言都偏一位，而症状看起来像循环 bug。**什么条件下它反而对**：循环里只有一条 LLM 调用路径时，A 是最省的选择（50 行、零依赖、失败信息可读），OpenHands 至今仍在用它。等哪天压缩器不再自己调模型（比如改成纯函数 + 外部注入摘要），A 就够了。

**B（prompt 哈希）** —— 我们的摘要 prompt 把工具输出拼了进去（`agent.py:249-283`），于是工具输出一变哈希就变，第一周会全花在写归一化函数上。OpenHands 的历史正是这个结局：`tests/integration/conftest.py` 用「有序下标 + 归一化后字符串相等」双重校验，`filter_out_symbols()` 要剥掉主机名、poetry 路径、SHA256、空白、非字母数字，最后在 2024-10 整套从 CI 删除。**什么条件下它反而对**：goose 的做法证明了它可行——前提是你哈希的是一个**窄而归一化**的对象（它剥掉 `tool_meta`/`_meta`/`is_error: false`，理由是「不属于 LLM 看到的语义输入」），并且愿意接受「录制在 CI 里 panic」这种硬约束。等我们的 prompt 构造稳定下来、且想让「prompt 漂移」自动变成测试失败时，B 是升级方向。

**C（HTTP mock）** —— 买到的是真实 HTTP 客户端、重试、SSE 解析、超时的覆盖，代价是端口、生命周期、flake，以及维护半个 provider 实现。**什么条件下它反而对**：做流式和重试的时候（[spec 02](../specs/02-event-seam-interrupt_CN.md)），尤其想测断流/半截 SSE/socket reset —— deepseek-harness 专门为此写了一个「wire-fault server」。到那一步再加，且只加那一层。

**D（VCR）** —— 重录要钥匙要钱，cassette 会腐烂，prompt 一改整个语料作废；所有用它的项目都在 CI 里硬禁止录制。**什么条件下它反而对**：需要跨多个 provider 协议做一致性矩阵时（opencode 的 7 场景 × 11 协议），或者想验证真实 payload 的形状。我们只有一个端点，拿不到这个收益。

## 怎么验证它是对的

1. `tests/fakes.py` 的 `FakeLLM` 加一条自测：脚本里只放 1 条 agent 响应，强制触发一次压缩，断言**主队列不被消费掉**（这正是 A 会失败的那条）。
2. `assert_consumed()`：测试结束时两条队列都必须空，多脚本和少脚本都要红。
3. `FakeLLMExhausted` 必须是异常而不是默认响应——写一条断言"少写一条脚本会让测试失败"的元测试。
4. 数字口径：`pytest tests/test_loop_scripted.py -q` 期望 **0 次网络调用**（用 `--disable-socket` 或断言 `FakeLLM` 不 import provider SDK），耗时 **待测**（目标个位数秒）。

## 回头看

> 待实现后补。
