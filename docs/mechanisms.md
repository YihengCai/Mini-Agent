# 机制状态

这页是机制状态的唯一来源。函数级设计在[实现规格](specs/)，取舍在[决策记录](decisions/)，复现步骤在 [PITFALLS](PITFALLS.md)。

状态：`待实现` / `待能力探测` / `实现中` / `已验证` / `后期设计` / `已取消`。只有实现、离线回归测试和 ADR 的“回头看”同时存在，才能标为 `已验证`。

| 机制 | 直觉实现的问题 | 本项目边界 | 验证方式 | 状态 |
|---|---|---|---|---|
| [按请求路由的 FakeLLM](specs/00-test-harness.md) | 单个 FIFO 会被循环外的压缩请求错误消费 | `tools is None` 与主循环使用两条有序队列；响应耗尽和存在未消费响应都失败 | `tests/test_loop_scripted.py` | 待实现 |
| [全局消息历史不变量](specs/00-test-harness.md) | 只在单个测试检查配对，其他路径仍可生成未配对记录 | 每次 FakeLLM 请求前检查工具调用与结果的双向配对 | `assert_history_valid()` 的反例测试 | 待实现 |
| [事件层](specs/02-event-layer.md) | 把 `print` 改成零散回调仍会复制控制流 | Agent 只发送带类型的事件；CLI、ACP、JSONL 是接收方 | `test_silent_mode`、固定输出对照测试 | 待实现 |
| [中断恢复](specs/02-event-layer.md) | 截断消息历史会删除已经产生副作用的记录 | 为缺失的 ID 合成工具结果，不删除已完成结果 | `test_interrupt_leaves_no_orphan` + 进程检查 | 待实现 |
| [流式输出 / steering](specs/02-event-layer.md) | 首个 token 输出后重试会拼接两个响应；轮次中途注入会切断工具调用组 | 模型客户端组装流；steering 只在步骤边界进入 | 增量输出、重试与边界测试 | 待实现 |
| [上下文管理](specs/01-context-manager.md) | 原地生成文字摘要会丢失结构、重复压缩，失败时还能变大 | 仅追加的原始日志；工具结果淘汰 + 单一摘要窗口 | `tests/test_context_invariants.py` | 待实现 |
| [提示词缓存](specs/01-context-manager.md) | 未探测端点就设计缓存断点会得到不起作用的代码 | C1–C3 通过后单独实现；否则不声明支持 | `provider_probe` + 真实 `usage` 记录 | 待能力探测 |
| [事务式编辑](specs/04-transactional-edit.md) | `str.replace()` 会静默修改多处；跨文件原子性也容易被夸大 | 预检、文件变化检查、单文件原子替换、限定在代码差异范围内的诊断 | `tests/test_edit_engine.py` | 待实现 |
| [Glob/Grep + 截断元数据](specs/05-code-retrieval.md) | 静默截断会让模型把部分结果当成完整结果 | 遵守忽略规则的搜索、真实总数、bash 首尾截断 | `tests/test_search_tools.py` | 待实现 |
| [操作系统沙箱](specs/03-sandbox-permissions.md) | 检测到 `sandbox-exec` 不等于配置真正生效 | macOS 先通过真实拒绝测试；其他平台分别验证 | `scripts/sandbox_probe.py` | 待实现 |
| [结构化权限](specs/03-sandbox-permissions.md) | `shlex.split()` 不是 shell 解析器，前缀允许列表会漏掉组合命令 | 采用最严格判定；解析失败时默认拒绝；只有沙箱生效时 ASK 才能降级为允许 | `tests/test_permission_corpus.py` | 待实现 |
| AGENTS / SKILL 信任边界 | 仓库文本进入系统角色会被错误升级为授权 | 文件、工具输出和 MCP 响应都是带来源的数据 | 恶意 AGENTS 测试样例仍触发 ASK | 待实现 |
| 规划模式 | 在提示词中写“不要修改”并不会移除修改类工具 | PLAN 下模型看不到修改类工具；用户决定 `exit_plan_mode`，两种结果都返回工具结果 | 接受/拒绝后的消息历史与工具列表测试 | 待实现 |
| [检查点 / 回退](specs/06-checkpoint-rewind.md) | `git stash` 或真实 index 会污染用户仓库，恢复还可能改变字节 | 私有影子存储，同时恢复原始日志与 `ContextState` | `tests/test_checkpoint.py` 逐字节测试矩阵 | 后期设计 |
| [subagent 上下文隔离](specs/07-subagent-isolation.md) | 返回自由文本只是嵌套对话，父级上下文仍不可控 | 只读工具、有限的结构化报告、预算、证据验证 | `tests/test_subagent_isolation.py` | 后期设计 |
| Repo map / PageRank | 自己编写的标准答案无法证明排序质量 | 保留调研；把实现预算投入 Glob/Grep | [ADR-0004](decisions/0004-cut-repomap-pagerank.md) | 已取消 |

## 维护规则

- 开始实现时改成 `实现中`；不要预填通过数量、性能或成本。
- 标成 `已验证` 时，验证方式必须链接真实测试名，状态必须链接对应 ADR。
- 实现改变模块边界或数据流时新开 ADR。
- 端点、模型或操作系统结论只写入 [PROVIDER_CAPABILITIES](PROVIDER_CAPABILITIES.md) 或平台探测结果。
