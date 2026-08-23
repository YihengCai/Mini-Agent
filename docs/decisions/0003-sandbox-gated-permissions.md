# ADR-0003：argv 结构化权限 + 以「已验证的沙箱」为门的 ask→allow 降级

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：docs/specs/03-sandbox-permissions_CN.md · docs/BUILD_LIST_CN.md「阶段 4：执行安全」· docs/AGENT_ROADMAP_CN.md「上游基线审计」· ADR-0002（权限提示是事件缝的一次往返）· docs/PITFALLS.md P-006

## 背景

执行环节今天**零约束**。`grep -rn "permission\|approve\|sandbox\|confirm"` 扫 `mini_agent/*.py mini_agent/tools/*.py mini_agent/llm/*.py mini_agent/acp/*.py mini_agent/schema/*.py mini_agent/utils/*.py`（排除 vendored 的 `mini_agent/skills/`）返回空。具体形态：

- `bash_tool.py:339` 就是 `shell_cmd = command`，`bash_tool.py:391-392` 把它原样交给 `create_subprocess_shell`。唯一的约束是 `cwd=self.workspace_dir`，`cd ..` 一个 token 就破了。
- 文件工具没有路径收敛：`file_tools.py:113`、`:200`、`:261` 都是 `if not file_path.is_absolute(): file_path = self.workspace_dir / file_path` —— 绝对路径分支**完全跳过**拼接，且从不与 workspace 比较。
- 路径解析方式本身是错的：`file_tools.py:72,164,221` 用 `Path(workspace_dir).absolute()`。本机实测（Darwin 25.5.0，2026-08-24）：`Path('/tmp/x').absolute()` → `/tmp/x`，`.resolve()` → `/private/tmp/x`。seatbelt 内核按 realpath 匹配，这个分歧会让沙箱拒绝 agent 写自己的工作区。
- 分发点已经分叉：`agent.py:435-436` 一份，`acp/__init__.py:157` 一份。任何挂在 `agent.py` 上的闸门，在 ACP 里都会静默缺席。

## 选项

1. **A —— argv 结构化解析 + 沙箱为门的降级**：词法前先对原始字符串做引号状态扫描，`shlex.shlex(punctuation_chars=True)` 切 segment，按 `(basename(argv0), 第一个非 flag 参数)` 匹配规则，复合命令取 `min()`，解析失败 fail-closed；只有当 OS 沙箱（macOS seatbelt / Linux bwrap）被**双向探针证实激活**时，才把 `ask` 降级为 `allow`。
2. **B —— 正则 denylist**：对命令字符串匹配 `rm -rf`、`sudo`、`curl | sh` 之类的模式。
3. **C —— 完整 shell 文法**：`bashlex` 或 `tree-sitter-bash`，正确处理 heredoc、`$( )` 嵌套、重定向。
4. **D —— 只做权限，不做沙箱**：全靠提示，每个危险动作问人。
5. **E —— 只做沙箱，不做权限**：把一切关进 seatbelt/bwrap，不问人。

## 决定

选 A，并把耦合方向写死：**解析器决定 ask/allow，内核决定 safe/unsafe；解析器永远不是"围堵"的承重墙。** 强制点放在**构造时包装**（`cli.py:414` 的 `BashTool`、`cli.py:422-424` 的 `ReadTool/WriteTool/EditTool` 各包一层 `GuardedTool`），不放在分发点 —— 因为分发点有两个（`agent.py:436` 与 `acp/__init__.py:157`），而构造点只有一个（`add_workspace_tools`，`cli.py:399`，`acp/__init__.py` 走的是同一个函数）。沙箱本身没法放进 wrapper，它必须改变子进程怎么 spawn，所以注入进 `BashTool.__init__`。

明确不做：规则 DSL（规则就是 `rules.py` 里一个 Python 列表）、跨 session 持久化的授权、逐工具的 MCP 策略（所有 MCP 工具统一坍缩成一个 ASK）、seccomp / landlock、rlimit / cgroup、Windows。

## 为什么否决其他的

**B —— 正则 denylist。** 它连引号都没有概念，所以两个方向同时错：`git commit -m 'rm -rf /'` 误报（用户学会无视提示），`npm run build; npm publish` 漏报。后者不是假想 —— 本机实测 `shlex.split("npm run build; npm publish")` → `['npm', 'run', 'build;', 'npm', 'publish']`，**`;` 从来不是一个 token**，`npm publish` 被读成给 `npm run` 的两个额外参数，一条"允许 npm run"的规则就批准了一次 publish。而每给正则打一个补丁就制造一个新绕过口。**什么条件下它反而是对的**：只做**告警不阻断**的那一层 —— 给人看的高亮提示、事后审计的 grep —— 那时误报的代价只是一行多余的黄字，而覆盖率比精确性更值钱。

**C —— 完整 shell 文法（bashlex / tree-sitter-bash）。** 它确实买来正确的 heredoc 与 `$( )` 嵌套，但**不解决真正的问题**：`bash -c "$PAYLOAD"` 里 payload 是运行时才存在的。本机实测 `shlex.split('bash -c "rm -rf /tmp/y"')` → `['bash', '-c', 'rm -rf /tmp/y']` —— 就算文法完美，你拿到的仍是一个不透明 token；换成 `bash -c "$X"` 连字面量都没有。shell 命令分类**在一般情况下不可判定**（`eval`、`$IFS` 分词、PATH 遮蔽、heredoc）。加上它是一个新依赖，而收益落在正确率的最后几个百分点上。**什么条件下它反而是对的**：审计对象是**静态语料**而不是运行期调用 —— 比如扫一遍 CI 脚本、Makefile、Dockerfile，那里没有沙箱兜底、也没有"稍后再问人"这个选项，覆盖率就是全部价值。

**D —— 只做权限，不做沙箱。** 纯提示的 UX 半衰期大约两天：用户一小时内被 `pytest`、`npm ci`、`git status` 打断二十次，然后找到"总是允许"或 `--yolo`，权限引擎沦为装饰 —— 一套没人愿意开着的权限系统，安全性是零。更根本的一条：**没有沙箱，你就没有资格自动放行任何东西**，因为一次错误的 `allow` 后果是不可逆的。**什么条件下它反而是对的**：在不支持沙箱的平台上 —— 而这正是本设计里 `NoSandbox.verified_active()` 恒返回 `False` 的那条路径：引擎永不降级，每个 `ask` 都到人手上。优雅的 no-op 不等于降级。

**E —— 只做沙箱，不做权限。** 沙箱要收得紧，就必须给少数真需要出去的事情（网络安装、workspace 之外的写入）留一个人工出口；没有这个出口，你只能为所有人放松 profile，于是沙箱也白做了。而且沙箱管不到不走 bash 的东西 —— MCP 工具、`WriteTool` 的绝对路径。**什么条件下它反而是对的**：无人值守的一次性批处理（CI 里跑一个 agent 任务），没有人在旁边可问，任务失败可以接受重跑 —— 那时"问人"这个动作根本不存在，把 profile 收到最紧才是唯一策略。

**关于沙箱探测：必须双向实测，不能用 `shutil.which`。** 本机实测（Darwin 25.5.0，2026-08-24）：`/usr/bin/sandbox-exec` 存在；喂一份语法错误的 profile，`sandbox-exec -f bad.sb /bin/echo hi` 打印 `sandbox-exec: syntax error: expecting ')'` 并以 **rc=65** 退出。也就是说"二进制在"和"profile 真的加载了"是两件事，而失败长得和普通命令失败一模一样。若 `verified_active()` 在一份从未加载成功的 profile 上返回 `True`，`ask→allow` 就会在**任何沙箱之外**自动批准 `curl | sh` —— 这是整套设计能产生的最坏结果，而且它是静默的。所以探针要求 workspace 内的写入成功**并且**外部的写入失败，两者缺一即判定未激活。

**顺带记下两条实测的解析陷阱**（它们是选 A 的具体形态，不是选 A 的理由）：`shlex.split("echo '$X'")` 与 `shlex.split("echo $X")` 结果**逐字节相同**（都是 `['echo', '$X']`）—— posix 词法把你判断展开所需的引号信息销毁了，所以引号扫描必须在词法**之前**跑在原始字符串上；`punctuation_chars=True` 也切不开反引号，`` echo `rm -rf ~` `` 词法化成 `` ['echo', '`rm', '-rf', '~`'] ``；而 `shlex.split('echo "foo')` 抛 `ValueError: No closing quotation` —— 一句反射性的 `except: pass` 会把**解析失败变成不受检执行**。

## 怎么验证它是对的

- `pytest tests/test_permission_corpus.py -q`：约 45 组 `(command, expected_verdict, expected_reason_substring)`，**跑两遍**。第一遍 `sandbox=NoSandbox()`，看原始判定（`npm run build; npm publish` → ASK；`echo '$HOME'` → ALLOW；`echo "$HOME"` → ASK；`` echo `id` `` → DENY；`echo "unterminated` → DENY）。第二遍 `sandbox=FakeActiveSandbox()`，断言**每个 ASK 都变成 ALLOW、每个 DENY 仍是 DENY**。第二次参数化就是这条 ADR 的架构主张的可执行表达。
- `python3 scripts/sandbox_probe.py`：同一份 `BashTool` 构造两次（`NoSandbox` / `SeatbeltSandbox`），跑同一组命令，打一张 OK/BLOCKED 矩阵。**结尾必须有一节 `NOT PREVENTED`** —— `cat ~/.ssh/id_rsa` 在只禁写的 profile 下照样读；workspace **内部**的写入可以污染用户之后要 push 的仓库；走 MCP 而不是 bash 的一切。自曝缺口的那一节才是这个脚本的意义。
- 已实测（本机 Darwin 25.5.0，2026-08-24，仓库 `953b943` 工作树）：`shlex.split("npm run build; npm publish")` → `['npm','run','build;','npm','publish']`；`shlex.shlex(..., punctuation_chars=True)` 同一输入 → `['npm','run','build',';','npm','publish']`；`shlex.split("echo '$X'") == shlex.split("echo $X") == ['echo','$X']`；`shlex.split('echo "foo')` → `ValueError: No closing quotation`；`` shlex.shlex("echo `rm -rf ~`", punctuation_chars=True) `` → `` ['echo','`rm','-rf','~`'] ``；`Path('/tmp/x').absolute()` = `/tmp/x` 而 `.resolve()` = `/private/tmp/x`；`'/a/ws-evil/x'.startswith('/a/ws')` 为 `True` 而 `Path('/a/ws-evil/x').is_relative_to('/a/ws')` 为 `False`；语法错误的 SBPL profile 退出码 `65`。
- **待测**：profile 的逐条规则矩阵（`(deny file-write*)` 对 `TMPDIR` 的连带影响、外层 `.git` 的 index.lock、`(remote ip "localhost:*")` 是否放行全部出站）。这些要在实现 `profile.sb` 时逐条试射，spec 里的结论不能直接抄。
- **完全待测**：Linux bwrap 那条路径。本机没有 `bwrap`，规格里的参数一行都没跑过 —— 落地时若跑不了就在 README 里写明"Linux 路径未验证"，不要写"支持 Linux"。
- 前置修复（不做就无法验证）：`file_tools.py:72,164,221` 的 `.absolute()` → `.resolve()`。

## 回头看

> 待实现后补。
