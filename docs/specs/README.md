# 实现规格

这里只记录当前正在准备实现的模块，不枚举未来架构。规则：

1. 只写要解决的问题、不变量、边界和能够暴露当前缺陷的回归测试；代码是实现的事实来源。
2. 实现开始前才创建规格，完成后由代码、测试和 ADR 接管事实来源。
3. 中文是唯一文档语言；有成熟译法的词使用中文，没有稳定译法的专有概念才保留英文。

最近完成的规格包括 [LLM 测试替身、模型请求结构与默认收集边界](00-test-harness.md)、[Session、Turn、Step 生命周期与 CLI 观察边界](01-core-agent-loop.md)、[模型调用 contract 与协议 adapter](02-model-adapters.md) 和 [工具注册与批次执行强制点](03-tool-batch-enforcement.md)；实际接口和行为以其中链接的代码、测试和 ADR 为准。

最近完成的规格还包括 [后台 shell 状态与资源所有权](04-background-shell-ownership.md)、[模型可见工具输出预算](05-tool-output-budget.md)、[MCP 超时与连接的运行时所有权](06-mcp-runtime-ownership.md)、[Note 存储的失败关闭边界](08-note-storage-integrity.md)、[配置伴随文件的来源边界](09-config-companion-provenance.md)、[MCP transport 的显式校验边界](10-mcp-transport-validation.md)、[Turn 日志的排他分配](13-exclusive-turn-logs.md)、[Skill 发现的注册表快照](14-transactional-skill-discovery.md)、[core 的模型失败语义](16-core-model-error-semantics.md)、[后台 shell 输出的完成边界](17-background-shell-output-completion.md)、[运行时工作区的单一所有权](18-runtime-workspace-ownership.md)、[工具返回值的接纳所有权](19-tool-result-ownership.md) 和 [前台 shell 中断时的直接子进程回收](20-foreground-shell-interruption-cleanup.md)。旧配置分片与项目级 retry 分别由 [ADR-0028](../decisions/0028-config-file-matches-runtime-model.md) 和 [ADR-0027](../decisions/0027-no-project-retry-before-error-classification.md) 删除，对应旧规格不再作为现有能力入口。新的当前工作与未来问题只保留在 [`BUILD_LIST.md`](../BUILD_LIST.md)；进入相应实现时，再根据当时的代码、回归测试和端点探测结果写短规格。
