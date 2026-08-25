# 前台 shell 中断时的直接子进程回收

> 状态：已实现。直接子进程清理入口与前台执行位于 `mini_agent/tools/bash_tool.py:241-246,431-486`，离线回归位于 `tests/test_background_shell_lifecycle.py:157-215`；取舍见 [ADR-0026](../decisions/0026-foreground-shell-reaps-on-interruption.md)。

## 问题证据

旧前台超时分支只 `kill()` 不 `wait()`，取消则越过外层普通异常处理而不做任何清理（`git show b16a7f8^:mini_agent/tools/bash_tool.py | nl -ba | sed -n '423,487p'`）。故障注入证明前者没有等待直接子进程，后者连终止信号也没有发送。

## 本轮不变量

1. 正常 `communicate()` 完成时沿用其既有进程收敛与输出结果。
2. 超时在返回失败结果前，对仍运行的直接子进程执行 `kill()` 并等待 `wait()`。
3. `communicate()` 逃逸的其他 `BaseException` 先完成同一清理，再进入既有外层错误边界；普通 `Exception` 仍转换成失败结果，`CancelledError` 等保留原对象传播，清理失败不能替换原执行异常主因。
4. 前台进程不进入 `BackgroundShellManager`，不产生 `bash_id` 或增量输出视图。

## 不在范围

不处理进程组或后代进程，不保证重复取消或操作系统拒绝 `kill()` 时仍能收敛；不改变后台监控任务、主动终止尾部输出、shell/PowerShell 命令格式、超时上限、输出解码、权限或沙箱。

## 离线验证

- 超时替身让 `communicate()` 开始后超时，结果文本不变且 `kill_calls=wait_calls=1`；
- 真实取消正在等待的 `BashTool.execute()`，捕获到相同取消文本且 `kill_calls=wait_calls=1`；
- 删除任一清理挂钩时对应回归转红；
- 既有后台生命周期和真实前台/后台 bash 定向集合保持通过。
