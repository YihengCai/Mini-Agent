# 实现规格

这里记录“下一步怎么造”，不是展示所有可能性。规格服从三条规则：

1. 只写机制、不变量、边界和可失败工件；完整代码以实现为准。
2. 当前阶段写实现级规格；后期阶段只留设计说明，进入实现前再展开。
3. 中文是唯一维护版本。标识符、协议字段和产品字符串保留英文。

| 顺序 | 模块 | 文档 | 深度 |
|---|---|---|---|
| 0 | 假 LLM + 运行记录 | [00-measurement-rig_CN.md](00-measurement-rig_CN.md) | 当前实现规格 |
| 1 | 事件缝 + 中断 + steering | [02-event-seam-interrupt_CN.md](02-event-seam-interrupt_CN.md) | 当前实现规格 |
| 2 | 分层上下文管理 | [01-context-manager_CN.md](01-context-manager_CN.md) | 当前实现规格；cache 受能力探测门控 |
| 3 | 事务性编辑 + 诊断回灌 | [04-transactional-edit_CN.md](04-transactional-edit_CN.md) | 当前实现规格 |
| 4 | 沙箱 + 权限 | [03-sandbox-permissions_CN.md](03-sandbox-permissions_CN.md) | 当前实现规格；平台分别验收 |
| 5 | Glob/Grep + 自描述截断 | [05-code-retrieval_CN.md](05-code-retrieval_CN.md) | 当前实现规格 |
| 6 | checkpoint / rewind | [06-checkpoint-rewind_CN.md](06-checkpoint-rewind_CN.md) | 后期设计说明 |
| 7 | 子 agent 上下文隔离 | [07-subagent-isolation_CN.md](07-subagent-isolation_CN.md) | 后期设计说明 |

不再维护 PageRank repo map 的实现规格，原因见 [ADR-0004](../decisions/0004-cut-repomap-pagerank.md)。
