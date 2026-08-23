# 每轮检查点与 rewind（shadow git）

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Per-turn checkpoint & rewind (shadow-git snapshot store) — `mini_agent/checkpoint.py``


## 一句话

在每个 agent step 的第一次会改动文件的工具调用之前，把整个 workspace 快照进一个私有的 git object store（自己的 GIT_DIR + 自己的 GIT_INDEX_FILE + 自己的 attributes，绝不碰用户的 `.git`），记录 `commit_sha -> (turn, step, message-list snapshot)`，并让 `/rewind n` 用 `read-tree -u --reset` 恢复文件，**同时**把消息历史换成同一瞬间拍下的那份快照，这样 context 和文件系统永远不可能互相矛盾。

## 为什么这是难点

agent 循环是一个状态机，其中两个状态必须保持同步：模型对仓库的认知（它的消息历史，里面写着 "Successfully wrote to app.py"）和仓库本身。其他每一个 agent 特性都假设这两者一致。用户一说"不，退回去"，一个朴素实现就会在两个方向之一上打破这种一致：恢复了文件但保留 transcript（模型于是信心十足地去改已经不存在的函数，它下一次 `edit_file` 以 "Text not found" 失败——一个无限修复循环），或者截断了 transcript 但保留文件（模型重新写一遍已经生效的编辑，产出重复的代码）。这就是为什么 rewind 是最能暴露一个人到底有没有理解这个循环的特性。

它也是对自主性唯一站得住的答案。`max_steps=50`（agent.py:35）配上一个不受限的 `bash` 工具（bash_tool.py:391 的 `create_subprocess_shell`），意味着 agent 可以 `git reset --hard`、`rm -rf`，或者在 200 个文件上跑一次 codemod，没有任何撤销。git 自己的 reflog 救不了未提交的工作；用户编辑器的 undo 栈跨不过 40 次工具调用。没有检查点层，安全的操作模式就是"盯着每一个 diff 看"，而这恰恰否定了 agent 的意义。

最后，这是一个*机制*问题，不是管道工问题：内容寻址去重、索引 stat 缓存、attribute 中和、tracked/ignored 边界，每一条都要想清楚，而且每一条都有一个直觉但错误的实现。

## 仓库现状

代码库里任何地方都没有 checkpoint、undo 或 snapshot 相关代码。`grep -rn "checkpoint\|snapshot\|rewind" mini_agent/*.py mini_agent/tools/*.py` 什么都返回不出来。

改动点，全部无防护：
- `mini_agent/tools/file_tools.py:206` — `WriteTool.execute`：在 `file_path.parent.mkdir(parents=True, exist_ok=True)`（第 204 行）之后做 `file_path.write_text(content, encoding="utf-8")`。整文件覆盖，没有 read-before-write 检查，没有备份，也没有记录碰过哪个路径。
- `mini_agent/tools/file_tools.py:280-281` — `EditTool.execute`：`content.replace(old_str, new_str)` 然后 `write_text`。注意它用的是不带 count 的 `str.replace`，所以一个不唯一的 `old_str` 会静默改写每一处出现，尽管第 230 行的 docstring 声称要求唯一。原内容不可恢复。
- `mini_agent/tools/bash_tool.py:391`（前台）和 `:354`（后台）— `asyncio.create_subprocess_shell(command, cwd=self.workspace_dir)`，没有 `env=`，所以子进程整体继承 `os.environ`。任意改动、任意路径，而且*任何*我们全局设置的环境变量都会泄漏进 agent 自己的 `git` 调用里。

循环点：
- `mini_agent/agent.py:404` — `for tool_call in response.tool_calls:` 开始这一批；`agent.py:436` 的 `result = await tool.execute(**arguments)` 是所有工具唯一的分发点；`agent.py:468-474` 追加 `tool` 结果消息。挂 pre-mutation 快照的地方有且只有一处（第 436 行），而且它已经在一个 `try` 里面了（第 434 行）。
- `mini_agent/agent.py:376` — `self.messages.append(assistant_msg)`；紧跟在这次 append *之前*的那个索引，是这个 step 唯一的 tool-call-complete 边界。
- `mini_agent/agent.py:53` 的 `self.api_total_tokens` 和 `agent.py:55` 的 `self._skip_next_token_check` 是 rewind 必须重置的循环状态，否则一个被 rewind 过的（很短的）历史在下一 step 仍然会触发压缩。

一个 rewind 设计必须扛住的历史改写风险：
- `mini_agent/agent.py:153-233` 的 `_summarize_messages` 把 `self.messages` **整体替换**成 `[system] + [user_i, summary_i...]`（第 223 行 `self.messages = new_messages`）。任何以消息列表整数索引形式存储的检查点，在压缩第一次触发时就被静默作废。
- `mini_agent/agent.py:70-90` 的 `_cleanup_incomplete_messages` 在按 Esc 时截断到 `last_assistant_idx` —— 又一次移位索引。
- `mini_agent/cli.py:676` 的 `/clear` 做 `agent.messages = [agent.messages[0]]`。

轮次管道：`cli.py:715` 的 `agent.add_user_message(user_input)`，然后 `cli.py:775` 的 `asyncio.create_task(agent.run())`；斜杠命令在 `cli.py:661-703` 的 if/elif 链里分发，未知命令的兜底在 `cli.py:700`；补全词表是 `cli.py:600`。`Agent` 在 `cli.py:569-575` 构造。代码库里任何地方都没有 session id（`grep -n "session_id" mini_agent/*.py` → 只有 `acp/__init__.py:96`，它构造 `f"sess-{len(self._sessions)}-{uuid4().hex[:8]}"`）。`AgentLogger` 已经拥有 `~/.mini-agent/` 这个约定（`logger.py:25`）。

有两件事让这个 workspace 在实践中格外不友好：默认 workspace 是 `./workspace`（config.py:36），而它本身就被列在本仓库的 `.gitignore` 里；同时本仓库含有一个真实的 submodule（`.gitmodules` → `mini_agent/skills`），所以"被 gitignore 的子树"和"嵌套仓库"这两种情况第一天就是活的。

## 最小实现

## 新文件：`mini_agent/checkpoint.py`（约 300 LOC，纯标准库 —— 不引入新依赖）

### 磁盘布局
```
~/.mini-agent/checkpoints/<session_id>/
    git/                 # GIT_DIR (bare-ish, --template= so no hooks)
    git/info/attributes  # "* -text -filter -merge"
    index                # GIT_INDEX_FILE (persistent stat cache -> warm captures ~20ms)
    index.jsonl          # append-only checkpoint records
    messages/<id>.json.gz# message-list snapshot per checkpoint
```
`session_id = f"{ws_slug}-{time:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"`，在 `cli.run_agent` 里创建。

### 数据结构
```python
@dataclass(frozen=True)
class Checkpoint:
    id: int              # 1-based, monotonic within session
    sha: str             # commit in the shadow store
    turn: int            # index of the user message that opened this turn
    step: int            # agent-loop step (-1 for turn-start)
    label: str           # "turn-start" | "pre-write_file" | "pre-bash" | "turn-end" | "pre-rewind"
    message_index: int   # len(agent.messages) at capture — display/attribution only
    msg_path: str        # messages/<id>.json.gz  -> the authority for rewind
    real_head: str|None  # `git -C <ws> rev-parse HEAD` if workspace is a real repo
    changed: int         # paths written into this commit
    skipped_large: list[str]
    created_at: float

@dataclass
class RestoreReport:
    restored: list[str]; deleted: list[str]; left_alone: list[str]
    real_head_moved: tuple[str, str] | None
```

### git 调用（隔离这件事的全部内容就在这里）
```python
def _argv(self, *args: str) -> list[str]:
    return ["git", "--git-dir", str(self.gitdir), "--work-tree", str(self.ws),
            "-c", "core.bare=false", "-c", "core.autocrlf=false",
            "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
            "-c", "commit.gpgsign=false", "-c", "core.quotePath=false",
            *args]

def _env(self) -> dict[str, str]:
    return {**os.environ,
            "GIT_INDEX_FILE": str(self.index),      # never our real index
            "GIT_CONFIG_GLOBAL": os.devnull,        # no ~/.gitconfig leakage
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
```
`_env()` **只**作为 `env=` 传给 `subprocess.run`。绝不往 `os.environ` 里写任何东西 —— 见边界情况 4。

`init()`：
```
git init -q --bare --template= <gitdir>      # verified: yields only HEAD/config/objects/refs, zero hooks
write <gitdir>/info/attributes  = "* -text -filter -merge\n"
detect nested repos: any dir with a .git entry, depth<=4 -> warn once, they become gitlinks
refuse if ws in (Path.home(), Path("/")) or ws has >200k entries
```

### capture() —— 两阶段、定向
```python
def capture(self, *, messages: list[Message], turn: int, step: int,
            label: str, forced_paths: Iterable[str] = ()) -> Checkpoint | None:
```
1. `git status --porcelain=v1 -z -uall --no-renames` → 解析 `XY<space>path\0` 记录。这会尊重 workspace 的 `.gitignore`，在这个 6 232 文件的仓库上**冷启动耗时 19 ms**（实测；被忽略的目录塌缩成一条记录），之后 stat 缓存就是热的。
2. 与 `forced_paths`（本 step 中文件工具上报的路径）以及 shadow index 中已经 tracked 的一切取并集。
3. 丢掉匹配 `exclude` glob 的路径（`.venv/**`、`node_modules/**`、`.git/**`、`**/__pycache__/**`、`*.pyc`），以及 `st_size > max_file_bytes` 的路径（默认 5 MB）→ 记入 `skipped_large`。
4. 如果剩下的集合为空 → 返回一条复用上一个 `sha` 的记录（不产生新的 commit 对象）。对 `bash: ls` 这种是廉价空操作。
5. 精确 force-add 这个集合，走 stdin 且以 NUL 分隔，这样带空格/引号/`$` 的文件名能活下来：
   `git add -f --ignore-errors -A --pathspec-from-file=- --pathspec-file-nul`（用一个字面命名为 ``we ird'$file.py`` 的文件验证过；同一次调用也正确地把删除记录为 `AD`/` D`）。
6. `T = git write-tree`；`C = git -c user.email=mini-agent@localhost -c user.name=mini-agent commit-tree T [-p prev] -m body`，其中 `body` 内嵌 `turn/step/msgidx/label`，使得这个映射仅凭 object store 就能还原；`git update-ref refs/mini-agent/cp/<id> C`。
7. `gzip.open(msg_path,"wt").write(json.dumps([m.model_dump(exclude_none=True) for m in messages]))`。
8. 把记录追加到 `index.jsonl`。

刻意用 plumbing（`write-tree`/`commit-tree`/`update-ref`）而不是 `git commit`：没有 hooks，不要求 `user.name`，不会弹 GPG 签名提示，不走 `.gitmessage` 模板。

### restore()
```python
def restore(self, cp: Checkpoint) -> RestoreReport:
    self._sync_index()                       # git add -f -A <same targeted pathspec set>
    before = self._git("diff","--name-status", cp.sha)   # for the report
    self._git("read-tree","-u","--reset", cp.sha)
    ...
```
`_sync_index()` 这第一行是承重的：`read-tree -u --reset` 只会删除那些**在 index 里**且不在目标 tree 中的工作区文件。已验证 —— index 同步之后，检查点之后创建的一个文件和整个目录都被删掉了，`a.txt`（其间被删）被复活，被修改文件的内容逐字节恢复。

### rewind() —— 精神上必须原子的那部分
```python
def rewind(agent: "Agent", store: CheckpointStore, cp: Checkpoint,
           mode: Literal["both","code","conversation"] = "both") -> RestoreReport:
    store.capture(messages=agent.messages, turn=agent._turn_index, step=-1,
                  label="pre-rewind")                      # rewind is itself undoable
    report = store.restore(cp) if mode in ("both","code") else RestoreReport(...)
    if mode in ("both","conversation"):
        msgs = [Message(**d) for d in json.loads(gzip.open(cp.msg_path,"rt").read())]
        assert _is_boundary(msgs), "checkpoint was taken mid tool-call batch"
        agent.messages = msgs
        agent.api_total_tokens = 0            # agent.py:53 — stale count re-fires compaction
        agent._skip_next_token_check = False  # agent.py:55
    return report

def _is_boundary(msgs) -> bool:
    """Every assistant tool_call id has a following tool message with that id."""
    pending: set[str] = set()
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            pending |= {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            pending.discard(m.tool_call_id)
    return not pending
```
存**消息列表**而不是索引，才是它能扛过 `_summarize_messages`（agent.py:223）和 `_cleanup_incomplete_messages`（agent.py:86）的原因。一份 gzip 过的 80k token 历史约 60 KB。

### diff() —— 免费的，因为快照就是 commit
```python
def diff(self, a: Checkpoint, b: Checkpoint | None = None, mode="stat") -> str
#  stat  -> git diff --stat  a.sha b.sha
#  patch -> git diff -U3     a.sha b.sha
#  live  -> git diff --stat  a.sha            (checkpoint vs current worktree)
```

## 对现有文件的精确改动

- **`agent.py:1-16`** 加 `from .checkpoint import CheckpointStore, MUTATING_TOOLS`。
- **`agent.py:22-31`**（`__init__` 签名）加 `checkpointer: "CheckpointStore | None" = None`。
- **`agent.py:53-55`**（`__init__` 末尾）加 `self.checkpointer = checkpointer`、`self._turn_index = 0`、`self._step_boundary = 0`、`self._captured_this_step = False`、`self._touched_paths: set[str] = set()`。
- **`agent.py:58-61`**（`add_user_message`）在 append 之后：把 `self._turn_index = len(self.messages) - 1` 顶上去，并且若 `self.checkpointer` 存在则调用 `self.checkpointer.capture(messages=self.messages, turn=self._turn_index, step=-1, label="turn-start")`。正是这次 turn-start capture，让用户/编辑器在两轮*之间*做的改动能在之后的 rewind 中幸存。
- **`agent.py:376`** 紧**接在** `self.messages.append(assistant_msg)` **之前**插入 `self._step_boundary = len(self.messages); self._captured_this_step = False; self._touched_paths.clear()`。
- **`agent.py:433-436`** 把 `tool = self.tools[function_name]; result = await tool.execute(**arguments)` 这一对替换成：
```python
tool = self.tools[function_name]
if self.checkpointer and function_name in MUTATING_TOOLS and not self._captured_this_step:
    self._captured_this_step = True
    await asyncio.to_thread(                       # ~20-40ms of subprocess; don't block the loop,
        self.checkpointer.capture,                 # bash_tool's background monitors live on it
        messages=self.messages[: self._step_boundary],
        turn=self._turn_index, step=step, label=f"pre-{function_name}")
if function_name in ("write_file", "edit_file"):
    self._touched_paths.add(str(arguments.get("path", "")))
result = await tool.execute(**arguments)
```
  注意快照取的是 `self.messages[:self._step_boundary]` —— *不含*那条下达编辑指令的 assistant 消息的历史。恢复它会把模型退回到决策点，而且它按构造就是一个边界。
- **`agent.py:390-394` / `:399-401` / `:480-481` / `:490-492`** —— `run()` 有四条退出路径。把 `while` 包进 `try/finally`，在 `finally` 里放一次 `self.checkpointer.capture(..., label="turn-end", forced_paths=self._touched_paths)`，这样最后一轮的 diff 在正常返回、取消、以及打满 max-steps 三种情况下都有收尾快照。
- **`cli.py:569-575`** 构造 store 并传进去：
```python
store = CheckpointStore(workspace_dir, session_id, cfg=config.agent.checkpoints) if config.agent.checkpoints.enabled else None
if store: store.init()
agent = Agent(..., checkpointer=store)
```
- **`cli.py:600`** 往 `WordCompleter` 列表里加 `"/rewind", "/diff", "/checkpoints"`。
- **`cli.py:699`**（就在 `cli.py:700` 的未知命令 `else:` 分支之前）加三个 `elif` 分支：`/checkpoints` 以 `#id  turn/step  label  +a/-d  age` 形式打印 `store.list()`；`/diff [n]` 打印 `store.diff(cp[n-1], cp[n])`；`/rewind [n] [--code|--conversation]` 解析 `n`（默认 1 = 最近一个）并调用 `rewind(agent, store, cp, mode)`，然后打印 `RestoreReport`。
- **`cli.py:673-678`**（`/clear`）同时重置 `agent._turn_index = 0` 并拍一次 `turn-start`，否则检查点列表引用的是一份已经不存在的历史。
- **`config.py:32-37`** 往 `AgentConfig` 里加：`checkpoints: CheckpointConfig = Field(default_factory=CheckpointConfig)`，其中 `enabled: bool = True`、`max_file_bytes: int = 5_000_000`、`keep: int = 200`、`exclude: list[str] = [...]`。
- **`cli.py:156-186`**（`print_help`）加上这三个命令。

`mini_agent/acp/__init__.py:127-165` 是这个循环的一份漂移的副本 —— 明确不在范围内；不挂钩子，并且把这一点说出来。

## 实测性能（本仓库，git 2.55.0，macOS/APFS）
| 操作 | 耗时 |
|---|---|
| `status --porcelain -z -uall`，冷，空 index，6 232 个文件含 `.venv` | **19 ms** |
| 定向 `add -f` 3 个变更文件 + `write-tree` + `commit-tree` | **~27 ms** |
| 全量 `git add -A -f .`（朴素设计），冷 | **4.80 s**，42 MB 对象 |
| 全量，热（stat 缓存命中） | 37 ms |
| 60 MB 全零文件 → object store | 384 KB（zlib），0.36 s |

## 边界情况

1. **树内的 `.gitattributes` 会静默破坏往返一致性。** 直觉做法（错的）：以为 `-c core.autocrlf=false`（甚至 `-c core.attributesFile=/dev/null`）就能让存储逐字节忠实。实测：workspace 里有 `* text=auto` 时，一个 CRLF 文件 checkpoint 再 restore 回来变成了 LF（md5 59b0d7… → dd8c6a…）—— 这是对用户树里每一个行尾的静默改写；而有 `*.dat filter=lfs` 时，shadow store 会去调用 LFS 的 clean filter。`core.attributesFile` 输了，因为树内的 `.gitattributes` 优先级高于它。正确处理：把 `* -text -filter -merge` 写进 `$GIT_DIR/info/attributes`，它的优先级高于树内 attributes —— 对 CRLF 文件和 LFS 标记文件都验证为逐字节一致。刻意**不**加 `-diff`：它同样能中和 attributes，但会让 `git diff` 把每个文件都报成二进制（`-	-	crlf.txt`），毁掉免费的每轮 diff。

2. **用错 plumbing 去 restore 会把 agent 新建的文件留在原地。** 直觉做法（错的）：`git checkout-index -a -f` 或 `git checkout <sha> -- .` —— 两者都能把 checkpoint 里的文件写回去，但都无从知道 agent 在 checkpoint *之后*创建的文件，于是这次 rewind 静默地把 `broken_helper.py` 留在磁盘上。正确处理：先用 `git add -f -A` 把当前状态同步进那个持久 index，*然后*执行 `git read-tree -u --reset <sha>`。已验证：index 同步之后，`brandnew.txt` 和整个新建的 `d2/` 目录被移除，一个被删的文件被复活，一个被修改文件的内容被恢复。不变量是 `read-tree -u` 只删除 index 知道的东西 —— 而这正是让这个存储安全的性质：**它绝不删除自己从未捕获过的文件**（被忽略、被排除或超大的文件原地保留，并在 `left_alone` 中上报）。

3. **全量 `-f` 是 27 ms 特性和 5 s 特性之间的差别。** 直觉做法（错的）：用 `git add -A -f .` 好把 gitignore 掉的文件也覆盖住。就在本仓库上，这会去哈希 `.venv` —— 第一轮就是 4.80 s 和 42 MB —— 而且它会把 `.env` 里的密钥和构建产物一并扫进一个用户从未审计过的存储里。同样直觉、同样错的做法：干脆去掉 `-f`，于是一个被 `.gitignore` 的 workspace（Mini-Agent 自己的默认 workspace `./workspace` 在本仓库里就是被 gitignore 的）根本什么都不会 checkpoint。正确处理：`-f` 只作用在一份显式路径清单上 —— 本 step 中文件工具上报碰过的路径，加上 shadow index 里已经 tracked 的一切 —— 再加上 `':(exclude,glob).venv/**'` 这类 pathspec magic（已验证：exclusion magic 仍然压过 `-f`）和一个 `max_file_bytes` 过滤。由此得到的规则干净且讲得清楚：*被忽略的文件通过被碰过一次进入存储；tracked 状态此后是粘性的。* 必须明说的后果：一个只被 `bash` 改动过的 gitignore 文件永远不会被捕获，因为 `git status` 不会报告它，而工具也没有点名它。

4. **嵌套仓库变成 gitlink，不会被快照。** 直觉做法（错的）：以为整树 `add` 就是整树。已验证：在一个含 `sub/.git` 的工作树上执行 `git add -A` 会记录 `160000 <sha> sub` 并打印 "adding embedded git repository"；内容不会被存储，而强行加会直接失败 —— `fatal: Pathspec 'sub/nested.txt' is in submodule 'sub'`。本仓库自带 `.gitmodules` → `mini_agent/skills`，所以 demo 立刻就会撞上。正确处理：init 时扫描嵌套的 `.git`，警告一次，把这些子树从 capture 和 restore 中都排除掉（它们有自己的历史和自己的 reflog），并让 `/rewind` 说出 "2 nested repos not covered: mini_agent/skills"。工作树*自己*的顶层 `.git` 会被 git 自动跳过 —— 已验证，它从不出现在 `ls-files` 中。

5. **按索引截断 transcript。** 直觉做法（错的）：存 `message_index` 然后做 `agent.messages = agent.messages[:idx]`。两种失败模式：(a) `_summarize_messages`（agent.py:223）把整个列表替换成 `[system, user1, summary1, ...]`，而 `_cleanup_incomplete_messages`（agent.py:86）在按 Esc 时截断 —— 任一发生之后，每一个存下来的索引都指向毫无意义的位置，rewind 于是悄悄地把文件恢复到一个毫不相干的上下文之上；(b) 如果检查点是在同一条 assistant 消息的若干工具调用之间拍的，在那里截断就会留下一条带 3 个 `tool_calls` 却只有 1 个 `tool` 结果的 assistant 消息，两家 provider 都会以 400 拒绝。正确处理：快照消息列表本身（gzip 过，约 60 KB），只在 pre-assistant-message 边界（`self._step_boundary`，agent.py:376）取检查点，并在装载前断言 `_is_boundary()`。同时重置 `api_total_tokens`（agent.py:53）和 `_skip_next_token_check`（agent.py:55）—— 否则一份刚刚 rewind 出来的 3 条消息的历史会立刻触发压缩，因为来自那段长历史的 API token 计数还杵在那里。

6. **agent 自己也在跑 git。** 三个子情况。(a) 环境泄漏：`bash_tool.py:391` 调用 `create_subprocess_shell(..., cwd=...)` 且没有 `env=`，所以子进程继承 `os.environ`。一个朴素实现在启动时设置一次 `os.environ['GIT_DIR']`，于是 agent 自己的 `git status` 报告的是 shadow store，它的 `git commit` 也写进了 shadow store。正确处理：按调用逐次构造 env 字典，只通过 `subprocess.run(env=...)` 传递；绝不碰 `os.environ`。(b) agent 在真实仓库里跑 `git reset --hard` 或 `git clean -xdf`：我们下一次 capture 只是看到文件在变，rewind 就把它们恢复了 —— 这严格*优于* git 本身，git 无法恢复被 `reset --hard` 摧毁的未提交工作。(c) agent 跑 `git commit`：文件内容可能没变，所以没什么可快照的，但真实的 `HEAD` 移动了。正确处理：在每个检查点里记录 `real_head`（一次只读的 `git -C <ws> rev-parse HEAD`），rewind 时如果它移动过，就**打印** `git reset --soft <old_sha>` 并拒绝执行它。一条值得在面试里说出来的硬不变量：*检查点存储对用户仓库执行零次写入 —— 不写 `.git/index`，不写 refs，不写 stash。*

## 怎么证明它有效

两份工件，计算耗时都在 10 秒以内，不需要 API key（demo 直接驱动 `CheckpointStore` 和一个桩 `Agent`，所以它不依赖 `tests/test_agent.py` 里那种打真实 API 的测试风格 —— 那里有 2 个测试函数、0 个断言）。

**1. `tests/test_checkpoint.py`（pytest，约 180 LOC）。** 搭一个 fixture workspace，里面包含：一个真的 `git init` 过、有一次提交的仓库；一个列了 `.env` 和 `*.log` 的 `.gitignore`；一个含 `* text=auto` 的 `.gitattributes`；一个 CRLF 文件；一个 200 字节的二进制文件；一个符号链接；一个可执行脚本；以及一个嵌套仓库 `sub/`。辅助函数 `fingerprint(ws) -> str` = 对每个文件的 `(relpath, st_mode & 0o777, is_symlink, sha256(bytes))` 排序后取 sha256，跳过 `.git/` 和 `sub/`。

断言：
- `capture` → 把一切都改掉（重写 CRLF 文件、`rm` 一个 tracked 文件、创建 `new/deep.py`、`chmod -x`、删掉符号链接、通过*文件工具*路径写那个被 gitignore 的 `.env`）→ `restore` → `fingerprint` 与之前完全一致。这一条断言就覆盖了 CRLF/LFS 中和、可执行位、符号链接和复活。
- restore 之后 `new/deep.py` 不再存在（untracked 删除语义）。
- 一个在检查点之后创建、且**从未**被捕获的文件（`build/out.o`，被 glob 排除）*仍然*存在，并列在 `report.left_alone` 里。
- `sub/nested.txt` 未被触碰，且检查点记录把 `sub` 列为嵌套仓库。
- **最值钱的那条断言：** 一次完整 capture+restore 循环前后，`sha256(<ws>/.git/index)`、`git -C <ws> rev-parse HEAD` 和 `git -C <ws> status --porcelain` 三者逐字节一致 → 可证明 shadow store 从未写过用户的仓库。
- Rewind 一致性：构造一份 12 条、结尾停在批次中间的假消息列表，断言 `_is_boundary` 拒绝它；在一份脚本化的 3 轮 transcript 上跑真实的钩子路径，`rewind(n=2)`，断言 `len(agent.messages)` 与快照一致、`_is_boundary(agent.messages)` 为真、且 `agent.api_total_tokens == 0`。
- 压缩存活性：capture，然后跑一次 `_summarize_messages` 式的破坏（`agent.messages = [agent.messages[0]]`），再 rewind —— 断言完整的压缩前历史回来了。这就是基于索引的实现过不去的那条断言。

运行：`pytest tests/test_checkpoint.py -q` → 期望 `12 passed in ~3s`。

**2. `scripts/demo_checkpoint.py`** —— 打印你真正拿给面试官看的那份工件：
```
turn 1  pre-write_file   3 files    27 ms   sha 4a1c9e2
turn 2  pre-bash        14 files    31 ms   sha b7730f1
  $ rm -rf src && git reset --hard HEAD~1      <- destructive, uncommitted work lost
turn 3  turn-end         0 files    18 ms   (tree unchanged, no new commit)

/diff 2
 src/app.py    | 12 ++++++------
 src/util.py   |  3 +++

/rewind 2
 restored 14  deleted 2  left_alone 1 (build/out.o, excluded)
 conversation: 41 -> 18 messages (boundary OK)
 warning: real HEAD moved 953b943 -> 24812a3 since this checkpoint.
          run `git reset --soft 953b943` yourself if you want it back.
 tree fingerprint: MATCH
```
再加上它现场测出来的基准表：在 6 232 文件的 workspace 上冷跑 `status`（实测 19 ms）、定向 capture（约 27 ms），以及朴素 `git add -A -f .` 的对照（4.80 s / 42 MB），这样这个设计选择是用数字而不是观点来辩护的。

## 深度追问

1. **"为什么存整个消息列表而不是它的一个索引？"** 因为这个列表不稳定。agent.py:223 的 `_summarize_messages` 从头重建 `self.messages`，agent.py:86 的 `_cleanup_incomplete_messages` 在按 Esc 时截断它，cli.py:676 的 `/clear` 清空它。索引一个都扛不住，而且失败是静默的 —— 你把文件恢复到了一份已经不再描述它们的 transcript 之上。被否掉的替代方案：在 `Message`（schema.py:29）上加一个单调递增的 `seq` 字段，做 sha→seq 映射。这在字节上更便宜，但加了一个 Anthropic/OpenAI 序列化器都得被教会剥掉的字段，而且它仍然重建不了被压缩*销毁*掉的消息。一份 gzip 快照对 80k token 的历史约 60 KB —— 检查点自身的 git 对象通常还更贵。第二个被否掉的替代方案：把 transcript 作为 `.mini-agent/messages.json` 快照进 git tree；否掉的原因是 `read-tree -u` 会把它落进用户的工作树里。

2. **"你到底在哪里拍快照，为什么在那里？"** 惰性地，在一个 step 的第一次会改动文件的工具调用处（agent.py:436），并打上在 assistant 消息被 append *之前*捕获的那个消息边界（agent.py:376）。由此掉出两条性质。(1) 它是 pre-mutation 的，而且当这个 step 不改动任何东西时它是免费的 —— 一个只调用 `read_file` 的 step 从不产生子进程。(2) 这个边界按构造就是 tool-call-complete 的，所以 `_is_boundary()` 永远不可能失败。那个诱人的替代方案 —— 在每一次单独的工具调用前拍快照以获得更细粒度 —— 是错的：一个 3 次调用的批次里、在第 2 次和第 3 次之间的快照，没有任何有效的 transcript 可以与之配对；恢复它会逼你要么捏造合成的 tool result，要么截断到一个和文件*不同*的点，于是文件与上下文互相矛盾，而这正是这个特性存在的意义所要防的那个 bug。我会当作不变量说出来的规则是：**一个检查点里的文件状态与消息状态是在同一瞬间捕获的，而且只会被一起装载。** 批次级粒度是能做到这一点的最细粒度。

3. **"shadow git 仓库真的是对的原语吗，比起对被碰过的文件做 copy-on-write？"** copy-on-write 只要 60 LOC，而且在三条轴上确实更好：不用全树扫描，不依赖 `git` 二进制，每个文件有精确的来源记录。它在五条轴上输。(1) 它看不见 `bash` 造成的改动 —— 你只知道文件工具点名过的路径，而 `bash` 正是 `rm -rf`、codemod 和 `git reset --hard` 的所在地；要抓住这些你就得遍历并哈希整棵树，而那正是 git 用一个 stat 缓存过的 index 已经在做的事（实测 19 ms）。(2) 没有内容去重：十轮编辑一个 5 MB 文件就是 50 MB；git 对每份不同内容只存一个 zlib blob —— 一个 60 MB 的全零文件落地是 384 KB。(3) 删除和创建需要手搓 tombstone，而用 `mv` 完成的目录重命名是不可见的。(4) 你拿不到 diff 渲染；有了 commit，`git diff --stat <a> <b>` 是免费的，而且对重命名和二进制文件的处理已经是正确的。(5) 整树带删除的恢复就是一条 `read-tree -u --reset`。我会换掉它的场合：一个 `status` 遍历要跑一两秒的 monorepo，或者需要回滚进程/数据库的场合 —— 那时正确的原语是文件系统快照（APFS `clonefile`、btrfs/ZFS subvolume）或者一层 overlayfs upper layer，也就是由 sandbox 而不是 agent 来拥有回滚。shadow git 是"单一 workspace、数万文件、需要 diff"这个区间的甜点。

4. **"这和 aider 的自动提交、和 Claude Code 的检查点有什么不同？"** aider 提交进*用户的真实仓库*，消息带 `aider:` 前缀，`/undo` 对它自己最后一个提交做 `git reset --hard`（如果 HEAD 不是 aider 的就拒绝）。取舍：用户能在普通 `git log` 里看见检查点，而且它们作为真实历史能扛过重启 —— 这是实打实的优势 —— 但它污染历史和 `git blame`，需要配置好的 `user.email`，除非抑制否则会触发用户的 hooks，无法捕获 gitignore 的文件，在非 git 的 workspace 里完全没法用（Mini-Agent 的默认 `./workspace` 恰恰就是这种），还会和用户自己的提交与暂存 index 竞争。Claude Code 的检查点走的是另一头：放在仓库外面，只管*它自己*编辑过的文件，`/rewind` 提供 code-only / conversation-only / both 三种模式 —— 而且它明确**不**撤销 bash 的副作用。我的设计坐在两者之间：像 Claude Code 一样在仓库外（对用户的 `.git` 零写入 —— 在测试里断言了），但像 aider 一样做全树 diff，因此 `bash` 造成的改动*确实*被捕获，代价是每个会改动的 step 付一次 19 ms 的 `status` 遍历。我保留三种 rewind 模式，因为 conversation-only 是真的有用 —— "代码留着，带个提示重跑一遍推理"。

5. **"这个特性撤销不了什么，你怎么把这件事说诚实？"** 任何不是工作树里的文件的东西：写进数据库的行、已经发出去的 HTTP 请求、装进一个在排除列表上的 venv 里的包、来自 `bash_tool.py:354` 且仍在运行的后台进程（它的 `BackgroundShellManager._shells` 会原封不动地扛过一次 rewind，而且可能还在往你刚刚恢复的文件里写）、用户真实的 `HEAD` 和暂存 index、嵌套仓库的内容，以及文件 mtime（被恢复的文件拿到全新 mtime，所以构建系统会重新构建 —— 通常没问题，偶尔对 `make` 是承重的）。让它保持诚实的设计决策是：`restore()` 返回一个带 `restored / deleted / left_alone` 的 `RestoreReport`，而且这个存储*绝不*删除自己从未捕获过的路径，因此失败模式永远是"撤销得比你预期的少"，而不是"你在乎的东西消失了"。UI 打印这份报告和 `real_head` 警告，而不是声称成功。具体地，我还会在有任何后台 shell 存活时阻止 rewind，或者至少带上 `bash_id` 发出警告。

6. **"rewind 自身就是破坏性的 —— 有人误 rewind 了怎么办？"** `rewind()` 做的第一件事就是 `capture(label='pre-rewind')`，所以 rewind 前的状态是一个普通检查点，再来一次 `/rewind 1` 就回到它 —— 检查点的历史是只追加的，从不截断。这是与 `git reset --hard` 的一个刻意区别：那里被摧毁的状态是未提交的，因而连 reflog 都够不着。这也意味着这个存储是一条线性日志，不是一棵树：rewind 到检查点 3 再继续，会产生检查点 9、10、11，它们的父提交是 8，于是 `git log --graph refs/mini-agent/cp/*` 免费显示出分支结构，而 `index.jsonl` 保持为一个扁平的时间序列表 —— 而这正是 `/rewind n` 应该去数的那个顺序，因为用户数的是墙钟意义上的"几件事之前"，不是 DAG 上的祖先关系。

## 前置条件

1. `mini_agent/agent.py:390-394`、`:399-401`、`:480-481`、`:490-492` —— `run()` 从四个不同的地方返回，所以 `turn-end` capture 要么复制四遍，要么把循环体包进 `try/finally`。先做 `try/finally`；大约 10 行，后面每一个钩子都依赖它。

2. `mini_agent/cli.py:673-678` —— `/clear` 设置 `agent.messages = [agent.messages[0]]`，没有碰任何轮次计数器（现在也还没有）。检查点钩子引入的任何轮次计数器都必须在这里重置，否则检查点记录会指向一份已经被丢掉的 transcript。

3. `mini_agent/tools/bash_tool.py:354` 和 `:391` —— `create_subprocess_shell` 不传 `env=`，所以子进程继承 `os.environ`。这不是一个要修的 bug，而是一条要遵守的约束：它意味着检查点存储绝不能把 GIT_DIR / GIT_INDEX_FILE / GIT_CONFIG_GLOBAL 写进 `os.environ`。验证方式是在 `checkpoint.py` 里 grep `os.environ[` —— 除了 `{**os.environ, ...}` 之外应该零命中。

## 明确不做

不做：跨会话持久化与恢复（检查点随会话目录一起消亡）、分支式检查点*树*的 UI（DAG 确实存在于 object store 里，但 `/rewind n` 按墙钟顺序线性计数）、部分/hunk 级 rewind、恢复用户真实的 `HEAD`/index/stash（是刻意拒绝，而非仅仅未实现）、非文件系统副作用的回滚、嵌套仓库内容、同一 workspace 上两个 agent 之间的加锁、Windows，以及除会话结束时对 `keep=200` 之外检查点做一次 `prune` 之外的任何后台 `git gc`。对面试官：「我做的是机制，不是产品 —— 有意思的决策全在于捕获什么、以及怎么让文件状态和消息状态不互相矛盾，被砍掉的项目没有一件会改变这两点。我最愿意为之辩护的那一刀是拒绝恢复用户真实的 `HEAD`：这个存储相对他们的仓库是只读的，我宁可打印 `git reset --soft <sha>` 让他们自己去跑，也不要一个 agent 悄悄改写他们的历史。」

## 代码量

总计约 700 LOC：`mini_agent/checkpoint.py` 约 300（store 190，rewind + 边界检查 45，diff/list/gc 65）；`agent.py` 钩子约 25，分布在 5 处；`cli.py` 约 70（3 个斜杠命令 + help + 接线）；`config.py` 约 12；`tests/test_checkpoint.py` 约 180；`scripts/demo_checkpoint.py` 约 120。

## 工期

2.5-3 天。第 1 天：在一个 scratch workspace 上做 `checkpoint.py` 的 capture/restore，完全由脚本驱动 —— `info/attributes`、pathspec-exclusion 对 `-f`、以及 `read-tree` index 同步这几个发现都在这里钉死。第 2 天：agent/cli 钩子、rewind 边界逻辑，以及那三个斜杠命令。第 3 天（半天）：fingerprint 测试套件和带计时表的 demo 脚本。
