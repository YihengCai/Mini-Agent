# ADR-0007：上下文先做两层派生视图，文件状态归入编辑机制

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 取代：ADR-0001 的“三层上下文 + FileLedger”边界；保留其 raw log、tool eviction、单一摘要与安全边界决定
- 关联：`docs/specs/01-context-manager_CN.md` · `docs/specs/04-transactional-edit_CN.md`

## 背景

ADR-0001 把 FileLedger 作为 context tier 3：约 150 LOC，逐轮重新 hash 已观察文件并把表格注入尾部。但原规格的验收主要证明历史配对、压缩确定性和 cache 行为，没有一个离线工件会在删除 FileLedger 后独立失败。与此同时，read-before-write、磁盘陈旧性和写后 hash 已经是事务性编辑必须拥有的状态。

把同一份文件新鲜度同时放进 context manager 和 edit engine，会产生两个权威来源；而在压缩机制尚未落地前，无法证明额外注入的 ledger 真能改善任务结果。

## 选项

1. 保留 FileLedger 为上下文第三层，每轮注入。
2. 文件状态只由事务性编辑持有；context manager 先做 tool eviction + 单一摘要。
3. 完全不跟踪文件状态，只依赖模型主动重读。

## 决定

选 2。上下文模块只管理消息视图；编辑模块以磁盘为 ground truth，记录 `(mtime_ns, size, sha256)` 并在写前拒绝陈旧 patch。未来若出现“编辑安全正确，但压缩后模型仍因看不到工作集摘要而重复劳动”的可复现任务，再单独设计只读工作集摘要。

## 为什么否决其他的

**选项 1** 在已有稳定文件账本、多个消费者都需要工作集视图时是对的，例如 IDE/LSP 已提供 versioned document store。当前项目没有这个基础，context 自己造账本会先扩大模块边界。

**选项 3** 在只读问答 agent 中是对的；coding agent 会写文件，外部 bash 也能改文件，完全不检查陈旧性会让旧 patch 覆盖新内容。

## 怎么验证它是对的

- context invariant tests 不需要文件系统 fixture；
- edit tests 能独立证明外部改写触发 stale；
- 删除 context manager 不会关闭 edit staleness 保护；
- 若未来引入工作集摘要，必须先提供一个当前两层方案会失败的任务。

## 回头看

> 待两层 context 与 edit engine 都实现后补。
