# 检查点 / 回退

> 状态：后期设计，未实现。开始实现前，必须基于已经稳定的上下文管理器与编辑引擎重写规格。

## 要研究的问题

为什么 coding agent 的文件副作用与对话状态必须一起回退，以及为什么不能操作用户仓库的 `git stash`、index 或 HEAD。

## 不变量

- 不修改用户仓库的 HEAD、index、refs、reflog、hooks 和全局 Git 配置；
- 捕获与恢复逐字节一致，不受工作区 `.gitattributes`、filters 或 `autocrlf` 影响；
- 检查点之后创建、删除或修改的文件都能恢复；
- 被忽略文件的策略明确，不能宣称覆盖整个工作区却漏掉这类文件；
- 嵌套仓库和 submodule 不被当成普通目录，未处理目标列入 `left_alone`；
- 回退同时恢复 `raw_log + ContextState`，而不是派生的 `agent.messages`；
- 回退操作本身可以撤销。

## 候选设计

使用私有影子 Git 存储：私有 `GIT_DIR`、私有 `GIT_INDEX_FILE`、禁用 hooks 和用户配置，并在私有 `info/attributes` 中关闭文本转换和 filters。对象存储提供按内容寻址的去重；检查点元数据记录文件快照与对应上下文状态。

这只是候选方案，不是实现承诺。进入本阶段前必须重新验证 Git plumbing、被忽略文件、CRLF、LFS attributes、嵌套仓库和大文件行为。

## 进入条件

1. `raw_log/ContextState` 接口已经稳定；
2. 编辑引擎定义了修改类工具；
3. 事件层能在修改前后发送检查点事件；
4. 隔离的临时仓库测试样例能在一分钟内跑完恢复矩阵。

## 验证

`tests/test_checkpoint.py` 必须证明：

- 用户 `.git/index` SHA、HEAD 和 `status --porcelain` 在捕获与恢复前后不变；
- CRLF、缺少文件尾换行、可执行位、符号链接、创建和删除的文件都能逐字节还原；
- 被忽略文件和嵌套仓库行为与公开 contract 一致；
- 恢复后下一次模型请求视图不包含被移除步骤的工具结果；
- 不支持的情况明确失败，不返回虚假的成功结果。

这些测试全部通过前，README 不得把检查点和回退列为已支持能力。
