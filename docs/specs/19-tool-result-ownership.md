# 工具返回值的接纳所有权

> 状态：已实现。工具返回的接纳与投影位于 `mini_agent/core/tool_execution.py:108-132,162-185`，离线回归位于 `tests/test_tool_execution.py:671-695`；取舍见 [ADR-0025](../decisions/0025-executor-owns-admitted-tool-results.md)。

## 问题证据

执行器曾直接保留工具返回的可变 `ToolResult`。`ToolFinished` 得到自己的深快照，但同步观察者若通过工具保留的别名修改原对象，随后模型消息会读取新值，造成事件事实与 Session 历史分叉（`git show da13e33^:mini_agent/core/tool_execution.py | nl -ba | sed -n '108,132p;162,185p'`）。

## 本轮不变量

1. 合法 `ToolResult` 在工具协程返回后立即复制，执行器只发布和投影自己的快照。
2. 工具保留的返回对象后续仍可自行修改，但不能改变 `ToolFinished`、模型消息或 Session 历史。
3. 未知工具、普通异常与非法返回继续由执行器创建同序失败结果。
4. 原始成功/失败字段、每条输出预算、事件类型和成组提交语义不变。

## 不在范围

不把 `ToolResult` 改成不可变类型，不增加结构化结果、序列化层、工具权限、并行、重试、取消恢复或持久事实日志。

## 离线验证

- 工具返回并保留内容为 `original` 的结果；
- 同步观察者收到 `ToolFinished` 后把工具别名改成 `mutated-after-event`；
- 事件快照与最终工具消息仍为 `original`，工具自己的别名保持变异值；
- 删除接纳复制时，只有最终工具消息被污染，对应回归转红。
