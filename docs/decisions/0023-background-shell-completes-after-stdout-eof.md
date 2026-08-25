# ADR-0023：后台 shell 在 stdout EOF 后才完成

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/tools/bash_tool.py`、`tests/test_background_shell_lifecycle.py`、提交 `a6d4881`

## 背景

`BackgroundShellManager` 的 monitor 是后台进程 stdout 的唯一读取者，读到的行写入 `BackgroundShell.output_lines`；`BashOutputTool` 之后只读取该列表（`mini_agent/tools/bash_tool.py:52-86,151-189,552-575`）。旧 monitor 却以 `process.returncode is None` 作为读取循环条件，退出码一旦出现就直接 `wait()` 并发布完成状态（`git show a6d4881^:mini_agent/tools/bash_tool.py | nl -ba | sed -n '151,181p'`）。

退出码只说明进程不再生产数据，不说明异步管道缓冲已被消费者读完。确定性 fake 令 `returncode=0` 且 stdout 仍含两行时，旧实现把 shell 标为 `completed`，`BashOutputTool.stdout` 却是空字符串。

## 选项

1. **退出码出现后停止读取**：状态发布最快，但尾部事实是否保留取决于 monitor 调度时机。
2. **先等待进程，再一次性读取剩余 stdout**：步骤直观，但等待期间未持续消费管道，输出填满缓冲时子进程可能无法退出。
3. **monitor 持续逐行读取到 EOF，再等待并发布退出状态**：沿用现有流式消费，仅把正常完成边界从退出码改为管道 EOF。

## 决定

采用选项 3。存在 stdout 时，monitor 持续调用 `readline()`，只有返回空字节才结束正常读取；之后 `await process.wait()`，最后按退出码发布 `completed` 或 `failed`（`mini_agent/tools/bash_tool.py:151-181`）。

timeout 仍重试；普通读取异常在进程存活时仍重试，退出后停止，保持既有错误策略。主动 `terminate()` 仍可能在进程退出后取消尚未完成的 monitor，本轮不承诺终止期间尾部输出；也不改变进程组、后代进程、输出容量、过滤游标、解码或模型投影。

## 为什么否决其他的

**否决退出码边界**：生产者生命周期与管道消费生命周期不是同一个状态；用前者替代 EOF 会让 `completed` 隐藏不可恢复的缺失事实。若输出本来就允许按“尽力而为”丢弃，且完成延迟比事实完整性更重要，这个方案才合适。

**否决先 wait 再读取**：持续消费 stdout 是防止管道回压阻塞子进程的必要条件。若子进程输出严格有很小的上限，或传输由不会阻塞生产者的外部日志服务承担，退出后批量读取才可以简化实现。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_background_shell_lifecycle.py tests/test_bash_tool.py` 实测 `40 passed in 9.77s`。
- fake process 在 monitor 获得调度前已经 `returncode=0`，有限流仍返回两行再 EOF；回归要求两行逐字可读、状态为 `completed`、退出码为零且只等待一次（`tests/test_background_shell_lifecycle.py:45-52,165-188`）。
- 临时恢复 `returncode is None` 循环条件时该回归 1 项转红，实测 `1 failed in 0.48s`。
- 显式排除 `external` 的完整集合实测 `312 passed, 9 deselected in 13.07s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现没有新增读取任务或缓冲副本，只让既有 owner 以真正的 EOF 作为自然完成证据。它修复的是进程自然退出后的确定性尾部丢失；主动终止、无限输出、容量控制和后代进程仍需各自的失败证据后独立进入。
