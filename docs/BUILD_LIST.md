# Mini-Agent 学习路线

## 目标

在真实 agent loop 上研究并实现现代 coding agent 的关键机制。每个机制必须留下一个离线、可重复、删除关键实现后会失败的测试。

“先进设计”必须满足：

1. 有具体的失败方式；
2. 边界与不变量可以写成测试；
3. 模型服务或操作系统能力经过探测；
4. 所有数字来自本仓库的可重复测量；
5. 明确写出不做什么，以及原因。

## 文档职责

- [README](../README.md)：项目身份和当前真实能力。
- [机制表](mechanisms.md)：机制状态的唯一来源。
- 本文：实现顺序与依赖，不重复函数级设计。
- [实现规格](specs/)：当前和下一阶段的实现边界；后期模块只保留设计说明。
- [决策记录](decisions/)：ADR，记录真实取舍。
- [PITFALLS](PITFALLS.md)：已经复现的反直觉问题。
- [外部调研](reference/)：只提供证据。

## 当前判断

上游已有可运行的 agent loop、文件与 shell 工具、CLI、MCP、skills、日志和 ACP 适配器，但仍是演示项目：部分测试会错误通过，渲染与循环耦合，取消路径不调用 `Task.cancel()`，上下文压缩会破坏消息历史结构，编辑 contract 与实现不一致，也没有权限控制或沙箱。

因此先建立测试框架，再增加能力。

## 阶段 0：测试框架

### 0.1 FakeLLM 与消息历史不变量

按 ADR-0005 实现按请求路由的有序队列、请求记录和 `assert_history_valid()`。第一批测试必须证明：预设响应不足会失败、未配对的工具调用会失败、压缩请求不会消费主循环响应。

规格：[00-test-harness.md](specs/00-test-harness.md)

### 0.2 模型服务能力探测

实现 `scripts/provider_probe.py`，先测试 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 中会影响设计的能力：缓存、流式输出、工具调用配对、连续用户消息、推理块往返和上下文上限。真实 API 探测默认跳过，不进入离线测试。

### 0.3 已确认问题的最小修复

- 对齐 token 用量的累计语义与字段名；
- 工作区路径统一使用 `.resolve()`；
- `EditTool` 拒绝空匹配和非唯一匹配；
- 取消 bash 任务时终止并等待子进程。

每项单独写回归测试，不顺手重构。

## 阶段 1：事件层与中断

先把终端渲染、ACP 适配器和日志记录器从控制流中拆出，再实现取消、流式输出与 steering（运行中追加指令）。

完成标准：

- `agent.py` 不负责终端渲染；
- CLI 与 ACP 使用同一 agent loop；
- Esc 能取消 LLM 或工具任务；
- 缺失的工具结果通过合成结果修复，已完成结果不丢失；
- shell 子进程不残留；
- steering 只在已闭合的步骤边界注入。

规格：[02-event-layer.md](specs/02-event-layer.md) · ADR：[ADR-0002](decisions/0002-event-seam-before-context.md)

## 阶段 2：上下文管理与提示词缓存

把 `self.messages` 变成仅追加的 `raw_log`，模型请求使用确定性的派生视图。先实现工具结果淘汰与单一摘要窗口；文件状态不属于本阶段。

提示词缓存是条件分支：只有 C1–C3 探测通过才实现 `cache_control` 和成本比较。不支持时只保证上下文正确性与 token 预算，不保留不起作用的 vendor 专用代码。

完成标准：随机消息历史没有未配对记录；重复构建的输出逐字节一致；摘要失败不增大输入；找不到安全边界时明确失败。

规格：[01-context-manager.md](specs/01-context-manager.md) · ADR：[ADR-0007](decisions/0007-split-file-state-from-context.md)

## 阶段 3：编辑与搜索

### 3.1 事务式编辑与诊断

实现唯一匹配、文件变化检查、多段修改预检、单文件原子替换和限定在代码差异范围内的诊断。在检查点实现前，不承诺跨文件崩溃原子性。

规格：[04-transactional-edit.md](specs/04-transactional-edit.md)

### 3.2 Glob/Grep 与输出预算

实现 Glob/Grep、忽略规则、工作区边界限制和显式截断元数据；同时限制 bash 的成功与错误输出。PageRank repo map 暂不实现，除非外部代码定位基准测试证明 Glob/Grep 不够。

规格：[05-code-retrieval.md](specs/05-code-retrieval.md) · ADR：[ADR-0004](decisions/0004-cut-repomap-pagerank.md)

## 阶段 4：沙箱与权限

在事件层上接入权限请求，在工作区工具的统一构造点套上防护。先完整支持 macOS seatbelt；Linux bwrap 只有在 Linux 环境通过相同探测后才声明支持。

完成标准：真实的操作系统拒绝测试、解析失败时默认拒绝、`NoSandbox` 不自动允许、公开 `NOT PREVENTED` 列表。

规格：[03-sandbox-permissions.md](specs/03-sandbox-permissions.md) · ADR：[ADR-0003](decisions/0003-sandbox-gated-permissions.md)

## 阶段 5：信任边界与规划模式

### 5.1 AGENTS.md 信任边界

仓库中的 `AGENTS.md`、SKILL、MCP 响应和工具输出都是数据，不能因为进入系统角色就获得授权。先修复 skills 元数据直接拼入系统提示词形成的注入面，再实现 AGENTS 查找。

### 5.2 规划模式

规划模式从模型可见的工具列表中物理移除修改类工具；唯一出口是由用户接受或拒绝的 `exit_plan_mode`。拒绝时也必须返回对应工具结果，并在轮次边界切换模式。

进入实现前各写一份短规格；现在不预写类或数据结构。

## 阶段 6：检查点与 subagent

### 6.1 检查点 / 回退

在原始日志、`ContextState` 和修改类工具边界稳定后实现影子存储。不能逐字节还原或会触碰用户 `.git` 的版本不发布。

设计说明：[06-checkpoint-rewind.md](specs/06-checkpoint-rewind.md)

### 6.2 subagent 上下文隔离

在事件、上下文、权限和预算统计稳定后实现。要证明的是“subagent 对话记录不进入父级上下文”，不只是“能启动第二个 agent”。

设计说明：[07-subagent-isolation.md](specs/07-subagent-isolation.md)

## 阶段 7：任务回归测试集

至少两个机制完成后再建立任务测试集。任务来自已经出现的真实失败，每个任务都有独立于 agent 的准备与验证程序；结果分为模型失败、API 失败、超过最大步数、超时和测试框架崩溃。

它用于发现回归，不是通用基准测试。样本量、端点、模型、日期和原始 JSONL 必须公开；不报告无法由原始记录重算的百分比或成本。

## 完成标准

一个机制同时满足以下条件才标为“已验证”：

1. 实现已经落地；
2. 离线回归测试在删除关键实现后会失败；
3. [机制表](mechanisms.md)链接真实测试名和 ADR；
4. ADR 的“回头看”记录实现偏差；
5. README 才将它列为已实现能力。

沙箱与检查点还必须通过真实的平台与文件测试矩阵；否则状态只能是“设计中”。
