# 架构决策记录

ADR 记录实现过程中真实发生的选择与取舍，不替未来模块预写设计。

| 文档 | 回答的问题 |
|---|---|
| `docs/UPSTREAM_AUDIT.md` | 上游当前怎样工作、哪里有已确认问题 |
| `docs/BUILD_LIST.md` | 当前工作与待研究问题 |
| `docs/specs/` | 当前模块准备怎样实现 |
| `docs/decisions/` | 为什么选这个方案，为什么不选其他方案 |

## 规则

- 文件名使用 `NNNN-kebab-case.md`；`0000-template.md` 不占决策编号。
- 只有实现已经开始、用户明确接受或代码落地的选择才编号。
- 改变已记录的决定时新开 ADR，并把旧 ADR 状态改为 `已推翻（见 ADR-NNNN）`。
- 状态：`已采纳` / `已推翻（见 ADR-NNNN）`。
- 不改写旧 ADR 的决定；实现完成后补“回头看”，记录实际偏差与测量结果。
- 代码结论必须有 `file:line`、复现命令或测量结果。
- 没有测量的数字写 `待测`；模型服务能力写 `待探测`。
- 每个被否决的选项都要写明在什么条件下它反而更合适。

## 什么时候写 ADR

当前实现中出现以下任一情况时需要 ADR：

1. 两个以上方案都能落地，选择会影响后续实现；
2. 改变模块边界、公开 contract、状态所有权、依赖或安全模型；
3. 已出现的测试或测量促使我们放弃原计划；
4. 推翻已有 ADR。

候选设计、未来路线、普通问题修复、重命名、格式调整、`file:line` 更正和代码本身已经能说明的细节不写 ADR。

## 索引

| ADR | 决定 | 状态 |
|---|---|---|
| [0001](0001-strict-global-llm-call-script.md) | LLM 测试替身使用带用途标签的全局调用序列 | 已推翻（见 ADR-0006） |
| [0002](0002-bounded-and-atomic-file-tools.md) | 文件工具采用有界读取、唯一匹配和单文件原子替换 | 已采纳 |
| [0003](0003-remove-acp-and-extract-core-loop.md) | 删除无真实客户端验证的 ACP，以同步事件连接 core 与 CLI | 已采纳 |
| [0004](0004-session-turn-step-lifecycle.md) | 以 Session、Turn、Step 分开对话、控制权交接与模型—工具执行 | 部分推翻（observer 见 ADR-0032） |
| [0005](0005-explicit-model-api-adapters.md) | 以中性 contract、静态注册表和具体 adapter 隔离模型 API 差异 | 已采纳 |
| [0006](0006-remove-legacy-local-compaction.md) | 删除旧本地压缩，暂以完整历史直传 | 已采纳 |
| [0007](0007-explicit-opt-in-for-external-tests.md) | 外部测试必须通过 marker 与收集门显式允许 | 已采纳 |
| [0008](0008-session-owned-tool-batch-executor.md) | Session 以冻结注册与批次执行器统一模型工具调用 | 部分推翻（账本见 ADR-0031） |
| [0009](0009-runtime-owned-background-shells.md) | 后台 shell 由一次 CLI runtime 显式持有并统一关闭 | 已采纳 |
| [0010](0010-model-facing-tool-output-budget.md) | 工具原始事实保持完整，模型消息投影按 UTF-8 字节约束 | 已采纳 |
| [0011](0011-runtime-owned-mcp-connections.md) | MCP 超时与连接由一次 CLI runtime 显式持有并统一关闭 | 已采纳 |
| [0012](0012-strict-single-source-config-loading.md) | 配置模型同时持有默认值与未知字段边界 | 已推翻（见 ADR-0028） |
| [0013](0013-fail-closed-note-storage.md) | 损坏的 Note 存储失败关闭并保留原字节 | 已推翻（见 ADR-0030） |
| [0014](0014-positive-step-budget-at-config-and-core.md) | 配置与 core 共同拒绝非正 Step 预算 | 已采纳 |
| [0015](0015-bind-config-companions-to-selected-source.md) | 配置伴随文件绑定到已选主配置来源 | 已采纳 |
| [0016](0016-reject-explicit-invalid-mcp-transports.md) | 显式非法 MCP transport 只隔离当前 server | 已采纳 |
| [0017](0017-nonnegative-retry-count-at-config-and-runtime.md) | 配置与运行时共同拒绝负重试次数 | 已推翻（见 ADR-0027） |
| [0018](0018-finite-and-saturating-retry-backoff.md) | 退避数值有限且溢出时按上限饱和 | 已推翻（见 ADR-0027） |
| [0019](0019-exclusive-turn-log-allocation.md) | 每个 Turn 通过排他创建独占日志文件 | 已采纳 |
| [0020](0020-transactional-skill-discovery.md) | Skill 发现以完整快照发布并拒绝重名 | 已采纳 |
| [0021](0021-retry-module-owns-enabled-switch.md) | 重试模块单一持有 enabled 开关 | 已推翻（见 ADR-0027） |
| [0022](0022-core-preserves-model-error-semantics.md) | core 保留模型异常自身语义 | 已采纳 |
| [0023](0023-background-shell-completes-after-stdout-eof.md) | 后台 shell 在 stdout EOF 后才完成 | 已采纳 |
| [0024](0024-cli-owns-runtime-workspace.md) | CLI 单一持有运行时工作区 | 已采纳 |
| [0025](0025-executor-owns-admitted-tool-results.md) | 执行器取得工具返回值所有权 | 已采纳 |
| [0026](0026-foreground-shell-reaps-on-interruption.md) | 前台 shell 中断前回收直接子进程 | 已采纳 |
| [0027](0027-no-project-retry-before-error-classification.md) | 在模型错误分类前不做项目级重试 | 已采纳 |
| [0028](0028-config-file-matches-runtime-model.md) | 配置文件直接匹配运行时模型 | 已采纳 |
| [0029](0029-remove-unprobed-thinking-field.md) | 未探测推理状态不进入共享 schema | 已采纳 |
| [0030](0030-remove-incomplete-note-memory.md) | 删除不可读取的 Note 半能力 | 已采纳 |
| [0031](0031-scope-tool-call-ids-to-pending-batches.md) | 调用标识符只约束未完成工具批次 | 已采纳 |
| [0032](0032-observers-do-not-control-turns.md) | observer 普通异常不控制 Turn | 已采纳 |
| [0033](0033-keep-usage-on-model-response-events.md) | usage 只保留在模型响应事件 | 已采纳 |

此前批量生成的实现前提案已从活跃文档删除；需要时可以从 Git 历史查阅，但不占用正式编号。
