# Mini-Agent 学习路线

## 目标

在真实 agent loop 上逐步研究 coding agent harness。每次只展开当前问题：先让现有缺陷可以离线复现，再比较方案、实现、验证，最后记录实际取舍和踩坑。

这不是完整产品路线图。未来方向只是一组待研究问题，不代表接口、顺序或方案已经确定。

## 当前状态

仓库已在上游 baseline 加代码审计的基础上完成第一项 harness 改造。生产 agent loop 未修改；新增能力位于 `tests/llm_test_double.py` 和 `tests/test_agent_loop_offline.py`。已确认的上游问题见 [`UPSTREAM_AUDIT.md`](UPSTREAM_AUDIT.md)。

## 已完成：建立可靠的 agent loop 测试入口

LLM 测试替身让 agent loop 回归测试不访问网络、不消耗真实 API，并且响应与调用顺序可重复。当前已经验证：

1. 主循环与摘要调用按一条带用途标签的全局序列获得脚本化响应；
2. 意外调用、响应不足和未消费响应都会让测试失败；
3. 每次模型请求都能检查工具调用与工具结果的配对结构；
4. 测试运行真实 agent loop，而不是在测试里手工模拟消息追加。

实现和验证边界见 [`specs/00-test-harness.md`](specs/00-test-harness.md)，全局序列的取舍见 [`decisions/0001-strict-global-llm-call-script.md`](decisions/0001-strict-global-llm-call-script.md)。

## 下一项：让 CLI 与 ACP 共用唯一的 agent loop

下一步先检查怎样让 CLI 与 ACP 共用唯一的 agent loop。现有证据是 `Agent.run()` 直接渲染终端，而 ACP 在 `mini_agent/acp/__init__.py:127-165` 复制了循环；尚未开始修改。

进入实现前只保留三个问题：

- 核心循环应输出什么最小信息，才能同时服务 CLI、ACP 和测试；
- 谁持有会话状态、取消状态和工具执行结果；
- 怎样证明两个适配器没有重新实现控制流。

现在不预先命名事件类型、渲染器或接收器，也不冻结流式输出、steering 和权限接口。

## 待研究问题

以下条目没有正式顺序；只有当前实现暴露出依赖或失败案例时，才创建规格或 ADR。

- 上下文压缩怎样保持工具调用结构，并保证失败时不会扩大输入；
- 编辑工具怎样拒绝歧义匹配，并检测读取后的外部文件变化；
- 搜索和 shell 输出怎样显式报告截断；
- 权限判断与操作系统沙箱怎样划分责任；
- 仓库指令、skills、MCP 和工具输出怎样保持信任边界；
- 规划模式是否需要通过工具可见性强制执行；
- 检查点怎样同时恢复文件和模型可见状态而不修改用户 Git；
- subagent 是否真的减少父级上下文，而不只是启动另一段对话；
- 什么时候已经出现足够的真实回归，值得建立任务级测试集。

模型服务能力只在某个当前设计确实依赖它时探测，结果写入 [`PROVIDER_CAPABILITIES.md`](PROVIDER_CAPABILITIES.md)。

## 一项改造的完成标准

1. 有一个来自当前代码或实现过程的可复现失败；
2. 离线回归测试会在关键实现被删除后转红；
3. 实现边界由代码和真实测试说明；
4. 过程中发生的取舍才写 ADR，亲历的错误假设才写 PITFALL；
5. 最后更新 README 的真实能力与限制。
