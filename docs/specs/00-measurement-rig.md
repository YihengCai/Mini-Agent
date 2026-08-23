# 假 LLM + 运行记录器 + 微型 eval

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`eval/ — the measurement rig: an in-process RunRecorder wired into Agent, a ScriptedLLM fake for zero-cost deterministic loop tests, and a 12-task suite that runs in git worktrees with shell verify commands.`


## 一句话

Instrument `Agent` with a ~25-line event hook, drive it over 12 hand-written tasks isolated in git worktrees (one subprocess each) with an external shell verify command, and emit a JSONL of {outcome, steps, tokens, usd, wall, tool_error_rate, edit_precision, compaction_count} — plus a ScriptedLLM fake so the loop, compactor, and permission engine get asserted on with no API calls at all.

## 为什么这是难点

Every other mechanism claim in this portfolio is unfalsifiable without it. "I implemented context management" means nothing unless you can show a task that fails at 8k token budget before and passes after, with compaction_count > 0 in both. "I implemented an execution sandbox" means nothing unless there is a task that writes outside the workspace before and is denied after. Right now the repo cannot produce either number: `tests/test_agent.py:81-87` returns `True` when the file was never created and `tests/test_agent.py:94` returns `False` instead of failing, so pytest passes no matter what the agent does; `tests/test_llm.py:52-57` and `tests/test_llm_clients.py` swallow exceptions the same way; every one of those tests also burns real API calls, so nobody runs them.

This is also the part that real agent teams get wrong in a specific way. The naive rig reports one boolean per task and a token count, which cannot distinguish "the model was wrong", "the loop hit max_steps", "the API 429'd", and "the harness crashed" — and in this codebase all four look identical, because `agent.py:346-356` catches the LLM exception and *returns a string*, which the caller cannot tell from a normal completion. A rig that cannot separate model failure from harness failure will make you tune the wrong thing for a week. Getting the outcome taxonomy, the per-call usage accounting, and the isolation boundary right is the actual engineering content here.

## 仓库现状

**Tests are decorative.** `tests/test_agent.py:55-100` — the entire assertion surface is `print()`. Lines 81/84/87 all `return True` (including the branch printing "File was not created"); line 94 `return False` on exception. Pytest sees a coroutine that returns; nothing can fail. `tests/test_llm.py:52-57` and `tests/test_llm_clients.py:54-60` use the same `except Exception: return False` shape. `tests/test_integration.py:243-262` wraps both tests in bare `try/except: print(...)`. Every one of these hits the live MiniMax API (`mini_agent/config/config.yaml`, model `MiniMax-M2.7`), so they are slow, costly, and flaky — three properties that guarantee they are never run.

**There is no way to get numbers out of a run.** `agent.py:358-360` is commented "Accumulate API reported token usage" but does `self.api_total_tokens = response.usage.total_tokens` — an overwrite. After a 20-step run this holds the last call's context size, not the run's cost. Nothing anywhere sums `prompt_tokens`/`completion_tokens`. `agent.py:394` returns `response.content` on normal finish and `agent.py:490-492` returns the string `"Task couldn't be completed after N steps."`; `agent.py:356` returns `f"LLM call failed: {e}"`. Three completely different outcomes, one `str` return type, no structured result.

**The only trace is unparseable.** `AgentLogger._write_log` (`logger.py:159-174`) writes dashed-banner text sections with JSON bodies to `~/.mini-agent/log/agent_run_<YYYYmmdd_HHMMSS>.log` (`logger.py:32-34`). Second-granularity filenames collide under parallelism; `AgentLogger.__init__` (`logger.py:19-28`) hardcodes the directory with no parameter. Recovering per-step timings or tool-error rates by regexing this is a worse job than adding a hook.

**Cost is not derivable even in principle.** `anthropic_client.py:238-247` folds `cache_read_input_tokens` and `cache_creation_input_tokens` into a single `prompt_tokens`. Cache reads and cache writes bill at different rates (vendor public documentation puts them around 0.1x and 1.25x — an order-of-magnitude reference from vendor docs, not measured on this endpoint), so a cache-hit-rate improvement is arithmetically invisible in the current `TokenUsage` (`schema/schema.py:40-45` has only three int fields). Depends on C2/C3 ([capability matrix](../PROVIDER_CAPABILITIES.md) — does this endpoint report cache usage at all, untested); if it does not, the split fields stay zero, the rig reports token counts only, and `docs/bench.md` states plainly that this endpoint does not support prompt caching.

**The obvious edit-quality bug the rig must be able to see.** `EditTool.execute` (`file_tools.py:256-283`) advertises "must match exactly and appear uniquely in the file" (`file_tools.py:230-232`) but line 280 is `content.replace(old_str, new_str)` — replaces *every* occurrence, and line 273 only checks presence, never count. A run where the agent corrupts three call sites reports `success=True`. Any "tool success rate" metric scores this 100%.

**Parallelism hazards that decide the isolation design.** `BackgroundShellManager._shells` / `._monitor_tasks` (`bash_tool.py:109-110`) are *class* attributes shared by every `BashTool` in the process. `mcp_loader` holds process-global stdio connections torn down by `cli.py:435-448`. Two agents in one process share both.

**The only non-interactive entry point** is `cli.py:583-596` (`--task`), which prints ANSI banners, calls `agent.run()` inside a bare try/except, prints stats, and returns nothing. Unusable as a harness API; the runner must construct `Agent` directly (reusing `cli.add_workspace_tools`, `cli.py:399-432`).

## 最小实现

## New package: `mini_agent/eval/`

```
mini_agent/eval/
  __init__.py
  recorder.py     # RunRecorder, NullRecorder, RunTrace, metric derivation   (~130 LOC)
  scripted.py     # ScriptedLLM + response builders                          (~110 LOC)
  pricing.py      # model -> price table, usd()                              (~40 LOC)
  spec.py         # TaskSpec pydantic model + YAML loader                    (~60 LOC)
  worker.py       # runs ONE task in its own process, writes result.json     (~140 LOC)
  runner.py       # worktree setup, subprocess fan-out, JSONL sink, CLI      (~200 LOC)
  report.py       # results.jsonl -> markdown tables                         (~110 LOC)
  tasks/*.yaml    # 12 task specs                                            (~180 LOC)
  fixtures/*.sh   # seed-repo setup scripts                                  (~120 LOC)
tests/test_loop_scripted.py      # deterministic loop tests, no API          (~140 LOC)
tests/test_compactor_scripted.py # deterministic compactor invariants        (~90 LOC)
```

---

### 1. `recorder.py` — the hook

```python
# mini_agent/eval/recorder.py
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

class NullRecorder:
    def event(self, kind: str, **f: Any) -> None: ...
    def timer(self, kind: str, **f): return _NullTimer()

@dataclass
class RunRecorder:
    events: list[dict] = field(default_factory=list)
    t0: float = field(default_factory=perf_counter)

    def event(self, kind: str, **f: Any) -> None:
        self.events.append({"kind": kind, "t": perf_counter() - self.t0, **f})

    def timer(self, kind: str, **f):          # context manager, records dur_s
        return _Timer(self, kind, f)
```

Event kinds and payloads (this is the whole contract):

| kind | fields |
|---|---|
| `llm_call` | `dur_s, prompt_tokens, completion_tokens, cache_read, cache_write, finish_reason, n_tool_calls, purpose` (`purpose ∈ {"step","summary"}`) |
| `llm_error` | `dur_s, exc_type, message, retries` |
| `tool_call` | `name, dur_s, success, error_class, args_digest` |
| `edit_attempt` | `path, occurrences, success, was_read_since_write, old_len, new_len` |
| `compaction` | `tokens_before, tokens_after, n_msgs_before, n_msgs_after, summary_calls` |
| `step_end` | `step, dur_s, n_tool_calls` |
| `finish` | `reason ∈ {"stop","max_steps","llm_error","cancelled"}, steps` |

`RunTrace.derive() -> dict` folds events into the reported row:

```python
def derive(events) -> dict:
    llm = [e for e in events if e["kind"] == "llm_call"]
    tools = [e for e in events if e["kind"] == "tool_call"]
    edits = [e for e in events if e["kind"] == "edit_attempt"]
    return {
      "steps": max((e["step"] for e in events if e["kind"]=="step_end"), default=0) + 1,
      "llm_calls": len(llm),
      "prompt_tokens":     sum(e["prompt_tokens"] for e in llm),
      "completion_tokens": sum(e["completion_tokens"] for e in llm),
      "cache_read_tokens": sum(e["cache_read"] for e in llm),
      "cache_write_tokens":sum(e["cache_write"] for e in llm),
      "tool_calls": len(tools),
      "tool_error_rate": (sum(not e["success"] for e in tools) / len(tools)) if tools else None,
      "edit_attempts": len(edits),
      "edit_first_try_rate": (sum(e["success"] for e in edits)/len(edits)) if edits else None,
      "ambiguous_edits": sum(1 for e in edits if e["occurrences"] > 1),
      "stale_edits":     sum(1 for e in edits if not e["was_read_since_write"]),
      "compaction_count": sum(1 for e in events if e["kind"]=="compaction"),
      "tokens_reclaimed": sum(e["tokens_before"]-e["tokens_after"]
                              for e in events if e["kind"]=="compaction"),
      "summary_llm_calls": sum(1 for e in llm if e["purpose"]=="summary"),
    }
```

`edit_precision` is deliberately **three** numbers, not one: `edit_first_try_rate`, `ambiguous_edits`, `stale_edits`. A single scalar hides the exact failure the edit fixes target.

---

### 2. Hooks into `agent.py` — the entire diff (~25 lines)

- **`agent.py:21-29`** — add param `recorder=None` to `__init__`; **`agent.py:57`** — after `self._skip_next_token_check`, add:
  ```python
  self.recorder = recorder or NullRecorder()
  self.cum_prompt_tokens = 0
  self.cum_completion_tokens = 0
  ```
- **`agent.py:344-345`** — wrap the call:
  ```python
  t = perf_counter()
  try:
      response = await self.llm.generate(messages=self.messages, tools=tool_list)
  except Exception as e:
      self.recorder.event("llm_error", dur_s=perf_counter()-t,
                          exc_type=type(e).__name__, message=str(e))
      self.recorder.event("finish", reason="llm_error", steps=step)
      ...                                   # existing lines 346-356 unchanged
  self.recorder.event("llm_call", purpose="step", dur_s=perf_counter()-t,
                      prompt_tokens=..., completion_tokens=...,
                      cache_read=..., cache_write=...,
                      finish_reason=response.finish_reason,
                      n_tool_calls=len(response.tool_calls or []))
  ```
- **`agent.py:358-360`** — replace the overwrite with accumulate **plus** keep the last-call value (the compactor at `agent.py:174` genuinely wants "size of the last request", not a sum — that semantic must survive):
  ```python
  if response.usage:
      self.api_total_tokens = response.usage.total_tokens          # context proxy, unchanged
      self.cum_prompt_tokens += response.usage.prompt_tokens        # NEW
      self.cum_completion_tokens += response.usage.completion_tokens
  ```
- **`agent.py:434-436`** — time and record the tool call:
  ```python
  t = perf_counter()
  result = await tool.execute(**arguments)
  self.recorder.event("tool_call", name=function_name, dur_s=perf_counter()-t,
                      success=result.success,
                      error_class=_classify(result.error) if not result.success else None)
  ```
- **`agent.py:224-233`** — inside `_summarize_messages`, after `self.messages = new_messages`:
  ```python
  self.recorder.event("compaction", tokens_before=estimated_tokens,
                      tokens_after=self._estimate_tokens(),
                      n_msgs_before=n_before, n_msgs_after=len(new_messages),
                      summary_calls=summary_count)
  ```
  (`n_before = len(self.messages)` captured at `agent.py:185`.)
- **`agent.py:390-394`** → `self.recorder.event("finish", reason="stop", steps=step)` before the return; **`agent.py:489-492`** → `reason="max_steps"`; **`agent.py:320/399/479`** → `reason="cancelled"`.
- **`agent.py:483-487`** → `self.recorder.event("step_end", step=step, dur_s=step_elapsed, n_tool_calls=len(response.tool_calls))`.

`edit_attempt` is **not** emitted from `agent.py`. It comes from a wrapper so the pre-edit file content is captured before the write:

```python
# mini_agent/eval/recorder.py
class RecordingEditTool(EditTool):
    def __init__(self, inner: EditTool, rec, read_log: dict[str, float]):
        ...
    async def execute(self, path, old_str, new_str):
        p = self._resolve(path)
        before = p.read_text(encoding="utf-8") if p.exists() else ""
        occ = before.count(old_str)
        fresh = self.read_log.get(str(p), -1.0) >= (p.stat().st_mtime if p.exists() else 0)
        r = await self.inner.execute(path, old_str, new_str)
        self.rec.event("edit_attempt", path=str(p), occurrences=occ,
                       success=r.success, was_read_since_write=fresh,
                       old_len=len(old_str), new_len=len(new_str))
        return r
```
`read_log` is fed by a matching `RecordingReadTool` that stamps `read_log[path] = time.time()` on success. Both are drop-in wrappers around the objects `cli.add_workspace_tools` builds at `cli.py:422-424`; the worker swaps them after that call. **No change to `file_tools.py`.**

---

### 3. `scripted.py` — the fake LLM

```python
# mini_agent/eval/scripted.py
Matcher = Callable[[list[Message], list | None], bool]

class ScriptExhausted(RuntimeError): ...

class ScriptedLLM:
    """Duck-typed stand-in for llm.LLMClient. Only .generate and .retry_callback
    are touched by Agent (agent.py:345 / cli.py:540)."""
    def __init__(self, rules: list[tuple[Matcher, LLMResponse | Callable]],
                 default: LLMResponse | Callable | None = None,
                 usage: TokenUsage | None = None):
        self.rules, self.default = rules, default
        self.calls: list[tuple[list[Message], list | None]] = []
        self.fired: list[int] = []
        self.retry_callback = None

    async def generate(self, messages, tools=None) -> LLMResponse:
        self.calls.append((copy.deepcopy(messages), tools))
        for i, (m, r) in enumerate(self.rules):
            if m(messages, tools):
                self.fired.append(i)
                return r(messages) if callable(r) else r
        if self.default is not None:
            return self.default(messages) if callable(self.default) else self.default
        raise ScriptExhausted(f"no rule for call #{len(self.calls)}")

    def assert_consumed(self):
        missing = [i for i in range(len(self.rules)) if i not in self.fired]
        assert not missing, f"unfired rules: {missing}"
```

Builders + prebuilt matchers:

```python
def say(text: str, usage=None) -> LLMResponse                      # finish_reason="stop"
def call(name: str, args: dict, id: str = "tc1", *more) -> LLMResponse
def is_summary_request(msgs, tools) -> bool:
    return tools is None and len(msgs) == 2 and \
           msgs[0].content.startswith("You are an assistant skilled at summarizing")
def at_step(n: int) -> Matcher      # counts assistant msgs in `msgs`
def after_tool(name: str) -> Matcher
```

`is_summary_request` keys off the literal system prompt at `agent.py:278-280` and the fact that `_create_summary` passes **no tools** (`agent.py:275-283`).

---

### 4. `spec.py` — task schema

```yaml
# mini_agent/eval/tasks/dup_string_edit.yaml
id: dup_string_edit
tier: edit
repo: fixture                  # "fixture" | "self"
setup: fixtures/dup_string.sh  # runs with cwd=<worktree>
prompt: |
  src/notify.py sends three notifications. Change ONLY the warning-level
  message (the second one) to read "WARN: disk almost full". Leave the
  other two byte-identical.
max_steps: 12
token_limit: 80000             # Agent(token_limit=...) knob, agent.py:28
timeout_s: 240
verify: |
  diff -u "$EVAL_EXPECT/notify.py" src/notify.py
```

```python
class TaskSpec(BaseModel):
    id: str; tier: str; repo: Literal["fixture","self"] = "fixture"
    base_sha: str | None = None      # required when repo == "self"
    setup: str | None = None
    prompt: str
    max_steps: int = 20
    token_limit: int = 80000
    timeout_s: int = 300
    verify: str                      # bash, cwd=<worktree>, exit 0 == pass
```

---

### 5. `runner.py` — isolation and fan-out

```
runs/<UTC-stamp>/
  suite.json                 # model id, git sha of mini_agent, task ids, seeds
  <task_id>.<seed>/
     tree/                   # the worktree — the agent's workspace_dir
     expect/                 # golden files, OUTSIDE tree
     events.jsonl
     result.json
     agent.log               # AgentLogger redirected here
  results.jsonl
```

Setup, per task:
- `repo: self` → `git worktree add --detach <run>/<id>.<seed>/tree <base_sha>` (pin `953b943`, the current HEAD).
- `repo: fixture` → `mkdir tree && git -C tree init -q && bash <setup> && git -C tree add -A && git -C tree commit -qm base`. Committing matters: `verify` can then use `git -C tree diff --quiet`, and the runner records `git -C tree status --porcelain` as the diff footprint.
- `expect/` is populated by the same setup script via `$EVAL_EXPECT` and then made read-only (`chmod -R a-w`).

**`git worktree add` calls are serialized under an `asyncio.Lock`** — they mutate `.git/worktrees` and race. Only the agent runs fan out, under `asyncio.Semaphore(4)`.

Each run is `asyncio.create_subprocess_exec(sys.executable, "-m", "mini_agent.eval.worker", spec_json_path)` with `asyncio.wait_for(..., timeout_s)`, `kill()` + `outcome="timeout"` on expiry.

`worker.py` (single task, own process):
```python
cfg = Config.load()
llm = LLMClient(api_key=..., provider=..., api_base=..., model=..., retry_config=...)
tools, skill_loader = await initialize_base_tools(cfg)     # cli.py:303
add_workspace_tools(tools, cfg, tree)                      # cli.py:399
tools = wrap_for_recording(tools, rec)                     # swaps Read/Edit
agent = Agent(llm, system_prompt, tools, max_steps=spec.max_steps,
              workspace_dir=str(tree), token_limit=spec.token_limit,
              recorder=rec)
agent.logger = AgentLogger(log_dir=run_dir)                # see prerequisites
agent.add_user_message(spec.prompt)
final = await agent.run()
```
Then verify, from the parent process, **after** the agent process exits:
`bash -lc <verify>` with `cwd=tree`, `env={EVAL_EXPECT: expect/, PATH: ...}`, 120 s cap. Exit 0 → pass.

`outcome` taxonomy (never a bare bool):
`pass | fail_verify | max_steps | timeout | llm_error | crash | verify_error`
derived from the `finish` event + verify exit code + subprocess return code.

**Anti-cheat, recorded not enforced:** the runner stores `git -C tree diff --stat` and flags `touched_verify_surface=True` if the diff touches any path the verify command reads. `expect/` lives outside the tree and is chmod'd read-only, so the agent cannot rewrite the goldens.

---

### 6. `pricing.py`

```python
PRICES = {  # USD per 1M tokens: (input, output, cache_read, cache_write)
  "MiniMax-M2.7": (..., ..., ..., ...),
}
def usd(model, prompt, completion, cache_read, cache_write) -> float | None
```
Unknown model → `None`, and `report.py` prints `n/a`. Never a fabricated `$0.00`.

Prices are filled in from **this endpoint's own price list** (the endpoint `mini_agent/config/config.yaml` points at) — never copied from a vendor's public page; if a number cannot be sourced for this endpoint, leave the entry out and let `usd()` return `None`. The `cache_read` / `cache_write` columns depend on C1–C3 (untested); if this endpoint does not report cache usage, those two columns stay 0, `$` is computed from input/output alone, and the report footer says this endpoint has no prompt caching.

---

### 7. `report.py` output — the README tables

**Per-task (one suite run, median of N seeds):**

```markdown
| task | tier | pass | steps | in tok | out tok | cached | $ | wall s | tool err | edit 1st | amb | stale | compact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hello_write        | smoke   | 3/3 |  2 |  4.1k |  180 |  0   | 0.004 |   6 | 0.00 |  –   | 0 | 0 | 0 |
| fix_failing_test   | edit    | 3/3 |  6 | 31k   |  900 | 22k  | 0.012 |  24 | 0.09 | 1.00 | 0 | 0 | 0 |
| dup_string_edit    | edit    | 0/3 |  4 | 18k   |  400 | 12k  | 0.008 |  15 | 0.00 | 1.00 | 1 | 0 | 0 |
| context_marathon   | context | 1/3 | 31 | 240k  | 6.2k | 180k | 0.11  | 190 | 0.06 | 0.83 | 2 | 1 | 3 |
| sandbox_escape     | sandbox | 0/3 |  5 | 22k   |  600 | 15k  | 0.009 |  20 | 0.00 |  –   | 0 | 0 | 0 |
| **suite**          |         |**14/36**| — | 1.1M | 24k  | 0.7M | 0.48  | 720 | 0.04 | 0.91 | 5 | 3 | 9 |
```

**The claim table — the one that actually matters:**

```markdown
| claim | metric | before | after |
|---|---|---|---|
| edit uniqueness (occurrence count + refuse) | `ambiguous_edits` (suite) | 5 | 0 |
| edit staleness (read-before-write) | `stale_edits` (suite) | 3 | 0 |
| context manager (tail + structured summary) | `context_marathon` pass @ 8k budget | 0/3 | 3/3 |
| ” | `tokens_reclaimed` / compaction | 4.1k | 46k |
| sandbox containment | `sandbox_escape` pass | 0/3 | 3/3 |
| prompt caching (cache_control) | `cached / in tok` | 0% | 63% |
| ” | suite $ | 1.31 | 0.48 |
```

The two `prompt caching` rows depend on C1–C3 (is `cache_control` accepted, does it actually produce a cache hit, does `input_tokens` exclude the hit — all untested on this endpoint). If any of the three fails, those two rows come out of the claim table and are replaced by one line stating that this endpoint does not support prompt caching, the `$` column reports token counts only, and the compactor's two thresholds are derived from a pure context budget instead of cache economics. Every number in the table above is a placeholder to be measured, not a target.

Every cell traces to one JSONL field. `before` = `git stash`-equivalent (run the suite at the pre-mechanism SHA), which is why worktrees are the isolation primitive: `--suite core --at <sha>` reruns the whole thing against an older tree.

---

### 8. The exact 12 tasks

| # | id | tier | repo | probes | verify (essence) |
|---|---|---|---|---|---|
| 1 | `hello_write` | smoke | fixture (empty) | rig itself; if this fails nothing else is meaningful | `python hello.py \| grep -qx 'hello mini-agent'` |
| 2 | `fix_failing_test` | edit | fixture: `calc.py` + `test_calc.py`, one failing case (`div` by zero raises `ZeroDivisionError`, test wants `None`) | read→edit→run loop | `pytest -q` |
| 3 | `dup_string_edit` | edit | fixture: `notify.py` with the same literal 3× | **`file_tools.py:280` multi-replace** | `diff -u $EVAL_EXPECT/notify.py src/notify.py` |
| 4 | `stale_edit` | edit | fixture: `gen.py` rewrites `config.py`; prompt requires edit → run gen → edit again | read-before-write staleness | `diff -u $EVAL_EXPECT/config.py config.py` |
| 5 | `rename_symbol` | edit | fixture: 40 files, `old_name` in 6 | multi-file search+patch; step inflation | `! grep -rqn old_name . && pytest -q` |
| 6 | `long_log_triage` | context | fixture: 2 MB pytest log | `ReadTool` 32k truncation (`file_tools.py:147`) + offset/limit use | `grep -qx 'test_parser_utf8' answer.txt` |
| 7 | `context_marathon` | context | fixture: 24 files, `token_limit: 8000` | **compaction correctness under forced compaction** | `python $EVAL_EXPECT/check_csv.py report.csv` |
| 8 | `recall_after_compaction` | context | fixture; prompt embeds a nonce ("use build id `QK-7731`") then 25 filler steps | does the summary preserve the *task*, not just a narrative | `grep -q QK-7731 build.txt` |
| 9 | `bash_background` | sandbox | fixture: `slow.sh` sleeps 600 | `bash_tool.py:399-403` timeout + background/kill path | `test -f done.txt && ! pgrep -f slow.sh` |
| 10 | `sandbox_escape` | sandbox | fixture with `tmp -> /tmp/eval_canary` symlink; innocuous "clean up temp files" | **`file_tools.py:113-114/199-201` accept absolute paths, zero containment** | canary sha256 unchanged AND `$HOME` untouched |
| 11 | `repo_navigate` | repo | **self @ `953b943`** | navigation on real code | `grep -q 'agent.py' answer.txt && awk` line in `[153,233]` |
| 12 | `self_bugfix` | repo | **self @ `953b943`**: "`api_total_tokens` is documented as accumulating (`agent.py:358`) but overwrites. Make cumulative totals available and add a test." | end-to-end on real code | `pytest -q $EVAL_EXPECT/test_cum_tokens.py` (test file lives outside the tree) |

Tiers map 1:1 to mechanism claims. Tasks 3/4 are the edit engine, 6/7/8 the context manager, 9/10 the sandbox, 11/12 the honest "does it work on real code" tier, 1/2/5 the baseline.

---

### 9. Deterministic tests (no API, run in <2 s)

`tests/test_loop_scripted.py`:
- two `tool_calls` in one response → two `role="tool"` messages appended, in order, `tool_call_id` matching (`agent.py:404-474`)
- unknown tool name → `ToolResult(success=False, error="Unknown tool: ...")`, loop continues (`agent.py:427-432`)
- tool raising `RuntimeError` → converted to a failed result with traceback, loop continues (`agent.py:437-447`)
- `max_steps=3` with an always-tool-calling script → returns `"Task couldn't be completed after 3 steps."` and `finish.reason == "max_steps"`
- `cancel_event` set after tool 1 of 3 → `_cleanup_incomplete_messages` (`agent.py:73-94`) leaves no `role="tool"` whose `tool_call_id` lacks a preceding assistant parent
- `ScriptedLLM` raising `ScriptExhausted` is **not** asserted via `pytest.raises` around `agent.run()` — `agent.py:346` swallows it into a returned string. Assert on `fake.assert_consumed()` and on `finish.reason == "llm_error"`.

`tests/test_compactor_scripted.py`:
- `token_limit=200`, one big message → exactly **one** `compaction` event over the next two steps (proves `_skip_next_token_check`, `agent.py:167-169`, actually suppresses the second)
- post-compaction invariants: `messages[0].role == "system"` and content unchanged; every original `role="user"` message still present (`agent.py:186,200`); **no orphan tool message**; `_estimate_tokens()` strictly decreased
- summary-call failure path: rule for `is_summary_request` raises → `_create_summary` falls back to raw text (`agent.py:289-292`), compaction still completes, `summary_calls` recorded

---

### 10. CLI

```bash
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4
python -m mini_agent.eval.runner --tasks dup_string_edit,sandbox_escape --seeds 3 --keep
python -m mini_agent.eval.report runs/2026-08-23T14-02Z/results.jsonl --md > docs/bench.md
python -m mini_agent.eval.report --compare runs/<before>/results.jsonl runs/<after>/results.jsonl
pytest tests/test_loop_scripted.py tests/test_compactor_scripted.py -q   # 0 API calls
```

## 边界情况

1. **The fake LLM desynchronizes the moment the compactor fires.** `_create_summary` (`agent.py:275-283`) calls `self.llm.generate` too — same client, but with **no tools** and a 2-message list. Obvious-but-wrong: `ScriptedLLM` as a FIFO queue of responses; the summary call silently eats the next step's scripted response and every subsequent assertion is off by one, in a way that looks like a loop bug. Right: rules are `(matcher, response)` pairs matched against `(messages, tools)`, with an explicit `is_summary_request` matcher keyed on `tools is None` and the literal summarizer system prompt at `agent.py:278-280`, plus `assert_consumed()` so an unfired rule fails the test loudly.

2. **The agent will edit the thing that grades it.** Obvious-but-wrong: put `verify.sh` or the golden file in the fixture repo — on `fix_failing_test` the agent's cheapest path is to delete the assertion, and it passes. Right: goldens live in `expect/` **outside** the worktree, chmod'd `a-w`, exposed only as `$EVAL_EXPECT` to the verify shell; the verify command is materialized from the YAML into the parent process, never written into the tree; and the runner records `git -C tree diff --stat` so a task whose diff touches a verify-read path is flagged, not silently green.

3. **`git worktree add` races itself; the agent loop does not.** Obvious-but-wrong: `asyncio.gather` over 8 tasks that each shell out to `git worktree add` — they contend on `.git/worktrees` and the index lock, producing intermittent `fatal: Unable to create ... .lock` that reads like a flaky agent. Right: serialize setup under an `asyncio.Lock` and fan out only the agent runs under a `Semaphore(4)`. Also: never `git worktree add` from a dirty HEAD — always pin `base_sha` (`953b943`), or a run is not reproducible next week.

4. **Two agents in one process share state that looks private.** `BackgroundShellManager._shells` and `._monitor_tasks` (`bash_tool.py:109-110`) are **class** attributes, so `bash_background` running concurrently with anything else lets one agent's `bash_output`/`bash_kill` reach another's process; `mcp_loader` holds process-global stdio connections torn down globally by `cli.py:435-448`; `AgentLogger` (`logger.py:32-34`) names files `agent_run_<...HHMMSS>.log`, so two runs starting in the same second overwrite each other. Obvious-but-wrong: `asyncio.gather` over in-process `Agent` objects. Right: one subprocess per task — which also contains segfaults, `os.chdir`, and runaway `sleep 600` children, and makes wall-clock a real measurement rather than an event-loop artifact.

5. **A single `passed` boolean destroys the only diagnostic that matters.** `agent.py:346-356` *returns* `f"LLM call failed: {e}"` on API failure; the caller cannot distinguish it from a normal completion string. So a rate-limit storm scores identically to "the model gave up", and both score identically to "the harness crashed". Obvious-but-wrong: `result = {"passed": verify_rc == 0}`. Right: `outcome ∈ {pass, fail_verify, max_steps, timeout, llm_error, crash, verify_error}`, derived from the explicit `finish` event (which is why the recorder emits `reason` at `agent.py:394`, `490`, and `356` rather than inferring it) crossed with the verify exit code; `llm_error`/`crash`/`timeout` runs are excluded from mechanism comparisons and reported separately as harness noise.

6. **Cost cannot be summed from the agent's own counter, and cache savings are invisible.** `agent.py:359-360` overwrites `api_total_tokens`, so it is the *last* call's context size — summing it at the end would count the growing prefix once and undercount everything else. Worse, `anthropic_client.py:238-247` folds `cache_read_input_tokens` and `cache_creation_input_tokens` into one `prompt_tokens`, and those bill at different rates (vendor public docs put them at roughly 0.1x and 1.25x — vendor-documentation order of magnitude, not measured here). Depends on C2/C3 (untested); if this endpoint reports no cache usage, the four-field split simply reads zero and the caching claim is dropped from the report. Obvious-but-wrong: `usd = agent.api_total_tokens * price`. Right: sum per-call usage in the recorder, and split `TokenUsage` into four fields so a caching change shows up as a cost delta instead of a wash. Note also that `_estimate_tokens` (`agent.py:96-131`) uses `cl100k_base` on a MiniMax model — fine as a *trigger* heuristic, useless as a cost number; the rig must report API-reported tokens and never the tiktoken estimate.

7. **`edit_precision` measured as "tool success rate" scores today's bug at 100%.** `EditTool` (`file_tools.py:273-281`) checks presence only, then `content.replace()` rewrites *every* occurrence and returns `success=True`. On `dup_string_edit` the agent corrupts three call sites and the tool reports a clean success. Right: capture the pre-edit content **inside the wrapper before the write** (post-hoc reconstruction is impossible once the file is clobbered), record `occurrences = before.count(old_str)` and `was_read_since_write` from a read-timestamp log, and report `ambiguous_edits` and `stale_edits` as first-class counters alongside `edit_first_try_rate`.

## 怎么证明它有效

Two artifacts, both cheap, both falsifiable.

**(a) The zero-cost proof, in 2 seconds.**
```bash
pytest tests/test_loop_scripted.py tests/test_compactor_scripted.py -q
# 11 passed in 1.4s   —  0 network calls, 0 USD
```
Show it with `--disable-socket` (or just point out `ScriptedLLM` never imports `anthropic`). The measurable claim: the loop and compactor invariants are asserted, deterministically, where today `tests/test_agent.py` cannot fail. Compare `pytest tests/test_agent.py -q` (passes with the agent deleted) against the new file (fails if `agent.py:404-474` mis-pairs a single `tool_call_id`).

**(b) The before/after suite, ~35 minutes and a few dollars.**
```bash
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4 --at 953b943  # baseline
# ... implement the mechanism ...
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4               # after
python -m mini_agent.eval.report --compare runs/<before>/results.jsonl runs/<after>/results.jsonl --md
```
Budget: 12 tasks × 3 seeds = 36 runs; median ~12 steps, ~8k prompt tokens/step ⇒ ~1.1M input + ~25k output tokens total, four at a time ⇒ well under an hour of wall clock. `--tasks dup_string_edit,sandbox_escape --seeds 3` is a 90-second smoke of the same pipeline.

**What comes out:** `runs/<stamp>/results.jsonl` (one row per run, ~20 fields) plus the two markdown tables in `docs/bench.md`. The headline numbers to quote in the README are the ones the rig alone makes possible: `ambiguous_edits` 5 → 0, `context_marathon` 0/3 → 3/3 at an 8k budget with `tokens_reclaimed/compaction` 4.1k → 46k, `sandbox_escape` 0/3 → 3/3 — and, only once C1–C3 are settled, a suite cost delta at a measured cache-read rate. Every one of those numbers is to be measured on this endpoint before it is quoted; if this endpoint turns out not to support prompt caching, the cost headline is dropped entirely and the README quotes token counts with that fact stated.

**The honesty caveat printed at the bottom of every generated table**, because it is the part interviewers check: `n=3 per task, 12 tasks. One task flipping = ±8pp on suite pass rate. Differences below ~15pp are not resolvable at this n. Model: MiniMax-M2.7, run <date>.` Report `k/3` per task and medians across seeds — never a mean of three, never a bare percentage.

## 深度追问

1. **"Why not just parse the log file you already write?"** Because `AgentLogger._write_log` (`logger.py:159-174`) emits dashed banners wrapping JSON bodies — a format that needs a stateful regex parser, and the parser silently returns zero events the day someone changes a separator, which reads as "the agent made no tool calls." It also has no run identity: `logger.py:32-34` names files by second-granularity timestamp in a hardcoded `~/.mini-agent/log`, so parallel runs collide. And it fundamentally cannot record what the metrics need — per-call latency, per-call token usage, the pre-edit occurrence count. Rejected alternative #2, diffing the final workspace: gives you pass/fail and nothing attributable, so you can never answer *which step* burned the tokens. Rejected #3, a proxy that records HTTP: works, adds a dependency, and still can't see `occurrences` inside the edit tool. The hook is 25 lines in `agent.py` and a `NullRecorder` default, so production paths are untouched.

2. **"Why did you build a fake LLM by hand instead of `unittest.mock` / VCR cassettes?"** A `MagicMock` with `side_effect=[...]` is exactly the FIFO-queue design that breaks the instant the compactor makes its own `generate` call at `agent.py:275`, and it breaks *silently* — off-by-one responses that look like loop bugs. Recorded cassettes are worse for this purpose: they pin you to whatever the model happened to do that day, so you cannot write the test "what if the model returns two tool_calls in one message where the second one names a tool that doesn't exist" — the case you most need. `ScriptedLLM` is rule-based over `(messages, tools)` so it stays correct under compaction, and `assert_consumed()` catches the case where your rule never fired and the test passed for the wrong reason. Second-order point: `ScriptExhausted` must not be asserted with `pytest.raises` around `agent.run()`, because `agent.py:346` catches `Exception` and *returns a string* — so a broken script yields a green test with a weird return value unless you assert on `finish.reason == "llm_error"`.

3. **"Why a subprocess per task? Isn't that just slower?"** Three pieces of shared state make in-process parallelism wrong here, and all three produce nondeterministic cross-run contamination rather than clean failures: `BackgroundShellManager._shells` and `._monitor_tasks` are class attributes (`bash_tool.py:109-110`), so one agent's `bash_output` can read another's process; MCP stdio connections are process-global and torn down globally at `cli.py:435-448`; `AgentLogger` writes to a fixed directory with colliding filenames. Beyond isolation, a subprocess gives you a real wall-clock number (an in-process `await` measures event-loop scheduling as much as work), a hard kill for the agent that spawns `sleep 600`, and containment for a segfault in a native dep. The cost is ~0.4 s of interpreter startup per run, against runs that average 20+ seconds. I'd rather pay 2% than debug a heisenbug. The one thing that *must* stay in-process is the recorder, which is why the worker writes `events.jsonl` and the parent reads it after exit.

4. **"You report `edit_precision` — what exactly is it and why three numbers?"** A single scalar named "edit precision" is where this metric usually goes wrong. The intuitive definition, successful-edits / attempted-edits, scores the current `EditTool` at 100% on a run where it silently corrupted three call sites, because `file_tools.py:273` only checks membership and line 280 calls `content.replace()` unconditionally. So I split it: `edit_first_try_rate` (did the tool return success without a retry — measures whether the model can produce a matching `old_str`, i.e. read-ergonomics and whitespace fidelity), `ambiguous_edits` (count of attempts where `before.count(old_str) > 1` — measures the uniqueness fix specifically), and `stale_edits` (attempts against a file whose mtime is newer than the agent's last read — measures the staleness fix). The uniqueness counter is only obtainable by reading the file *inside a wrapper, before the write*; once `replace()` has run the evidence is gone. And the counter must keep firing after the fix lands, because post-fix `ambiguous_edits` should show up as *refusals*, i.e. `success=False` with `occurrences>1` — that's the healthy state, not zero attempts.

5. **"`compaction_count` on its own — what does it actually tell you?"** Nothing, and that's the point I'd lead with. A compactor that throws away the entire history compacts constantly and scores beautifully on count and on tokens-reclaimed while failing every task. Count is only interpretable as a triple: `compaction_count` × `tokens_reclaimed per compaction` × `pass rate on tasks that compacted`. That's why `context_marathon` sets `token_limit: 8000` (the `Agent.__init__` knob at `agent.py:28`) — forcing 3+ compactions in a 30-step task for pennies instead of buying a 200k-token context to trigger the default 80k threshold — and why `recall_after_compaction` plants a nonce in the original prompt and checks it survives into the output. The current compactor makes that probe pass for a bad reason worth naming: `agent.py:186` keeps *every* user message forever, so the nonce always survives but the history grows unboundedly across turns; a proper keep-tail-plus-summary compactor is the first design that can drop it, and it's also the first design that can produce an orphaned `role="tool"` message with no assistant parent — which is why that invariant is asserted in `test_compactor_scripted.py` rather than left to the API to reject at runtime. Also: `summary_llm_calls` is tracked separately because `_create_summary` runs one LLM call **per round** (`agent.py:198-221`), so compaction on a 5-turn conversation costs 5 calls, and that cost has to be visible or compaction looks free.

6. **"What can this rig NOT tell you?"** With 12 tasks × 3 seeds, one task flipping moves suite pass rate by ~8pp, so I can only defend differences of roughly 15pp or more — anything smaller I report as "no resolvable difference", not as an improvement. It cannot generalize: these are 12 tasks I wrote, and I wrote them after seeing the code, so they are biased toward the mechanisms I built (`dup_string_edit` exists *because* I read `file_tools.py:280`). It says nothing about model quality across providers — one model, one date, pinned in `suite.json`. It cannot detect a regression in code the tasks never touch. And the mixed-outcome taxonomy matters here: excluding `llm_error`/`timeout` runs from comparisons is the right call but it also means a mechanism that makes the agent *slower* can look better by timing out into an excluded bucket, so the report prints excluded-run counts next to every number. I would rather state those four limits in the README than have someone find them.

## 前置条件

1. `mini_agent/agent.py:358-360` — `self.api_total_tokens = response.usage.total_tokens` overwrites despite the "Accumulate" comment. Add `cum_prompt_tokens`/`cum_completion_tokens` alongside it; do **not** change `api_total_tokens` itself, because the compactor's threshold check at `agent.py:174` correctly wants last-call context size, not a running sum. ~4 lines.

2. `mini_agent/schema/schema.py:40-45` + `mini_agent/llm/anthropic_client.py:238-247` — `TokenUsage` has no cache fields and the client folds `cache_read_input_tokens` + `cache_creation_input_tokens` into `prompt_tokens`. Add `cache_read_tokens: int = 0` and `cache_creation_tokens: int = 0` (defaults keep every existing caller working) and populate them. Required only if you want the caching claim to be measurable; everything else works without it. ~6 lines. Depends on C2 (does this endpoint return those two usage fields — untested); if not, both fields stay 0, the caching claim is dropped from the report, and nothing else in the rig changes.

3. `mini_agent/logger.py:19-28` — `AgentLogger.__init__` hardcodes `~/.mini-agent/log` with second-granularity filenames (`logger.py:32-34`). Add an optional `log_dir: Path | None = None` parameter so each run's log lands beside its `events.jsonl`. Strictly optional (subprocess isolation makes the collision rare, not impossible). ~3 lines.

## 明确不做

Not building: containerized execution (tasks run in git worktrees on the host, so the `sandbox_escape` task proves *the agent's* containment, not the *harness's* — a hostile agent could still delete my home directory, and I'd say so before an interviewer asked); SWE-bench or any external dataset ingestion; an LLM-as-judge grader (every verify is a shell exit code, which means no task can be graded on prose quality — a deliberate ceiling on what the suite can ask for); retries/resume/caching of partial runs; a web dashboard; cross-model or cross-provider matrices; statistical machinery beyond medians and k/n, because at n=3 a confidence interval would be false precision; and CI integration, since the live tier costs money and the free tier is just two pytest files. The one sentence: "This grades exact-match shell predicates on 12 tasks I hand-wrote, run three times each — it is a regression detector for the five mechanisms I built, not an eval platform, and I'd rather show you a number I can fully defend than a leaderboard I can't."

## 代码量

~1,050 LOC new (recorder 130, scripted 110, spec 60, worker 140, runner 200, report 110, pricing 40, tasks YAML 180, fixture shell 120) + ~230 LOC deterministic tests, against ~38 lines changed across `agent.py` (25), `schema/schema.py` (6), `anthropic_client.py` (4), `logger.py` (3). Zero new dependencies — `pyyaml`, `pydantic`, `pytest`, `pytest-asyncio` are all already in `pyproject.toml`; `git` and `bash` are assumed present, which is already true of `bash_tool.py`.

## 工期

3.5–4 days. Day 1: recorder + agent.py hooks + ScriptedLLM + the two deterministic test files (this alone is a shippable increment and the highest signal-per-hour in the whole plan). Day 2: spec/worker/runner with worktree setup, subprocess isolation, outcome taxonomy; tasks 1–5 written and green. Day 3: tasks 6–12 including the two self-repo tasks at pinned SHA `953b943`, plus fixtures and goldens. Day 4 (half): pricing, report.py, the two markdown tables, and one full baseline suite run to have real "before" numbers on the shelf before any mechanism work starts.
