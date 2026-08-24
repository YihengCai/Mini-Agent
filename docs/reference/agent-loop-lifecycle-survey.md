# 主流 coding agent harness 的执行生命周期对照

- 调查日期：2026-08-24
- 范围：Codex、OpenHands SDK、SWE-agent、mini-SWE-agent、aider 与 SWE-bench 的公开源码。
- 用途：记录外部实现怎样划分会话、一次执行和任务判定，为本项目下一步提供候选方向；本文不是 ADR，不表示候选接口已经采纳。

## 本项目当前问题

当前 `Agent` 同时持有长期消息、工具、压缩状态、取消句柄和一次执行的步数控制（`mini_agent/core/agent.py:33-73,374-589`）。它不是每次新建的一次性对象，但公开边界仍是一次性的 `await run() -> str`，因此形成了“长期状态对象 + 一次性调用 contract”的混合形态。

这个形态有三条可以直接复查的失败证据：

1. 模型只要不再请求工具，core 就把一次执行命名为 `completed`（`mini_agent/core/agent.py:475-491`）；这只能证明模型交回控制权，不能证明用户任务已经完成。
2. 正常回复、取消、模型错误和步数上限都返回字符串，停止原因只存在于可选的同步事件中（`mini_agent/core/agent.py:400-455,475-501,583-589`；`mini_agent/core/events.py:14-30`）。
3. CLI 在非交互模式和交互模式都丢弃 `run()` 的返回值，实际显示依赖事件接收器（`mini_agent/cli.py:584-597,777-798`）。因此字符串既不是主要消费边界，也不能承载持续控制。

## 外部实现怎样划分边界

| 项目 | 长期状态所有者 | 最小执行单位 | 客户端边界 | “完成”的含义 |
|---|---|---|---|---|
| Codex | thread/session 持有上下文与活动 turn | turn 内部继续拆成模型与工具步骤 | 客户端提交操作并消费事件；`turn/start` 先返回 turn，再持续发送生命周期和条目事件 | `TurnComplete` / `TurnAborted` 描述 turn 已终止，不判断代码任务是否正确 |
| OpenHands SDK | `LocalConversation` 持有状态和事件日志 | `Agent.step()` 产生下一批动作，`Conversation.run()` 负责循环 | 消息进入 conversation，状态变化和动作写入事件日志 | `FINISHED` 是循环状态；可选 goal controller 在循环外继续检查和追问 |
| SWE-agent | agent 持有历史，`run()` 驱动多个 `step()` | `StepOutput` 显式记录 `done`、`exit_status` 和 `submission` | `AgentRunResult` 返回运行信息与 trajectory（轨迹） | agent 的退出状态与补丁正确性分开；SWE-bench 在外部执行测试判定 |
| mini-SWE-agent | agent 持有消息和环境，公开 `step()` 供循环调用 | 一次模型响应及其动作 | `run()` 返回退出状态和提交内容，同时保存 trajectory | 退出只是 harness 停止；评测仍由外部完成 |
| aider | 长期 `Coder` 对象持有消息、仓库和编辑状态 | `run(with_message)` 可执行一次用户输入，内部继续驱动模型与工具 | 交互循环和单次消息都围绕同一个 `Coder`；文本返回主要是适配便利 | 文本返回不等于测试通过；其基准脚本另行执行和评分 |

对应的一手源码：

- Codex 的 [app-server 协议说明](https://github.com/openai/codex/blob/2df67054232090af8d2fa197c46b994bc2b0dda1/codex-rs/app-server/README.md)、[操作与事件定义](https://github.com/openai/codex/blob/2df67054232090af8d2fa197c46b994bc2b0dda1/codex-rs/protocol/src/protocol.rs) 和 [`CodexThread`](https://github.com/openai/codex/blob/2df67054232090af8d2fa197c46b994bc2b0dda1/codex-rs/core/src/codex_thread.rs)；链接固定在本次调查的提交 `2df6705`。
- OpenHands 的 [`Agent.step()`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/agent/agent.py)、[`LocalConversation.run()`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)、[会话状态](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py) 和 [goal controller](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/goal/controller.py)。
- SWE-agent 的 [`DefaultAgent`](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/agents.py) 与 [`StepOutput` / `AgentRunResult`](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/types.py)，以及 mini-SWE-agent 的 [`DefaultAgent.run()` / `step()`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py)。
- aider 的 [`Coder.run()`](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py#L794-L873) 与 [基准评测入口](https://github.com/Aider-AI/aider/blob/main/benchmark/benchmark.py)。
- SWE-bench 的 [评测说明](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md) 和 [`get_eval_report()`](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py) 把补丁执行结束与测试解析结果分开。

这些项目没有统一 API，也并非都取消了 `run()`。共同点是：可继续的会话、一次用户输入触发的 turn（执行段）、一次模型采样与工具处理的 step（步骤），以及任务是否正确，是不同层级。一次模型不再调用工具，最多表示当前 turn 交回控制权。

## 三层终止语义

本项目下一步应先避免把三层语义压进一个 `completed`：

1. **turn 停止**：由 agent loop 的可观察控制流决定，例如模型交回控制权、用户中断、预算耗尽或内部失败。它产生停止原因和最后一条消息，但不产生 `success=True`。
2. **任务继续或结束**：交互模式由用户决定；无人值守长任务可由独立 supervisor（监督器）依据明确检查继续发起 turn。模型声明“完成”只能是一个信号，不能替代检查。
3. **评测结果**：SWE-bench 一类评测器在 harness 外应用补丁、运行测试并给出 `resolved`。评测成功不应成为通用 agent loop 的内部状态。

## 候选改进方向

候选架构是“长期会话 + 可寻址 turn + 内部 step”，而不是给现有字符串再包一层数据类：

```text
CLI / benchmark client
        commands ↓       ↑ events
      AgentSession（长期状态与单个活动 turn 的所有者）
            └─ AgentLoop / TurnController
                    └─ step：模型请求 → 工具调用 → 观察结果

TaskSupervisor / BenchmarkEvaluator（会话外的完成判定）
```

- 客户端启动一个 turn 后获得句柄或标识，持续消费带 turn 标识的事件，并可等待终止、请求中断；以后出现真实需求时再增加 steering。
- `TurnOutcome` 只汇总停止原因、最后消息和错误。权威事实来自 turn 生命周期与事件，不把任务成功、统计或 trajectory 提前塞进结果。
- 当前 `AgentEventSink` 可以作为 CLI 的过渡适配器保留，但借用对象的同步回调不应成为未来持久化或多客户端 contract。
- `run_once()` 可以保留为脚本便利封装：内部启动一个 turn、等待终止并返回摘要；它不是 core 唯一入口。
- 这个边界是进程内 contract，不要求恢复 ACP。只有出现真实的跨进程或多客户端需求并有端到端测试时，才需要协议层。

## 方案取舍

1. **不只把 `str` 换成结构化 `RunResult`**：它能消除错误字符串与正常文本混淆，却仍让会话、活动执行和任务完成共享一个调用边界，也没有定义并发输入、中断或事件归属。若项目只做一次性批处理，这个较小方案反而足够。
2. **不照搬 Codex 的完整服务与事件存储**：当前只有一个本地 CLI，没有恢复、多客户端或跨进程的验证需求。若这些需求出现，稳定事件信封和持久化日志才值得成为当前工作。
3. **不把 `finish` 工具等同于成功**：显式结束工具可以表达“模型希望停止”，但模型仍可能误判。若某类任务确实没有确定性检查，它可以作为 supervisor 的输入，而不是最终裁决。
4. **第一步不同时实现长任务 supervisor 和基准评测**：先让 turn 的身份、状态所有权和停止原因可以离线验证；否则上层监督器会建立在含糊的 `completed` 上。完成这一步后，logger、stats、trajectory 与 SWE-bench 子集才有稳定的消费边界。
