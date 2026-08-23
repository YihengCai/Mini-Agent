# 实现规格

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

| 主题 | 中文 | 英文原件 |
|---|---|---|
| 假 LLM + 运行记录器 + 微型 eval | [00-measurement-rig_CN](00-measurement-rig_CN.md) | [EN](00-measurement-rig.md) |
| 分层上下文管理 + prompt cache 断点 | [01-context-manager_CN](01-context-manager_CN.md) | [EN](01-context-manager.md) |
| 事件缝 / 流式 / 正确中断 / steering | [02-event-seam-interrupt_CN](02-event-seam-interrupt_CN.md) | [EN](02-event-seam-interrupt.md) |
| 执行沙箱 + argv 结构化权限引擎 | [03-sandbox-permissions_CN](03-sandbox-permissions_CN.md) | [EN](03-sandbox-permissions.md) |
| Glob/Grep + repo map | [05-code-retrieval_CN](05-code-retrieval_CN.md) | [EN](05-code-retrieval.md) |
| 每轮检查点与 rewind（shadow git） | [06-checkpoint-rewind_CN](06-checkpoint-rewind_CN.md) | [EN](06-checkpoint-rewind.md) |
| 子 agent 与上下文隔离 | [07-subagent-isolation_CN](07-subagent-isolation_CN.md) | [EN](07-subagent-isolation.md) |

> 缺 `04`：**事务性编辑 + 诊断回灌**。它是评审阶段补进清单的（原始七项里没有），目前只有 [BUILD_LIST_CN.md §2.4](../BUILD_LIST_CN.md) 里的机制要点，没有完整规格。
>
> 译文说明：代码块、文件路径、`file:line`、标识符、API 字段名、以及要落地成产品字符串的文本（子 agent 的 system prompt、`[truncated: ...]` 页脚等）一律保留英文；tool_use / prompt cache / seatbelt / PageRank 等术语保留原词。
