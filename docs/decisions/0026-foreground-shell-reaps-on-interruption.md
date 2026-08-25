# ADR-0026：前台 shell 中断前回收直接子进程

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/tools/bash_tool.py`、`tests/test_background_shell_lifecycle.py`、提交 `b16a7f8`、[ADR-0009](0009-runtime-owned-background-shells.md)

## 背景

前台 `BashTool` 创建直接子进程后以 `wait_for(process.communicate())` 等待。超时分支只调用 `kill()` 就返回，外层 `except Exception` 又不会接住 `CancelledError`，因此调用者取消时完全没有回收动作（`git show b16a7f8^:mini_agent/tools/bash_tool.py | nl -ba | sed -n '423,487p'`）。

故障注入在旧实现上实测两项转红：超时为 `kill_calls=1, wait_calls=0`，取消为 `kill_calls=0, wait_calls=0`；进程已经由本次调用创建，却没有持有者等待它收敛。

## 选项

1. **只在超时后补一次 `wait()`**：修复现有超时测试，但调用者取消和其他逃逸异常仍遗留进程。
2. **把前台进程登记进 `BackgroundShellManager`**：复用已有关闭机制，但会把一次同步调用变成可跨调用观察的后台资源，并改变结果与标识符 contract。
3. **前台调用局部持有并统一终止、等待**：正常完成仍由 `communicate()` 收敛；超时和逃逸异常共用一个直接子进程清理入口。

## 决定

采用选项 3。模块级 `_kill_and_wait()` 在进程仍运行时先 `kill()`，随后始终等待 `wait()`（`mini_agent/tools/bash_tool.py:241-246`）。前台超时先调用它，再返回既有失败结果；`communicate()` 逃逸的其他 `BaseException` 先调用它，再回到既有外层错误边界：普通 `Exception` 仍转换成失败结果，`CancelledError` 等则保留原对象传播。清理失败时，原执行异常仍是重抛主因（`mini_agent/tools/bash_tool.py:431-486`）。

本轮不把前台进程交给 `BackgroundShellManager`，不改变命令格式、正常输出、超时文本、进程组、后代进程、权限或沙箱。

## 为什么否决其他的

**否决只补超时 `wait()`**：超时和取消都发生在同一个“已取得直接子进程、`communicate()` 未正常完成”的边界，只修一个异常类型会继续让资源语义取决于退出方式。若宿主禁止取消，且传输层只能以超时结束，这个最小方案才足够。

**否决登记进后台管理器**：前台调用的 contract 是等待命令并直接返回结果，没有稳定标识符，也不允许后续 `bash_output` 或 `bash_kill` 介入。若未来需要宿主从另一个 Turn 取消前台命令、增量读取其输出或在会话恢复后继续控制，管理器所有权反而合适，但那需要新的公开生命周期。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_background_shell_lifecycle.py tests/test_bash_tool.py` 实测 `42 passed in 9.72s`。
- `tests/test_background_shell_lifecycle.py:157-215` 分别强制超时与真实 `Task.cancel()`；两条路径都要求一次 `kill()` 后 `wait()`，超时错误文本和取消文本保持不变。
- 旧实现上两项回归实测 `2 failed, 28 deselected in 0.71s`；恢复清理后为 `2 passed, 28 deselected in 0.46s`。
- 显式排除 `external` 的完整集合实测 `317 passed, 9 deselected in 13.23s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现没有扩大 `BackgroundShellManager`，也没有为前台命令增加第二份状态。P-010 已记录“进入 finally 不等于资源收敛且不能覆盖主异常”的通用教训；本项把该原则补到 `BashTool` 直接取得的前台子进程，因此没有另建 PITFALL。
