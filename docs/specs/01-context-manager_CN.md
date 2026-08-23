# 分层上下文管理

> 状态：待事件缝与测试底座落地后实现。prompt cache 由 endpoint 能力探测单独门控。

## 要解决的问题

当前 `_summarize_messages()`（`mini_agent/agent.py:153-232`）就地覆写 `self.messages`：它删除 tool-use 结构，把摘要再次作为 `role="user"` 参与下一轮摘要；`_create_summary()`（`mini_agent/agent.py:257-292`）把未截断工具结果送进摘要器，失败时又把原文返回，因此“压缩”可以让上下文变大，复现见 [P-002](../PITFALLS.md)。

## 核心模型

`self.messages` 变成只追加的 `raw_log`；provider 请求由派生视图产生：

```text
raw_log + ContextState
  -> tool-result eviction
  -> one moving summary
  -> request view
```

`build_view(raw_log, state) -> list[Message]` 必须是确定性纯函数。日志是真相，压缩状态只决定“本轮看到什么”，不改写真相。

## 两层压缩

### 1. Tool-result eviction

按 `tool_call_id` 记录已驱逐结果，只改 tool 消息的 `content`，保留 `role`、`name` 和 id。占位符必须非空并说明：工具、回收原因、需要正文时应重新调用工具。

候选只来自已闭合、保护窗口之外的旧 tool result。不要按 raw 下标持久化驱逐状态；中断或未来 rewind 会移动下标。

### 2. 单一窗口摘要

只保留一份 `SummaryRecord(text, upto_index)`，`upto_index` 单调前进。边界不能切进 assistant tool call 与其全部 tool result 构成的原子组；找不到安全边界就拒绝摘要，让 eviction 继续承担压力或显式报上下文不足。

摘要输入必须逐条钳制并有总上限；网络失败走确定性的结构化降级摘要，绝不能返回原始全文。摘要输出必须严格小于被替换输入。

## 触发信号

以最近一次真实 `usage.prompt_tokens` 为精确锚点，只用本地 tokenizer 估算锚点之后新增消息。工具 schema 不在 `self.messages` 中，因此不能把本地全量 token 估算当真实水位。

使用高/低水位形成滞回，避免每步压缩；具体阈值必须由当前 endpoint 的上下文窗口与实测行为确定，文档不预填示例数字。

## prompt cache 门控

先运行 [能力矩阵](../PROVIDER_CAPABILITIES.md) 的 C1–C3：

- C1/C2 不成立：不实现 `cache_control` 断点，context manager 只优化正确性和窗口预算；
- C1/C2 成立：另开实现决策，验证 tools/system 排序、断点位置和压缩频率；
- C3 决定触发水位如何组合 `input_tokens`、`cache_read_input_tokens` 与 `cache_creation_input_tokens`。

不预先保留“空转的 cache 实现”。没有 endpoint 证据的代码既不可演示，也会把 provider 假设扩散进核心模块。

## 与文件状态的边界

本模块不维护 FileLedger。文件新鲜度、read-before-write 和编辑后的磁盘哈希属于[事务性编辑](04-transactional-edit_CN.md)；上下文模块只负责消息视图。以后若证据表明压缩确实让模型丢失关键文件状态，再通过单独 ADR 引入可重建的工作集摘要。

## 离线工件

`tests/test_context_invariants.py` 使用带固定 seed 的生成器覆盖串行与并行 tool 组：

- 任意安全配置生成的 view 都没有 orphan；
- 同一 `(raw_log, state)` 两次输出逐字节一致；
- eviction 不删除消息骨架；
- summary boundary 不切入 tool 组；
- 摘要失败时输出仍小于输入；
- 找不到安全边界时明确拒绝，而不是产出畸形请求。

`scripts/ctx_bench.py` 只做离线 transcript 重放，报告峰值 request token、累计 request token、摘要调用次数和 orphan 数。cache 指标只有 C1–C3 实测通过后才加入。

## 明确后置

跨会话摘要、语义重要性排序、被驱逐结果再水化、子 agent 隔离、文件账本、provider 侧专有 context management。
