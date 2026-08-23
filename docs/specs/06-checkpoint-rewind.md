# 每轮检查点与 rewind（shadow git）

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Per-turn checkpoint & rewind (shadow-git snapshot store) — `mini_agent/checkpoint.py``


## 一句话

Before the first mutating tool call of each agent step, snapshot the whole workspace into a private git object store (own GIT_DIR + own GIT_INDEX_FILE + own attributes, never the user's `.git`), record `commit_sha -> (turn, step, message-list snapshot)`, and make `/rewind n` restore the files with `read-tree -u --reset` **and** replace the message history with the snapshot taken at the same instant, so context and filesystem can never disagree.

## 为什么这是难点

An agent loop is a state machine with two states that must stay in sync: the model's belief about the repo (its message history, which says "Successfully wrote to app.py") and the repo itself. Every other agent feature assumes they agree. The moment a user says "no, go back", a naive implementation breaks the agreement in one of two directions: restore files but keep the transcript (the model now confidently edits functions that no longer exist, and its next `edit_file` fails with "Text not found" — an infinite repair loop), or truncate the transcript but keep the files (the model re-writes edits that are already applied, producing doubled code). This is why rewind is the feature that exposes whether someone actually understands the loop.

It is also the only credible answer to autonomy. `max_steps=50` (agent.py:35) with an unrestricted `bash` tool (bash_tool.py:391 `create_subprocess_shell`) means the agent can `git reset --hard`, `rm -rf`, or run a codemod across 200 files with no undo. Git's own reflog cannot save uncommitted work; the user's editor's undo stack cannot span 40 tool calls. Without a checkpoint layer, the safe operating mode is "watch every diff", which defeats the point of an agent.

Finally it is a *mechanism* problem, not a plumbing problem: content-addressed dedup, index stat-caching, attribute neutralization, and the tracked/ignored boundary all have to be reasoned about, and every one of them has an obvious-but-wrong implementation.

## 仓库现状

There is no checkpoint, undo, or snapshot code anywhere. `grep -rn "checkpoint\|snapshot\|rewind" mini_agent/*.py mini_agent/tools/*.py` returns nothing.

Mutation sites, all unguarded:
- `mini_agent/tools/file_tools.py:206` — `WriteTool.execute`: `file_path.write_text(content, encoding="utf-8")` after `file_path.parent.mkdir(parents=True, exist_ok=True)` (line 204). Full overwrite, no read-before-write check, no backup, and no record of which path was touched.
- `mini_agent/tools/file_tools.py:280-281` — `EditTool.execute`: `content.replace(old_str, new_str)` then `write_text`. Note it uses `str.replace` with no count, so a non-unique `old_str` silently rewrites every occurrence despite the docstring at line 230 claiming uniqueness is required. Prior content is unrecoverable.
- `mini_agent/tools/bash_tool.py:391` (foreground) and `:354` (background) — `asyncio.create_subprocess_shell(command, cwd=self.workspace_dir)` with no `env=`, so the child inherits `os.environ` wholesale. Arbitrary mutation, arbitrary paths, and *any* env var we set globally would leak into the agent's own `git` invocations.

Loop sites:
- `mini_agent/agent.py:404` — `for tool_call in response.tool_calls:` begins the batch; `agent.py:436` `result = await tool.execute(**arguments)` is the single dispatch point for every tool; `agent.py:468-474` appends the `tool` result message. There is exactly one place to hook a pre-mutation snapshot (line 436) and it is already inside a `try` (line 434).
- `mini_agent/agent.py:376` — `self.messages.append(assistant_msg)`; the index immediately *before* this append is the only tool-call-complete boundary for the step.
- `mini_agent/agent.py:53` `self.api_total_tokens` and `agent.py:55` `self._skip_next_token_check` are loop state that a rewind must reset, or a rewound (short) history still triggers compaction on the next step.

History-rewriting hazards a rewind design must survive:
- `mini_agent/agent.py:153-233` `_summarize_messages` **replaces** `self.messages` with `[system] + [user_i, summary_i...]` (line 223 `self.messages = new_messages`). Any checkpoint stored as an integer index into the message list is silently invalidated the first time compaction fires.
- `mini_agent/agent.py:70-90` `_cleanup_incomplete_messages` truncates to `last_assistant_idx` on Esc — again shifting indices.
- `mini_agent/cli.py:676` `/clear` does `agent.messages = [agent.messages[0]]`.

Turn plumbing: `cli.py:715` `agent.add_user_message(user_input)` then `cli.py:775` `asyncio.create_task(agent.run())`; slash commands are dispatched in the if/elif chain `cli.py:661-703` with the unknown-command fallback at `cli.py:700`; the completer word list is `cli.py:600`. `Agent` is constructed at `cli.py:569-575`. There is no session id anywhere in the codebase (`grep -n "session_id" mini_agent/*.py` → only `acp/__init__.py:96`, which builds `f"sess-{len(self._sessions)}-{uuid4().hex[:8]}"`). `AgentLogger` already owns the `~/.mini-agent/` convention (`logger.py:25`).

Two things make the workspace hostile in practice: the default workspace is `./workspace` (config.py:36) which is itself listed in this repo's `.gitignore`, and this repo contains a real submodule (`.gitmodules` → `mini_agent/skills`), so both the "gitignored subtree" and "nested repo" cases are live on day one.

## 最小实现

## New file: `mini_agent/checkpoint.py` (~300 LOC, stdlib only — no new dependency)

### Layout on disk
```
~/.mini-agent/checkpoints/<session_id>/
    git/                 # GIT_DIR (bare-ish, --template= so no hooks)
    git/info/attributes  # "* -text -filter -merge"
    index                # GIT_INDEX_FILE (persistent stat cache -> warm captures ~20ms)
    index.jsonl          # append-only checkpoint records
    messages/<id>.json.gz# message-list snapshot per checkpoint
```
`session_id = f"{ws_slug}-{time:%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"`, created in `cli.run_agent`.

### Data structures
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

### The git invocation (this is the whole isolation story)
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
`_env()` is passed as `env=` to `subprocess.run` **only**. Nothing is ever written into `os.environ` — see edge case 4.

`init()`:
```
git init -q --bare --template= <gitdir>      # verified: yields only HEAD/config/objects/refs, zero hooks
write <gitdir>/info/attributes  = "* -text -filter -merge\n"
detect nested repos: any dir with a .git entry, depth<=4 -> warn once, they become gitlinks
refuse if ws in (Path.home(), Path("/")) or ws has >200k entries
```

### capture() — two-phase, targeted
```python
def capture(self, *, messages: list[Message], turn: int, step: int,
            label: str, forced_paths: Iterable[str] = ()) -> Checkpoint | None:
```
1. `git status --porcelain=v1 -z -uall --no-renames` → parse `XY<space>path\0` records. This respects the workspace `.gitignore`, costs **19 ms cold on this 6 232-file repo** (measured; ignored dirs collapse to one entry), and is stat-cache-warm afterwards.
2. Union with `forced_paths` (paths the file tools reported this step) and with anything already tracked in the shadow index.
3. Drop paths matching `exclude` globs (`.venv/**`, `node_modules/**`, `.git/**`, `**/__pycache__/**`, `*.pyc`) and paths whose `st_size > max_file_bytes` (default 5 MB) → recorded in `skipped_large`.
4. If the surviving set is empty → return a record reusing the previous `sha` (no new commit object). Cheap no-op for `bash: ls`.
5. Force-add exactly that set, NUL-delimited on stdin so filenames with spaces/quotes/`$` survive:
   `git add -f --ignore-errors -A --pathspec-from-file=- --pathspec-file-nul` (verified with a file literally named ``we ird'$file.py``; the same call correctly records deletions as `AD`/` D`).
6. `T = git write-tree`; `C = git -c user.email=mini-agent@localhost -c user.name=mini-agent commit-tree T [-p prev] -m body` where `body` embeds `turn/step/msgidx/label` so the mapping is recoverable from the object store alone; `git update-ref refs/mini-agent/cp/<id> C`.
7. `gzip.open(msg_path,"wt").write(json.dumps([m.model_dump(exclude_none=True) for m in messages]))`.
8. Append the record to `index.jsonl`.

Plumbing (`write-tree`/`commit-tree`/`update-ref`) rather than `git commit` on purpose: no hooks, no `user.name` requirement, no GPG-signing prompt, no `.gitmessage` template.

### restore()
```python
def restore(self, cp: Checkpoint) -> RestoreReport:
    self._sync_index()                       # git add -f -A <same targeted pathspec set>
    before = self._git("diff","--name-status", cp.sha)   # for the report
    self._git("read-tree","-u","--reset", cp.sha)
    ...
```
The `_sync_index()` first line is load-bearing: `read-tree -u --reset` only removes worktree files that are **in the index** and absent from the target tree. Verified — with the index synced, a file created after the checkpoint and a whole directory created after it were both deleted, `a.txt` (deleted since) was resurrected, and a modified file's content came back byte-identical.

### rewind() — the part that has to be atomic in spirit
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
Storing the **message list**, not an index, is what makes this survive `_summarize_messages` (agent.py:223) and `_cleanup_incomplete_messages` (agent.py:86). A gzipped 80k-token history is ~60 KB.

### diff() — free, because the snapshots are commits
```python
def diff(self, a: Checkpoint, b: Checkpoint | None = None, mode="stat") -> str
#  stat  -> git diff --stat  a.sha b.sha
#  patch -> git diff -U3     a.sha b.sha
#  live  -> git diff --stat  a.sha            (checkpoint vs current worktree)
```

## Exact edits to existing files

- **`agent.py:1-16`** add `from .checkpoint import CheckpointStore, MUTATING_TOOLS`.
- **`agent.py:22-31`** (`__init__` signature) add `checkpointer: "CheckpointStore | None" = None`.
- **`agent.py:53-55`** (end of `__init__`) add `self.checkpointer = checkpointer`, `self._turn_index = 0`, `self._step_boundary = 0`, `self._captured_this_step = False`, `self._touched_paths: set[str] = set()`.
- **`agent.py:58-61`** (`add_user_message`) after the append: bump `self._turn_index = len(self.messages) - 1` and, if `self.checkpointer`, `self.checkpointer.capture(messages=self.messages, turn=self._turn_index, step=-1, label="turn-start")`. This turn-start capture is what makes user/editor edits made *between* turns survive a later rewind.
- **`agent.py:376`** immediately **before** `self.messages.append(assistant_msg)` insert `self._step_boundary = len(self.messages); self._captured_this_step = False; self._touched_paths.clear()`.
- **`agent.py:433-436`** replace the `tool = self.tools[function_name]; result = await tool.execute(**arguments)` pair with:
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
  Note the snapshot is `self.messages[:self._step_boundary]` — the history *without* the assistant message that ordered the edit. Restoring it rolls the model back to the decision point, and it is a boundary by construction.
- **`agent.py:390-394` / `:399-401` / `:480-481` / `:490-492`** — `run()` has four exit paths. Wrap the `while` in `try/finally` and put one `self.checkpointer.capture(..., label="turn-end", forced_paths=self._touched_paths)` in the `finally`, so the last turn's diff has a closing snapshot on normal return, cancellation, and max-steps alike.
- **`cli.py:569-575`** build the store and pass it:
```python
store = CheckpointStore(workspace_dir, session_id, cfg=config.agent.checkpoints) if config.agent.checkpoints.enabled else None
if store: store.init()
agent = Agent(..., checkpointer=store)
```
- **`cli.py:600`** add `"/rewind", "/diff", "/checkpoints"` to the `WordCompleter` list.
- **`cli.py:699`** (just before the `else:` unknown-command branch at `cli.py:700`) add three `elif` branches: `/checkpoints` prints `store.list()` as `#id  turn/step  label  +a/-d  age`; `/diff [n]` prints `store.diff(cp[n-1], cp[n])`; `/rewind [n] [--code|--conversation]` resolves `n` (default 1 = most recent) and calls `rewind(agent, store, cp, mode)` then prints the `RestoreReport`.
- **`cli.py:673-678`** (`/clear`) also reset `agent._turn_index = 0` and capture a `turn-start`, otherwise the checkpoint list references a history that no longer exists.
- **`config.py:32-37`** add to `AgentConfig`: `checkpoints: CheckpointConfig = Field(default_factory=CheckpointConfig)` with `enabled: bool = True`, `max_file_bytes: int = 5_000_000`, `keep: int = 200`, `exclude: list[str] = [...]`.
- **`cli.py:156-186`** (`print_help`) add the three commands.

`mini_agent/acp/__init__.py:127-165` is a drifted duplicate of the loop — explicitly out of scope; leave it unhooked and say so.

## Measured performance (this repo, git 2.55.0, macOS/APFS)
| operation | cost |
|---|---|
| `status --porcelain -z -uall`, cold, empty index, 6 232 files incl. `.venv` | **19 ms** |
| targeted `add -f` of 3 changed files + `write-tree` + `commit-tree` | **~27 ms** |
| blanket `git add -A -f .` (the naive design), cold | **4.80 s**, 42 MB of objects |
| blanket, warm (stat cache hit) | 37 ms |
| 60 MB file of zeros → object store | 384 KB (zlib), 0.36 s |

## 边界情况

1. **In-tree `.gitattributes` silently corrupts the round trip.** Obvious-and-wrong: assume `-c core.autocrlf=false` (and even `-c core.attributesFile=/dev/null`) makes the store byte-faithful. Measured: with `* text=auto` in the workspace, a CRLF file checkpointed and restored comes back LF (md5 59b0d7… → dd8c6a…) — a silent rewrite of every line ending in the user's tree, and with `*.dat filter=lfs` the shadow store would invoke the LFS clean filter. `core.attributesFile` loses because in-tree `.gitattributes` outranks it. Right handling: write `* -text -filter -merge` into `$GIT_DIR/info/attributes`, which outranks in-tree attributes — verified byte-identical for both the CRLF file and the LFS-marked file. Deliberately do **not** add `-diff`: it also neutralizes attributes, but it makes `git diff` report every file as binary (`-	-	crlf.txt`), killing the free per-turn diffs.

2. **Restoring with the wrong plumbing leaves the agent's new files behind.** Obvious-and-wrong: `git checkout-index -a -f` or `git checkout <sha> -- .` — both write the checkpointed files back but cannot know about a file the agent created *after* the checkpoint, so the rewind silently leaves `broken_helper.py` on disk. Right handling: `git add -f -A` the current state into the persistent index *first*, then `git read-tree -u --reset <sha>`. Verified: with the index synced, `brandnew.txt` and a whole new `d2/` directory were removed, a deleted file was resurrected, and a modified file's content restored. The invariant is that `read-tree -u` only deletes what the index knows about — which is exactly the property that makes the store safe: **it never deletes a file it never captured** (ignored, excluded, or oversize files are left in place and reported in `left_alone`).

3. **Blanket `-f` is the difference between a 27 ms feature and a 5 s one.** Obvious-and-wrong: `git add -A -f .` so gitignored files are covered. On this very repo that hashes `.venv` — 4.80 s and 42 MB on the first turn — and it sweeps `.env` secrets and build outputs into a store the user never audited. Obvious-and-also-wrong: drop `-f` entirely, and then a `.gitignore`d workspace (Mini-Agent's own default workspace, `./workspace`, is gitignored in this repo) checkpoints nothing at all. Right handling: `-f` applied only to an explicit path list — paths the file tools reported touching this step, plus everything already tracked in the shadow index — plus `':(exclude,glob).venv/**'` pathspec magic (verified: exclusion magic still wins over `-f`) and a `max_file_bytes` filter. The resulting rule is clean and explainable: *ignored files enter the store by being touched once; tracked-ness is then sticky.* Consequence to state out loud: a gitignored file mutated only by `bash` is never captured, because `git status` won't report it and the tool didn't name it.

4. **Nested repositories become gitlinks and are not snapshotted.** Obvious-and-wrong: assume a whole-tree `add` is whole-tree. Verified: `git add -A` on a work tree containing `sub/.git` records `160000 <sha> sub` and prints "adding embedded git repository"; contents are not stored, and forcing it fails outright — `fatal: Pathspec 'sub/nested.txt' is in submodule 'sub'`. This repo ships `.gitmodules` → `mini_agent/skills`, so the demo hits it immediately. Right handling: scan for nested `.git` at init, warn once, exclude those subtrees from both capture and restore (they have their own history and their own reflog), and make `/rewind` say "2 nested repos not covered: mini_agent/skills". The work tree's *own* top-level `.git` is skipped by git automatically — verified, it never appears in `ls-files`.

5. **Truncating the transcript by index.** Obvious-and-wrong: store `message_index` and do `agent.messages = agent.messages[:idx]`. Two failure modes: (a) `_summarize_messages` (agent.py:223) replaces the entire list with `[system, user1, summary1, ...]`, and `_cleanup_incomplete_messages` (agent.py:86) truncates on Esc — after either, every stored index points somewhere meaningless, and the rewind quietly restores files against an unrelated context; (b) if the checkpoint were taken between tool calls of one assistant message, truncating there leaves an assistant message with 3 `tool_calls` and 1 `tool` result, which both providers reject with a 400. Right handling: snapshot the message list itself (gzipped, ~60 KB), take checkpoints only at the pre-assistant-message boundary (`self._step_boundary`, agent.py:376), and assert `_is_boundary()` before installing it. Also reset `api_total_tokens` (agent.py:53) and `_skip_next_token_check` (agent.py:55) — otherwise a freshly-rewound 3-message history immediately triggers compaction because the API token count from the long history is still sitting there.

6. **The agent runs git itself.** Three sub-cases. (a) Env leakage: `bash_tool.py:391` calls `create_subprocess_shell(..., cwd=...)` with no `env=`, so the child inherits `os.environ`. A naive implementation sets `os.environ['GIT_DIR']` once at startup, and then the agent's own `git status` reports on the shadow store and its `git commit` writes into it. Right handling: build the env dict per-call and pass it only via `subprocess.run(env=...)`; never touch `os.environ`. (b) The agent runs `git reset --hard` or `git clean -xdf` in the real repo: our next capture just sees files changing, and rewind restores them — this is strictly *better* than git, which cannot recover uncommitted work destroyed by `reset --hard`. (c) The agent runs `git commit`: file contents may be unchanged, so there's nothing to snapshot, but the real `HEAD` moved. Right handling: record `real_head` (a read-only `git -C <ws> rev-parse HEAD`) in every checkpoint, and on rewind, if it moved, **print** `git reset --soft <old_sha>` and refuse to run it. Hard invariant, worth stating in the interview: *the checkpoint store performs zero writes to the user's repository — not to `.git/index`, not to refs, not to the stash.*

## 怎么证明它有效

Two artifacts, both under 10 seconds of compute, no API key needed (the demo drives `CheckpointStore` + a stub `Agent` directly, so it does not depend on the live-API test style used in `tests/test_agent.py`, which has 2 test functions and 0 asserts).

**1. `tests/test_checkpoint.py` (pytest, ~180 LOC).** Builds a fixture workspace containing: a real `git init`'d repo with one commit; a `.gitignore` listing `.env` and `*.log`; a `.gitattributes` with `* text=auto`; a CRLF file; a 200-byte binary; a symlink; an executable script; and a nested repo `sub/`. Helper `fingerprint(ws) -> str` = sha256 over sorted `(relpath, st_mode & 0o777, is_symlink, sha256(bytes))` for every file, skipping `.git/` and `sub/`.

Assertions:
- `capture` → mutate everything (rewrite CRLF file, `rm` a tracked file, create `new/deep.py`, `chmod -x`, delete the symlink, write the gitignored `.env` through the *file tool* path) → `restore` → `fingerprint` is identical to before. This single assert covers CRLF/LFS neutralization, exec bit, symlinks, and resurrection.
- `new/deep.py` no longer exists after restore (untracked-deletion semantics).
- A file created after the checkpoint that was **never** captured (`build/out.o`, excluded by glob) *does* still exist, and is listed in `report.left_alone`.
- `sub/nested.txt` is untouched, and the checkpoint record lists `sub` as a nested repo.
- **The money assert:** `sha256(<ws>/.git/index)`, `git -C <ws> rev-parse HEAD`, and `git -C <ws> status --porcelain` are all byte-identical before and after a full capture+restore cycle → the shadow store provably never wrote to the user's repo.
- Rewind consistency: build a fake message list of 12 messages ending mid-batch, assert `_is_boundary` rejects it; run the real hook path over a scripted 3-turn transcript, `rewind(n=2)`, assert `len(agent.messages)` matches the snapshot, `_is_boundary(agent.messages)` is true, and `agent.api_total_tokens == 0`.
- Compaction survival: capture, then run `_summarize_messages`-style destruction (`agent.messages = [agent.messages[0]]`), then rewind — assert the full pre-compaction history comes back. This is the assert an index-based implementation cannot pass.

Run: `pytest tests/test_checkpoint.py -q` → expect `12 passed in ~3s`.

**2. `scripts/demo_checkpoint.py`** — prints the artifact you actually show an interviewer:
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
plus the benchmark table it measures live: cold `status` on the 6 232-file workspace (19 ms measured), targeted capture (~27 ms), and the naive `git add -A -f .` comparison (4.80 s / 42 MB) so the design choice is defended with a number rather than an opinion.

## 深度追问

1. **"Why store the whole message list instead of an index into it?"** Because the list is not stable. `_summarize_messages` at agent.py:223 rebuilds `self.messages` from scratch, `_cleanup_incomplete_messages` at agent.py:86 truncates it on Esc, and `/clear` at cli.py:676 empties it. An index survives none of those, and the failure is silent — you restore files against a transcript that no longer describes them. Rejected alternative: put a monotonic `seq` field on `Message` (schema.py:29) and map sha→seq. That's cheaper in bytes but adds a field that the Anthropic/OpenAI serializers would have to be taught to strip, and it still can't reconstruct messages that compaction *destroyed*. A gzipped snapshot is ~60 KB for an 80k-token history — the checkpoint's own git objects usually cost more. Second rejected alternative: snapshot the transcript into the git tree as `.mini-agent/messages.json`; rejected because `read-tree -u` would then materialize it into the user's working tree.

2. **"Where exactly do you take the snapshot, and why there?"** Lazily, at the first mutating tool call of a step (agent.py:436), tagged with the message boundary captured *before* the assistant message was appended (agent.py:376). Two properties fall out. (1) It's pre-mutation and it's free when the step doesn't mutate — a step that only calls `read_file` never spawns a subprocess. (2) The boundary is tool-call-complete by construction, so `_is_boundary()` can never fail. The tempting alternative — snapshot before every individual tool call for finer granularity — is wrong: a snapshot between call 2 and call 3 of a 3-call batch has no valid transcript to pair with; restoring it forces you to either invent synthetic tool results or truncate to a *different* point than the files, and now files and context disagree, which is the exact bug the feature exists to prevent. The rule I'd state as an invariant: **the file state and the message state in one checkpoint are captured at the same instant and are only ever installed together.** Batch-level granularity is the finest granularity at which that is possible.

3. **"Is a shadow git repo even the right primitive, versus copy-on-write of touched files?"** Copy-on-write is 60 LOC and genuinely better on three axes: no full-tree scan, no dependency on the `git` binary, exact per-file provenance. It loses on five. (1) It cannot see `bash` mutations — you only know the paths the file tools named, and `bash` is where `rm -rf`, codemods, and `git reset --hard` live; to catch those you'd have to walk and hash the tree, which is what git already does with a stat-cached index (19 ms measured). (2) No content dedup: ten turns editing a 5 MB file cost 50 MB; git stores one zlib blob per distinct content — a 60 MB file of zeros landed as 384 KB. (3) Deletions and creations need hand-rolled tombstones, and directory renames done by `mv` are invisible. (4) You get no diff rendering; with commits, `git diff --stat <a> <b>` is free and already correct about renames and binaries. (5) Whole-tree restore-with-deletion is one `read-tree -u --reset`. Where I'd switch: a monorepo where a `status` walk crosses a second or two, or a case needing process/DB rollback — then the right primitive is a filesystem snapshot (APFS `clonefile`, btrfs/ZFS subvolume) or an overlayfs upper layer, i.e. the sandbox owns the rollback rather than the agent. Shadow git is the sweet spot for "single workspace, tens of thousands of files, needs diffs".

4. **"How is this different from aider's auto-commit and from Claude Code's checkpoints?"** aider commits into the *user's real repository* with an `aider:` message and `/undo` does a `git reset --hard` of its own last commit (refusing if HEAD isn't aider's). Trade-offs: the user sees checkpoints in ordinary `git log`, and they survive reboots as real history — a genuine advantage — but it pollutes history and `git blame`, needs a configured `user.email`, fires the user's hooks unless suppressed, cannot capture gitignored files, is unusable in a non-git workspace (Mini-Agent's default `./workspace` is exactly that), and races with the user's own commits and staged index. Claude Code's checkpoints go the other way: outside the repo, only files *it* edited, and `/rewind` offers code-only / conversation-only / both — and it explicitly does **not** undo bash side effects. My design sits between them: outside the repo like Claude Code (zero writes to the user's `.git` — asserted in the test), but whole-tree diffed like aider so that `bash`-caused mutations *are* captured, paid for with a 19 ms `status` walk per mutating step. I keep the three-way rewind mode because conversation-only is genuinely useful — "keep the code, retry the reasoning with a hint".

5. **"What can this feature not undo, and how do you keep that honest?"** Anything that isn't a file in the work tree: rows written to a database, HTTP requests already sent, packages installed into a venv that's on the exclude list, background processes still running from `bash_tool.py:354` (whose `BackgroundShellManager._shells` survives a rewind untouched and may still be writing to files you just restored), the user's real `HEAD` and staged index, nested-repo contents, and file mtimes (restored files get fresh mtimes, so build systems will rebuild — usually fine, occasionally load-bearing for `make`). The design decision that keeps it honest is that `restore()` returns a `RestoreReport` with `restored / deleted / left_alone` and the store *never* deletes a path it never captured, so the failure mode is always "less was undone than you expected", never "something you cared about vanished". The UI prints the report and the `real_head` warning rather than claiming success. Concretely I'd also block rewind while any background shell is alive, or at least warn with the `bash_id`s.

6. **"Rewind is itself destructive — what happens when someone rewinds by mistake?"** The first thing `rewind()` does is `capture(label='pre-rewind')`, so the pre-rewind state is a normal checkpoint and `/rewind 1` again returns to it — the history of checkpoints is append-only, never truncated. That's a deliberate difference from `git reset --hard`, where the destroyed state was uncommitted and therefore unreachable even via reflog. It also means the store is a linear log, not a tree: rewinding to checkpoint 3 and continuing produces checkpoints 9, 10, 11 whose parent commit is 8, so `git log --graph refs/mini-agent/cp/*` shows the branch structure for free while `index.jsonl` stays a flat chronological list — which is the ordering `/rewind n` should count in, because users count in wall-clock "how many things ago", not in DAG ancestry.

## 前置条件

1. `mini_agent/agent.py:390-394`, `:399-401`, `:480-481`, `:490-492` — `run()` returns from four different places, so a `turn-end` capture has to be duplicated four times or the loop body has to be wrapped in `try/finally`. Do the `try/finally` first; it is ~10 lines and every later hook depends on it.

2. `mini_agent/cli.py:673-678` — `/clear` sets `agent.messages = [agent.messages[0]]` without touching any turn counter (there is none yet). Whatever turn counter the checkpoint hooks introduce must be reset here, or checkpoint records point at a transcript that was thrown away.

3. `mini_agent/tools/bash_tool.py:354` and `:391` — `create_subprocess_shell` passes no `env=`, so the child inherits `os.environ`. This is not a bug to fix, it is a constraint to respect: it means the checkpoint store must never write GIT_DIR / GIT_INDEX_FILE / GIT_CONFIG_GLOBAL into `os.environ`. Verify by grepping for `os.environ[` in `checkpoint.py` — it should be zero hits outside `{**os.environ, ...}`.

## 明确不做

Not building: cross-session persistence and resume (checkpoints die with the session dir), a branching checkpoint *tree* UI (the DAG exists in the object store, but `/rewind n` counts linearly in wall-clock order), partial/hunk-level rewind, restoration of the user's real `HEAD`/index/stash (deliberately refused, not merely unimplemented), rollback of non-filesystem side effects, nested-repo contents, locking between two agents on one workspace, Windows, and any background `git gc` beyond a session-end `prune` of checkpoints past `keep=200`. To an interviewer: "I built the mechanism, not the product — the interesting decisions are all in what gets captured and how file state and message state are kept from disagreeing, and none of the cut items change either. The one cut I'd defend hardest is refusing to restore the user's real `HEAD`: the store is read-only with respect to their repository, and I'd rather print `git reset --soft <sha>` and let them run it than have an agent quietly rewrite their history."

## 代码量

~700 LOC total: `mini_agent/checkpoint.py` ~300 (store 190, rewind + boundary check 45, diff/list/gc 65); `agent.py` hooks ~25 across 5 sites; `cli.py` ~70 (3 slash commands + help + wiring); `config.py` ~12; `tests/test_checkpoint.py` ~180; `scripts/demo_checkpoint.py` ~120.

## 工期

2.5-3 days. Day 1: `checkpoint.py` capture/restore against a scratch workspace, driven entirely from a script — this is where the `info/attributes`, pathspec-exclusion-vs-`-f`, and `read-tree` index-sync findings get nailed down. Day 2: agent/cli hooks, the rewind boundary logic, and the three slash commands. Day 3 (half): the fingerprint test suite and the demo script with the timing table.
