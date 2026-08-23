# Mini-Agent 学习改造路线

## 目标

这个仓库不是要堆功能，也不是宣称当前实现已经 SOTA。目标是：在一个真实、可运行的 agent loop 上，研究现代 coding agent 的关键机制，亲手实现它们，并为每个机制留下一个离线、可失败、可复跑的证明。

“先进设计”必须同时满足：

1. 有具体失败模式，而不是从别的项目抄一个模块名；
2. 边界与不变量能写成测试；
3. 当前 endpoint 或 OS 能力经过探测，不能靠厂商假设；
4. 实现后的数字来自本仓库实测；
5. 能说明没有做什么，以及为什么。

## 文档分工

- [README](../README.md)：项目身份、当前真实能力、学习路线；不写未落地能力。
- [mechanisms.md](mechanisms.md)：机制状态的唯一真相源。
- 本文：实现顺序与依赖，不重复函数级设计。
- [specs/](specs/)：当前/下一阶段的实现边界；后期模块只留设计说明。
- [decisions/](decisions/)：真正的取舍及其反例。
- [PITFALLS.md](PITFALLS.md)：已经复现的反直觉行为。
- [reference/](reference/)：外部项目调查，只提供证据，不自动成为本项目方案。

## 当前结论

上游已经提供单 agent loop、文件与 shell 工具、CLI、MCP、skills、日志和 ACP 桥；但它仍是 demo：测试可假绿，渲染焊在循环里，取消不真正取消，压缩器会丢结构，写工具契约与实现不一致，也没有权限与沙箱。

因此学习顺序从“让机制可证伪”开始，而不是先做新功能。

## 阶段 0：可信测试底座

### 0.1 FakeLLM 与历史不变量

按 ADR-0005 实现两条路由队列、请求记录和 `assert_history_valid()`。第一批测试必须证明：少脚本会红、孤立 tool call 会红、压缩调用不会错消费主循环响应。

规格：[00-measurement-rig_CN.md](specs/00-measurement-rig_CN.md)

### 0.2 Provider 能力探针

实现 `scripts/provider_probe.py`，先测 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 中真正影响设计的能力：cache、streaming、tool pairing、连续 user、thinking 往返和上下文上限。真实 API 探针默认跳过，不进入离线测试。

### 0.3 四个窄修复

这些 bug 已有复现，不等待大模块：

- token usage 的“累计”语义与字段名对齐；
- workspace 路径统一 `.resolve()`；
- `EditTool` 拒绝空串与非唯一匹配；
- bash 取消时终止并等待子进程。

每个修复单独测试，不能顺手重构。

## 阶段 1：事件化执行内核

先把 `print()`、ACP 和 logger 从控制流中拆出来，再做正确取消、流式与 steering。完成标准：

- `agent.py` 不再负责终端渲染；
- CLI 与 ACP 使用同一循环；
- Esc 能取消 LLM/tool task；
- 历史通过“合成 tool result”修复，已完成结果不丢；
- shell 子进程不残留；
- steering 只在闭合 step 边界注入。

规格：[02-event-seam-interrupt_CN.md](specs/02-event-seam-interrupt_CN.md) · 决策：[ADR-0002](decisions/0002-event-seam-before-context.md)

## 阶段 2：上下文与 cache

把 `self.messages` 变成只追加 raw log，以确定性派生 view 发送给 provider。先实现 tool-result eviction 与单一窗口摘要；FileLedger 不放在本阶段。

cache 是条件分支：只有 C1–C3 实测成立才实现 `cache_control` 与成本比较；不支持时如实停在上下文正确性和 token 预算，不写空转代码。

完成标准：随机历史下无 orphan、重复构建逐字节相同、摘要失败不会增大输入、边界找不到时安全拒绝。

规格：[01-context-manager_CN.md](specs/01-context-manager_CN.md) · 决策：[ADR-0007](decisions/0007-split-file-state-from-context.md)

## 阶段 3：让 agent 真正会改代码

### 3.1 事务性编辑与诊断

实现唯一匹配、陈旧检查、多 hunk 预验证、单文件原子替换与 diff 范围诊断。跨文件崩溃原子性在 checkpoint 之前不作虚假承诺。

规格：[04-transactional-edit_CN.md](specs/04-transactional-edit_CN.md)

### 3.2 搜索与输出预算

实现 Glob/Grep、ignore 语义、workspace confinement 和自描述截断；同时钳制 bash 成功/失败输出。PageRank repo map 不做，除非未来出现外部定位评测证明 Glob/Grep 不够。

规格：[05-code-retrieval_CN.md](specs/05-code-retrieval_CN.md) · 决策：[ADR-0004](decisions/0004-cut-repomap-pagerank.md)

## 阶段 4：执行安全

在事件缝上接权限请求，在 workspace tool 构造点统一套 guard。当前机器先把 macOS seatbelt 做完整；Linux bwrap 只有在 Linux 环境通过相同 probe 后才声明支持。

完成标准不是“命令看起来被拦了”，而是：真实 OS 拒绝测试、解析失败 fail-closed、NoSandbox 不自动放行、`NOT PREVENTED` 清单公开。

规格：[03-sandbox-permissions_CN.md](specs/03-sandbox-permissions_CN.md) · 决策：[ADR-0003](decisions/0003-sandbox-gated-permissions.md)

## 阶段 5：控制面与信任边界

### 5.1 AGENTS.md 信任阶梯

文件来源的 `AGENTS.md`、SKILL、MCP 响应和工具输出都是数据，不得进入 system role 授权动作。先修现有 skills metadata 直接拼 system prompt 的注入面，再增加 AGENTS.md 发现规则。

### 5.2 Plan mode

PLAN 模式从 provider 可见工具表中物理移除写工具；唯一出口是人类接受/拒绝的 `exit_plan_mode`。拒绝也必须闭合 tool call，并在轮次边界切换模式。

这两个模块进入实现前各写一份短 spec；现在不预写数据结构。

## 阶段 6：恢复与委派

### 6.1 Checkpoint / rewind

在 raw log、ContextState 和 mutating-tool 边界稳定后实现 shadow store。任何不能逐字节往返或会触碰用户 `.git` 的版本都不发布。

设计说明：[06-checkpoint-rewind_CN.md](specs/06-checkpoint-rewind_CN.md)

### 6.2 子 agent 上下文隔离

在事件、上下文、权限和预算记账稳定后实现。唯一有价值的主张是“探索原文不进入父上下文”，不是“能启动第二个 Agent”。

设计说明：[07-subagent-isolation_CN.md](specs/07-subagent-isolation_CN.md)

## 阶段 7：小型任务回归

至少两个机制落地后再建任务 suite。任务来自已经出现的真实失败，每个任务有外部 setup/verify；结果区分模型失败、API 失败、max steps、timeout 和 harness crash。

它是回归检测器，不是通用 benchmark。样本规模、endpoint、模型、日期和原始 JSONL 必须公开；不报告无法由原始记录重算的百分比或成本。

## Definition of Done

一个机制只有同时满足以下条件才算“已验证”：

1. 实现代码已落地；
2. 有一个离线测试会在删除关键实现后失败；
3. [mechanisms.md](mechanisms.md) 链接到真实测试名和 ADR；
4. ADR 的“回头看”记录实际偏差；
5. README 只在此时把它移入“已实现”。

沙箱与 checkpoint 额外要求真实平台/文件矩阵通过；否则只能写“设计中”。
