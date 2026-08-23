# 子 agent 上下文隔离设计说明

> 状态：后期模块，未实现。它建立在事件、上下文、权限和预算记账之上。

## 学习目标

子 agent 的价值不是“再开一段聊天”，而是限制哪些探索字节进入父上下文。父 agent 只能收到有界、可验证的结果；子 transcript 永不直接拼回父消息列表。

## 必须保持的不变量

- 父上下文一次委派只增加 assistant tool call 与一个有界 tool result；
- 子 agent 没有写工具、`task` 工具或默认 MCP 权限；
- 唯一正常出口是结构化 `submit_report`；
- 报告中的 path/line/quote 在返回父 agent 前对 workspace 验证；
- 步数、输入 token、墙钟时间分别限额，任一耗尽都进入有界抢救轮；
- 父取消单向传播给子，子失败不能取消父；
- 不递归派生子 agent；
- 权限请求默认拒绝，不在父 UI 中静默批准。

## 候选形状

父 agent 通过 `task` tool 创建一个共享 LLM client、独立消息列表的只读 Agent。子 agent 只获得 Read/Glob/Grep 与经过权限层认可的只读工具；结束时调用 `submit_report` 返回 questions、answers、evidence、files examined、unresolved。

报告长度通过确定性降级阶梯限制，不使用第二次 LLM 摘要。预算耗尽时只保留 `submit_report`，给一次 partial report 机会；仍失败则返回明确失败 ToolResult。

## 进入条件

1. 父循环已有事件流和结构化结束原因；
2. context manager 已落地，可测父上下文峰值；
3. permission engine 能构造只读工具子集；
4. shell registry 与取消路径不再依赖进程全局隐式状态；
5. FakeLLM 支持第三类 route，且有需求证明两队列不够。

## 最小验收

`tests/test_subagent_isolation.py` 必须证明：

- 子 agent 读取带唯一标记的大 fixture 后，标记不出现在父消息序列；
- 父消息只增长一个闭合 tool 组；
- 写工具、MCP 和递归 task 不在子工具表；
- 伪造 path/quote 被标记并降低置信度；
- 预算耗尽只多一次抢救调用；
- 取消与超时不遗留子进程或后台 shell。

任务级 before/after 评测等本机制落地后再设计；不能拿旧压缩器做基线后提前写死预测数字。
