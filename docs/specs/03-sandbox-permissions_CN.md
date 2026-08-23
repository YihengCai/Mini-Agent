# 执行沙箱 + 结构化权限

> 状态：待事件缝落地后实现。每个平台必须独立通过真实拒绝测试，不能靠“检测到二进制”宣称支持。

## 要解决的问题

`BashTool` 把模型字符串直接交给 `asyncio.create_subprocess_shell()`（`mini_agent/tools/bash_tool.py:391-396`）；文件工具接受 workspace 外绝对路径和 `..`。权限分类也不能建立在 `shlex.split()` 后的第一个 token 上，反例见 [P-006](../PITFALLS.md)。

## 信任边界

权限解析器回答“是否自动执行、是否询问人”；OS 沙箱回答“即使判断错了，进程能影响哪里”。解析器不是安全边界，沙箱也不替代用户授权。

只有一次调用确实运行在经过双向探针验证的沙箱内，`ASK` 才允许降级为 `ALLOW`；`DENY` 永不降级。`NoSandbox` 下所有 `ASK` 都必须到人。

## 第一阶段平台范围

当前开发环境先实现并验证 macOS seatbelt：

- workspace 与专用临时目录可写；
- workspace 外写入被拒；
- 出站网络被拒；
- profile 路径通过参数绑定，不拼接用户路径；
- 启动探针同时证明“里面能写”和“外面不能写”。

Linux bwrap 保留同一接口和设计说明，但在 Linux 环境跑通相同 probe 与测试矩阵之前，不进入 README 的“已支持”列表。其他平台使用 `NoSandbox`，不得静默放宽权限。

## 权限解析与策略

解析过程保留原始字符串的引号/展开信息，再识别复合命令、重定向和 wrapper：

- 无法可靠解析 → fail-closed；
- `;`、`&&`、`||`、pipe 和 subshell 分段，最严格 verdict 胜出；
- 未解析变量、命令替换、heredoc、解释器 payload 至少为 `ASK`，高风险形状可直接 `DENY`；
- 规则匹配基于 `(argv0, subcommand, flags, resolved paths)`，不是字符串前缀；
- 未命中规则默认 `ASK`；
- session grant 使用规范化结构作 key，不使用原始命令文本。

权限 UI 通过事件缝请求/响应，不在 tool 内直接 `input()`；否则会与现有 Esc 输入线程竞争 stdin。

## 文件工具收敛

Read/Write/Edit 的目标先 `.resolve()`，再用 `Path.is_relative_to(workspace)` 判断。符号链接逃逸、绝对路径和不存在路径的尾部都要有明确策略。检查发生在真正打开文件之前。

闸门包装集中在 workspace tool 构造点，确保 CLI 与 ACP 得到同样的 GuardedTool；测试还要钉住所有执行路径确实经过 guard。

## 可失败工件

`tests/test_permission_corpus.py`：

- 对抗命令表在 `NoSandbox` 下验证原始 `ALLOW/ASK/DENY + reason`；
- 同一语料在 `FakeActiveSandbox` 下验证 `ASK -> ALLOW`、`DENY` 不变；
- 解析失败不会执行；
- CLI 与 ACP 装配出的写工具都被 guard 包裹；
- workspace confinement 覆盖绝对路径、`..`、符号链接和相似前缀目录。

`scripts/sandbox_probe.py` 在真实 seatbelt 下打印允许/阻止矩阵，最后固定打印 `NOT PREVENTED`：读取 workspace 外秘密、workspace 内破坏、MCP 副作用、内核或 profile 漏洞。

## 明确后置

Linux 支持声明、Windows token sandbox、容器编排、完整 shell AST、持久化 always-allow、敌意模型安全承诺。
