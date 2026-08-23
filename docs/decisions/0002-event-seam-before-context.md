# ADR-0002：先做事件层，再做上下文管理

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：`docs/specs/02-event-layer.md` · `docs/specs/01-context-manager.md` · `docs/PITFALLS.md` P-003

## 背景

`Agent.run()` 直接包含 30 处 `print()`（baseline 命令：`grep -c "print(" mini_agent/agent.py`），ACP 又在 `mini_agent/acp/__init__.py:127-165` 复制循环，且没有主循环的压缩与日志路径。上下文基准测试需要每一步的结构化数据；如果先做上下文管理器，只能继续解析 TTY 输出。

取消操作也位于相同控制流边界：`cli.py:778-781` 只设置 `cancel_event`，不调用 `Task.cancel()`；`bash_tool.py:398-409` 只在超时时终止子进程。

## 选项

1. 先实现最小事件层，再实现上下文管理器。
2. 先实现上下文管理器，事件层随后补。
3. 两个模块同时修改 `Agent.run()`。

## 决定

选择选项 1。保留 `await Agent.run() -> str`，循环只发送带类型的事件；CLI、ACP、JSONL 作为接收方。渲染、ANSI 和输出限制移到渲染器。流式输出、steering 和权限请求使用同一事件层，但不在第一步一起实现。

## 为什么否决其他的

先做上下文管理器会让压缩指标临时依赖 stdout 解析，随后还要再次改写输出路径。若上下文验证完全是离线 `build_view()` 重放，不需要运行时用量数据，选项 2 也成立。

并行实现会同时修改 `Agent.run()` 的相邻区域。只有多人开发且先固定事件接口时，选项 3 才有收益。

## 怎么验证

- `Agent(on_event=None)` 运行后 stdout 为空；
- CLI 渲染输出与改造前的 golden 结果一致；
- 中断后消息历史没有未配对记录，已完成工具结果不丢失；
- 取消长时间 shell 后没有残留子进程；
- 上下文基准测试读取 JSONL 事件，不解析 TTY 输出。

## 回头看

> 待实现后补。
