# ADR-0032：observer 普通异常不控制 Turn

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/agent.py`、`mini_agent/core/turn.py`、`mini_agent/core/events.py`、`mini_agent/cli.py`、`tests/test_tool_execution.py`

## 背景

提交 `faab31b` 中，同步 `event_sink` 的首个异常会进入 `_TurnEmitter._error`，agent loop 在 TurnStarted、StepStarted、ModelRequest、ModelResponse、ToolFinished、StepFinished、模型失败和 TurnFinished 等位置决定它是 Turn 主因、次因还是不可改写的终止后异常（`git show faab31b:mini_agent/core/agent.py | nl -ba | sed -n '56,85p;239,548p'`）。`TurnOutcome` 另有 `observer_error`，CLI 再绕过已损坏的 sink 输出 fallback（`git show faab31b:mini_agent/core/turn.py | nl -ba | sed -n '16,50p'`；`git show faab31b:mini_agent/cli.py | nl -ba | sed -n '258,273p;690,915p'`）。

这套主次归因没有可靠投递所需的持久化、确认、重试或多消费者；事件本身也只承诺进程内同步观察。与此同时，直接传播仍不安全：若 sink 在第一个 `ToolFinished` 抛错，第一个工具副作用已经发生，而 assistant 调用和整批结果尚未成组写入历史。

## 选项

1. **保留结构化 observer 失败矩阵**：宿主能知道观察不完整，但继续维护半套可靠投递协议。
2. **让异常直接传播**：实现最短，却可能在工具副作用后、历史提交前中断批次。
3. **最佳努力并在首错后停用**：捕获首个普通 `Exception` 后移除 sink；执行和 Turn 结果完全由模型、工具、中断与 Step 预算决定。

## 决定

采用选项 3。`_TurnEmitter.emit()` 只捕获 sink 的普通 `Exception` 并把当前 sink 置空；删除六处错误检查、observer 专用 Step 结果、主次错误合并、`TurnErrorKind`/`TurnOutcome` 的 `observer_error` 和 CLI fallback。`BaseException` 不在本项吞错范围。

事件深快照、工具串行执行、assistant/工具结果成组提交和 `TurnFinished` 身份都不变。明确放弃的是“宿主从 Turn 结果得知轨迹已不完整”；等轨迹、回放或任务级评测出现真实消费者时，应从持久化、确认或独立消费者隔离重新设计，而不是恢复当前矩阵。

## 为什么否决其他的

**否决结构化失败矩阵**：它能分类日志/渲染故障，却不能保证任何事件已经持久保存或可补发；复杂度与承诺不匹配。若未来 Turn 的完成必须等待审计轨迹确认，且事件存储能与工具事实建立明确提交边界，这套主次错误反而可能是协议的一部分。

**否决直接传播**：同步回调发生在工具批次内部，抛出时可能已有不可逆副作用；传播会让后项不执行且不提交配对历史。若 observer 本身就是与工具副作用同一事务的一部分，并能原子回滚，传播才可能合理；当前 CLI 渲染与文本日志不具备该条件。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_agent_session_offline.py tests/test_agent_loop_offline.py tests/test_tool_execution.py tests/test_config_provenance.py` 实测 `66 passed in 0.74s`。
- 双工具回归让 sink 在首个 `ToolFinished` 抛出 `OSError`；sink 不再收到后续事件，第二个工具仍执行，assistant 与两条结果完整入历史，Turn 正常以 `max_steps` 结束。
- 临时恢复异常传播后，该回归实测 `1 failed in 0.35s`。
- `.venv/bin/python -m pytest -q` 实测 `270 passed, 8 deselected in 13.36s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

生产代码净减 87 行，测试净减 77 行；五类主因/次因专用场景收敛为一个副作用最强的正向隔离回归。删除的是 observer 对执行的控制与错误投递承诺，事件快照、终止事件身份和模型原异常测试继续保留。
