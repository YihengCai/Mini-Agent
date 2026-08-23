# 实现规格

这里记录下一步实现，不枚举所有可能性。规则：

1. 只写机制、不变量、边界和失败测试；代码是实现的事实来源。
2. 当前阶段写实现级规格；后期阶段只保留设计说明。
3. 中文是唯一文档语言；有成熟译法的词使用中文，没有稳定译法的专有概念才保留英文。

| 顺序 | 模块 | 文档 | 详细程度 |
|---|---|---|---|
| 0 | FakeLLM + 测试框架 | [00-test-harness.md](00-test-harness.md) | 当前实现规格 |
| 1 | 事件层 + 中断 + steering | [02-event-layer.md](02-event-layer.md) | 当前实现规格 |
| 2 | 上下文管理 | [01-context-manager.md](01-context-manager.md) | 当前实现规格；提示词缓存受能力探测结果控制 |
| 3 | 事务式编辑 + 诊断 | [04-transactional-edit.md](04-transactional-edit.md) | 当前实现规格 |
| 4 | 沙箱 + 结构化权限 | [03-sandbox-permissions.md](03-sandbox-permissions.md) | 当前实现规格；每个平台分别验证 |
| 5 | Glob/Grep + 截断元数据 | [05-code-retrieval.md](05-code-retrieval.md) | 当前实现规格 |
| 6 | 检查点 / 回退 | [06-checkpoint-rewind.md](06-checkpoint-rewind.md) | 后期设计说明 |
| 7 | subagent 上下文隔离 | [07-subagent-isolation.md](07-subagent-isolation.md) | 后期设计说明 |

不再维护 PageRank repo map 的实现规格，原因见 [ADR-0004](../decisions/0004-cut-repomap-pagerank.md)。
