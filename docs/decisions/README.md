# 架构决策记录

ADR 记录真实选择与取舍，不重复实现规格，也不记录普通问题。

| 文档 | 回答的问题 |
|---|---|
| `docs/UPSTREAM_AUDIT.md` | 上游当前怎样工作、哪里有已确认问题 |
| `docs/BUILD_LIST.md` | 实现顺序与依赖 |
| `docs/mechanisms.md` | 机制状态与验证方式 |
| `docs/specs/` | 模块怎样实现 |
| `docs/decisions/` | 为什么选这个方案，为什么不选其他方案 |

## 规则

- 文件名使用 `NNNN-kebab-case.md`；`0000-template.md` 不占决策编号。
- 编号不复用、不重排、不删除。改变决定时新开 ADR，并把旧 ADR 状态改为 `已被 ADR-NNNN 取代`。
- 状态：`已采纳（未实现）` / `已采纳（已实现）` / `已废弃` / `已被 ADR-NNNN 取代`。
- 不改写旧 ADR 的决定；实现完成后补“回头看”，记录实际偏差与测量结果。
- 代码结论必须有 `file:line`、复现命令或测量结果。
- 没有测量的数字写 `待测`；模型服务能力写 `待探测`。
- 每个被否决的选项都要写明在什么条件下它反而更合适。

## 什么时候写 ADR

以下任一情况需要 ADR：

1. 两个以上方案都合理，需要选择一个；
2. 改变模块边界、状态所有权或数据流；
3. 明确取消一个通常会实现的能力；
4. 决定依赖一次测量或 PITFALL；
5. 实现顺序会影响返工量或正确性。

普通问题修复、重命名、格式调整、`file:line` 更正和已经由规格决定的实现细节不写 ADR。

## 索引

| # | 决定 | 状态 |
|---|---|---|
| [0001](0001-layered-context-manager.md) | 三层上下文管理器 | 已被 ADR-0007 取代 |
| [0002](0002-event-seam-before-context.md) | 事件层先于上下文管理 | 已采纳（未实现） |
| [0003](0003-sandbox-gated-permissions.md) | 结构化权限 + 仅在沙箱生效时降级 | 已采纳（未实现） |
| [0004](0004-cut-repomap-pagerank.md) | 删除 PageRank repo map | 已采纳（未实现） |
| [0005](0005-fake-llm-routed-queues.md) | FakeLLM 使用按请求路由的队列 | 已采纳（未实现） |
| [0006](0006-progressive-specification.md) | 实现规格按阶段展开 | 已采纳（已实现） |
| [0007](0007-split-file-state-from-context.md) | 文件状态归编辑引擎 | 已采纳（未实现） |
