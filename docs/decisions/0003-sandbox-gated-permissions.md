# ADR-0003：结构化权限 + 仅在沙箱生效时将 ASK 降级为 ALLOW

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：`docs/specs/03-sandbox-permissions.md` · `docs/PITFALLS.md` P-006 · ADR-0002

## 背景

baseline 没有权限控制或沙箱：`bash_tool.py:391-392` 把原始命令交给 `create_subprocess_shell`；`file_tools.py:113,200,261` 接受工作区外的绝对路径；CLI 与 ACP 各自分发工具。P-006 进一步证明 `shlex.split()` 不能表示组合 shell 命令。

本机探测还证明“可执行文件存在”不等于沙箱生效：有语法错误的 SBPL 配置使 `sandbox-exec` 以 rc=65 退出。沙箱必须同时通过内部允许与外部拒绝探测。

## 选项

1. 结构化权限解析器；只有经过验证的沙箱下 ASK 才能降级为 ALLOW。
2. 正则表达式拒绝列表。
3. 完整 shell grammar（bashlex/tree-sitter-bash）。
4. 只做权限控制。
5. 只做沙箱。

## 决定

选择选项 1。解析器根据组合命令结构产生 `ALLOW/ASK/DENY`，解析失败时默认拒绝，并采用最严格的判定。防护在工作区工具的统一构造点注入；沙箱在 `BashTool` 创建进程的路径注入。`DENY` 永不降级；`NoSandbox` 下 ASK 必须由用户决定。

## 为什么否决其他的

正则表达式拒绝列表既会误报也能被绕过，只适合提示性警告或审计搜索。

完整 shell 语法仍无法知道运行时变量、`eval` 或不透明的 `bash -c "$X"` 的真实载荷。静态 CI 或脚本审计中没有运行时沙箱时，它更合适。

只做权限控制会产生批准疲劳，错误的 ALLOW 没有内核隔离兜底；不支持沙箱的平台只能使用该模式，并禁止 ASK 降级。

只做沙箱无法表达用户对网络、工作区外写入或 MCP 副作用的意图。无人值守的 CI 任务没有人可询问时，它更合适。

## 怎么验证

- `tests/test_permission_corpus.py`：组合命令、引号、变量、子 shell、解析失败；
- 同一命令集在 `NoSandbox` 与 `FakeActiveSandbox` 下只允许 ASK 的判定变化；
- `scripts/sandbox_probe.py`：真实允许/拒绝矩阵，并打印 `NOT PREVENTED`；
- 工作区边界检查覆盖绝对路径、`..`、符号链接和相似前缀目录。

## 回头看

> 待实现后补。
