# 后台 shell 输出的完成边界

> 状态：已实现。monitor 与状态发布位于 `mini_agent/tools/bash_tool.py:151-189`，读取视图位于 `mini_agent/tools/bash_tool.py:552-575`，离线回归位于 `tests/test_background_shell_lifecycle.py:45-52,165-188`；取舍见 [ADR-0023](../decisions/0023-background-shell-completes-after-stdout-eof.md)。

## 问题证据

旧 monitor 看到非空退出码就停止读取，即使 stdout 管道仍有缓冲字节；随后发布 `completed`，而唯一读取工具无法恢复遗漏内容（`git show a6d4881^:mini_agent/tools/bash_tool.py | nl -ba | sed -n '151,181p'`）。

## 本轮不变量

1. monitor 是后台 stdout 的唯一读取者。
2. 自然完成时，stdout 返回 EOF 后才结束读取。
3. EOF 后等待进程并取得退出码，再发布 `completed` 或 `failed`。
4. 已经出现退出码但仍在管道中的完整行必须进入 `output_lines`。
5. `BashOutputTool` 继续只消费 manager 持有的行，不直接竞争管道。
6. timeout 和进程存活时的普通读取异常继续按既有策略重试。

## 不在范围

不保证主动终止期间的尾部输出，不改变进程组、后代进程、输出容量、过滤游标、解码、前台 shell 或模型可见投影。

## 离线验证

- 已退出 fake process 的 stdout 依次返回两行与 EOF；
- monitor 完成后读取工具得到两行，shell 状态与退出码正确，`wait()` 恰好一次；
- 恢复以退出码控制读取循环时，对应回归转红；
- 既有取消、终止、并发关闭与真实 bash 定向集合保持通过。
