# Checkpoint / rewind 设计说明

> 状态：后期模块，未实现。进入实现前必须在上下文管理与事务性编辑落地后的代码上重写实现规格。

## 学习目标

理解 coding agent 的文件副作用与对话状态为什么必须共同回滚，以及为什么直接操作用户仓库的 `git stash`、index 或 HEAD 是错误边界。

## 必须保持的不变量

- 不修改用户仓库的 HEAD、index、refs、reflog、hooks 和全局 Git 配置；
- capture/restore 前后逐字节保真，不受 workspace `.gitattributes`、filters 或 `autocrlf` 影响；
- checkpoint 之后创建、删除和修改的文件都能恢复；
- ignored 文件策略明确，不能一边宣称“整个 workspace”一边漏掉默认 workspace；
- nested repo/submodule 不被吞成普通目录，未处理目标列入 `left_alone`；
- rewind 同时恢复 `raw_log + ContextState`，不是旧实现里的派生 `agent.messages`；
- rewind 本身可撤销。

## 候选设计

使用独立 shadow Git store：私有 `GIT_DIR`、私有 `GIT_INDEX_FILE`、禁用 hooks 与用户配置，并在私有 `info/attributes` 中关闭文本转换和 filters。对象存储负责内容寻址去重，checkpoint metadata 记录文件快照与对应的上下文状态。

这只是候选方案，不是实现承诺。进入本阶段前必须重新验证 Git plumbing、ignored 文件、CRLF、LFS 标记、nested repo 和大文件行为。

## 进入条件

1. `raw_log/ContextState` 接口已经稳定；
2. 事务性编辑定义了哪些工具是 mutating；
3. 事件缝能在 mutation 前后发 checkpoint 事件；
4. 有独立临时仓库 fixture，可在一分钟内跑完整 restore 矩阵。

## 最小验收

`tests/test_checkpoint.py` 必须证明：

- 用户 `.git/index` 的 sha、HEAD 和 `status --porcelain` 在 capture/restore 前后不变；
- CRLF、无尾换行、可执行位、symlink、新建与删除文件逐字节往返；
- ignored 文件和 nested repo 的处理与公开承诺一致；
- 恢复文件后，下一次 provider view 不包含被撤销步骤的 tool result；
- 任一不支持情况显式失败，不打印虚假的成功。

在这些测试全部存在前，README 不得把 checkpoint/rewind 列为已支持。
