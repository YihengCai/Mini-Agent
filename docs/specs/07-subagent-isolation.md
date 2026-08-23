# 子 agent 与上下文隔离

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Sub-agent delegation with context isolation (`task`/explore tool)`


## 一句话

A `task` tool that runs a full child `Agent` with its own message list, a read-only tool subset, three orthogonal budgets (steps / input tokens / wall clock), and a mandatory structured `submit_report` return contract, so an arbitrarily large exploration costs the parent context a bounded ~1 KTok regardless of how much the child read.

## 为什么这是难点

Every coding agent hits the same wall: the cheapest way to answer "where is X handled?" is to read a lot of bytes, and every byte read lands permanently in the one context window that also has to hold the plan and the edits. This repo makes that concrete. `ReadTool` truncates at 32 000 tokens (`mini_agent/tools/file_tools.py:147`), and `BashTool` output is not truncated at all — `BashOutputResult.format_content` (`mini_agent/tools/bash_tool.py:32-48`) concatenates raw stdout with no cap. So two large reads, or one `grep -rn` over a repo, can push the parent past `token_limit=80000` (`mini_agent/agent.py:28`) and detonate `_summarize_messages` (`mini_agent/agent.py:153-233`), which irreversibly replaces the entire execution history with LLM prose. Exploration is the single largest context consumer and it is almost entirely *discardable*: of 40 KB read to answer a question, maybe 300 bytes matter.

Delegation is the preventive answer to the problem compaction answers reactively. It is also the only context mechanism that is a *hard bound* rather than a heuristic: with a structured return contract, the parent's growth per delegation is capped by construction, not by hoping the summarizer behaves. Building it forces you to confront the four things that make it real rather than cosmetic — how the child terminates, how you bound three orthogonal resources, what a return payload must contain to be trustworthy without re-reading the source, and what shared mutable state (file ledger, shell registry, approval session) a child may and may not touch.

## 仓库现状

There is no delegation of any kind. `Agent` (`mini_agent/agent.py:18-57`) is a flat single loop: one `self.messages` list seeded with the system prompt (`agent.py:47`), one `self.tools` dict (`agent.py:31`), one `self.max_steps` (`agent.py:32`), one `AgentLogger` (`agent.py:52`). `run()` (`agent.py:294-492`) executes tool calls strictly sequentially in `for tool_call in response.tool_calls:` (`agent.py:404`), awaiting each `tool.execute(**arguments)` inline (`agent.py:436`). Nothing constructs a second `Agent` anywhere except `acp/__init__.py:102`, which builds one per ACP session, not nested.

The single context defense is `_summarize_messages` (`agent.py:153-233`): it keeps the system prompt and every user message, and replaces each user→user span with an LLM-generated prose blob injected as `role="user"` with prefix `[Assistant Execution Summary]` (`agent.py:213-217`). It is lossy, unstructured, and fires only *after* the damage (checked at `agent.py:331`, every step).

Specific gaps this spec has to work around:
- **No FileLedger, no permission/approval system at all.** `grep -rn "ledger"` over `mini_agent/` returns nothing. `EditTool.description` claims "You must read the file first before editing" (`file_tools.py:230`) but `EditTool.execute` (`file_tools.py:254-285`) enforces nothing — it just `read_text` / `replace` / `write_text`. So the question "does the child share the ledger?" is a design question about a seam that does not exist yet; the spec reserves it rather than pretending.
- **Token accounting is an assignment, not an accumulation.** `agent.py:359-360` is `self.api_total_tokens = response.usage.total_tokens` under a comment that says "Accumulate". It reports the *last* call's total, so it cannot be used to measure tree cost. The benchmark needs real accumulators.
- **Logger filenames collide.** `AgentLogger.start_new_run` (`logger.py:30-41`) builds `agent_run_{YYYYmmdd_HHMMSS}.log` and opens it with mode `"w"`. A child spawned in the same wall-clock second silently truncates the parent's log.
- **`BackgroundShellManager._shells` is a class attribute** (`bash_tool.py:108-110`), i.e. process-global and shared between parent and any child.
- **`run()` prints its whole transcript to stdout** (step box `agent.py:334-336`, tool args `agent.py:405-421`, results `agent.py:462-468`) with no verbosity flag.
- There is no per-step hook anywhere in `run()`, so budget enforcement needs one.

## 最小实现

## New package: `mini_agent/subagent/`

### 1. `report.py` — the return contract (~90 LOC)

```python
from typing import Literal
from pydantic import BaseModel, Field

class Evidence(BaseModel):
    path: str                      # workspace-relative
    lines: str | None = None       # "120-160" or "147"
    quote: str | None = Field(None, max_length=200)  # verbatim

class Answer(BaseModel):
    question: str                  # must echo the parent's question verbatim
    answer: str = Field(max_length=600)
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(min_length=1, max_length=4)

class SubagentReport(BaseModel):
    status: Literal["complete", "partial", "failed"]
    answers: list[Answer]
    files_examined: list[str] = Field(max_length=30)
    unresolved: list[str] = Field(default_factory=list, max_length=5)
    next_actions: list[str] = Field(default_factory=list, max_length=5)

def verify_evidence(rep: SubagentReport, workspace: Path) -> SubagentReport
def render_report(rep: SubagentReport, meta: RunMeta, cap_tokens: int = 900) -> str
```

`verify_evidence` runs **server-side, before the report reaches the parent**: for each `Evidence`, resolve `path` under the workspace; if missing → rewrite it as `path + " [PATH NOT FOUND]"`; if `quote` is set, read the cited line range and check the quote appears literally (whitespace-normalised) → otherwise tag `[QUOTE UNVERIFIED]`. Any `Answer` with zero surviving verified evidence is downgraded to `confidence="low"`. This is what turns the contract from a formatting convention into a checked one.

`render_report` emits deterministic compact markdown and enforces the token cap with a fixed degradation ladder (measure with `tiktoken.get_encoding("cl100k_base")`, same encoder as `agent.py:101`): drop `next_actions` → drop `unresolved` → keep 1 evidence per answer → truncate each `answer` to 300 chars → append `[report truncated to fit parent context]`. Deterministic, never an LLM call.

### 2. `budget.py` (~60 LOC)

```python
@dataclass
class Budget:
    max_steps: int = 12
    max_input_tokens: int = 60_000
    timeout_s: float = 180.0
    warn_at: float = 0.75
```
Three budgets because there are three orthogonal resources: one `read_file` can add 32 000 tokens in a single step (`file_tools.py:147`), and one `find /` can hang for minutes without spending a step or a token.

### 3. `readonly_bash.py` (~110 LOC)

`class ReadOnlyBashTool(BashTool)` — overrides `name → "bash_readonly"`, a description that says read-only, and `execute()`: clamp `timeout` to ≤30 s, force `run_in_background=False`, run `_reject(command)` first, then `super().execute(...)`; additionally truncate the returned `content` to 8 000 tokens via `truncate_text_by_tokens` (`file_tools.py:11`) since the base tool truncates nothing.

`_reject(cmd) -> str | None` (returns a reason, or None):
1. Reject if the raw string contains `>`, `>>`, `` ` ``, `$(`, `tee`.
2. Split on `;`, `&&`, `||`, `|` and check **every** segment.
3. Per segment, first token must be in `{ls, cat, head, tail, sed, grep, rg, find, wc, file, tree, awk, sort, uniq, cut, stat, du, basename, dirname, git, diff, nl, xargs? no}` — `xargs`, `python*`, `perl`, `node`, `sh`, `bash`, `env` are always rejected.
4. Per-command flag rules: `sed` must not carry `-i`/`--in-place`; `find` must not carry `-delete`, `-exec`, `-execdir`, `-fprint`, `-fls`; `git` subcommand must be in `{status, log, diff, show, ls-files, blame, rev-parse, cat-file, grep}`.

State plainly in the docstring: **this is a guardrail against a confused model, not a sandbox against a hostile one.** Real isolation is a container/seccomp boundary; that is the scope cut.

### 4. `task_tool.py` (~340 LOC)

```python
_MUTATING = {"write_file", "edit_file", "bash", "bash_output", "bash_kill", "record_note", "task"}
_CHILD_ALLOW = {"read_file", "get_skill", "recall_notes"}

def build_child_tools(parent_tools: dict[str, Tool], workspace: Path,
                      cfg: SubagentConfig) -> list[Tool]:
    out = [t for n, t in parent_tools.items() if n in _CHILD_ALLOW]   # shared by reference
    out.append(ReadOnlyBashTool(workspace_dir=str(workspace)))
    out += [parent_tools[n] for n in cfg.mcp_allow if n in parent_tools]  # explicit allowlist
    assert not ({t.name for t in out} & _MUTATING)
    return out
```
MCP tools are **default-deny**: an MCP tool schema carries no side-effect annotation, so read-only-ness is unknowable from the wire format. Opt in by name in config.

```python
class _ReportBox:
    report: SubagentReport | None = None

class SubmitReportTool(Tool):          # name = "submit_report"
    def __init__(self, box: _ReportBox, questions: list[str], workspace: Path)
    async def execute(self, **kwargs) -> ToolResult
```
`execute` validates with `SubagentReport(**kwargs)`; on `ValidationError` returns `success=False` with pydantic's message so the child gets one in-budget repair attempt. It also checks `[a.question for a in rep.answers] == self.questions` (exact, ordered) and rejects a mismatch. On success it runs `verify_evidence`, stores into `box`, and returns `"Report accepted. Stop now — do not produce further output."`

```python
class TaskTool(Tool):
    name = "task"
    def __init__(self, llm_client, workspace_dir: Path, cfg: SubagentConfig, depth: int = 0)
    def bind_parent(self, agent: "Agent") -> None
    def reset_turn(self) -> None            # resets self._spawns_this_turn = 0
    async def execute(self, description: str, prompt: str,
                      questions: list[str], subagent_type: str = "explore") -> ToolResult
```

**Tool schema** (`parameters`):
- `description` — string, 3–8 words, for the terminal line only.
- `prompt` — string, required. Description text: *"The complete, self-contained instruction. The subagent has NO access to this conversation, the files you have read, or anything you know. Anything it needs must be written here."*
- `questions` — array of 1–5 strings, required. *"The exact questions you need answered. The subagent's report answers these, in order, and nothing else."*
- `subagent_type` — enum `["explore"]`, default `"explore"`.

`execute()` body, in order:
1. Guard: `depth > 0` → error (children never get `task`, this is belt-and-braces); `self._spawns_this_turn >= cfg.max_spawns_per_turn (8)` → error.
2. `budget = Budget(**cfg.budget)`; `run_id = uuid4().hex[:6]`.
3. `box = _ReportBox()`; `tools = build_child_tools(self.parent.tools, ws, cfg) + [SubmitReportTool(box, questions, ws)]`.
4. Build child system prompt (below), then:
   ```python
   child = Agent(llm_client=self.llm, system_prompt=sys_prompt, tools=tools,
                 max_steps=budget.max_steps, workspace_dir=str(ws),
                 token_limit=int(budget.max_input_tokens * 0.9))
   child.logger.start_new_run(tag=f"_sub_{run_id}")
   child.step_hook = make_budget_hook(box, budget, deadline)
   child.add_user_message(prompt + "\n\nQuestions:\n" + numbered(questions))
   ```
5. Cancellation wiring (see below), then `await child.run(cancel_event=child_cancel)` inside `contextlib.redirect_stdout(io.StringIO())`. Dump the captured text into the child's log file.
6. Shape the result and return.
7. `finally:` cancel the linker + watchdog tasks, and terminate any `BackgroundShellManager` ids that appeared during the child's run (diff the id set captured before/after via `BackgroundShellManager.get_available_ids()`, `bash_tool.py:120-123`) — never "kill all", the registry is shared with the parent.

**`make_budget_hook`** (the enforcement, called once per child step before the LLM call):
```python
def hook(agent, step) -> str | None:
    if box.report is not None: return "STOP"
    used_t = agent.tokens_in
    if step >= budget.max_steps - 1 or used_t >= budget.max_input_tokens or now() > deadline:
        agent.tools = {"submit_report": agent.tools["submit_report"]}   # poor-man's tool_choice
        agent.max_steps = step + 2
        return "BUDGET EXHAUSTED. All other tools have been removed. Call submit_report NOW with status=\"partial\" and whatever you have."
    if step >= budget.max_steps * warn_at or used_t >= budget.max_input_tokens * warn_at:
        return f"Budget warning: {budget.max_steps - step} steps and ~{budget.max_input_tokens - used_t} tokens remain. Wrap up and call submit_report."
    return None
```
The tool set is shrunk **in place** because neither client sets `tool_choice` — `AnthropicClient._make_api_request` only sets `model/max_tokens/messages/system/tools` (`llm/anthropic_client.py:68-79`). Removing every other tool is the closest available forcing function, and it needs no client change.

**Result shaping in `execute()`:**
| outcome | returned to parent |
|---|---|
| `box.report` set | `ToolResult(success=True, content=render_report(...))`, ≤900 tokens |
| budget hit, report `status="partial"` | same, `success=True` — a partial answer with evidence is useful; marking it failed makes the parent redo the expensive thing |
| salvage turn produced nothing | mechanically synthesised `SubagentReport(status="failed", answers=[Answer(q, "no answer produced", "low", [...])], files_examined=<paths harvested from the child's `read_file` tool-call arguments>)` — still `success=True`, so the parent learns *what was looked at* |
| LLM error / `RetryExhaustedError` | `ToolResult(success=False, error=f"subagent {run_id} failed after {n} steps: {str(e)[:200]} (log: {child.logger.get_log_file_path()})")` — **one line, never a traceback**; the generic handler at `agent.py:437-452` would otherwise inject a full `traceback.format_exc()` into parent context |
| user cancel | `success=False, error="subagent cancelled by user"` — the parent's own `_check_cancelled()` at `agent.py:441-446` then stops the parent within one tool result |
| deadline | `success=True` with `status="partial"` if the salvage turn landed, else `success=False, error="subagent {run_id} timed out after {t}s"` |

**Cancellation / timeout wiring** — two events, one direction:
```python
child_cancel = asyncio.Event()
linker  = asyncio.create_task(_link(self.parent.cancel_event, child_cancel))  # parent -> child ONLY
watchdog = asyncio.create_task(_fire_after(budget.timeout_s, child_cancel, reason))
result = await asyncio.wait_for(child.run(cancel_event=child_cancel),
                                timeout=budget.timeout_s + 30)   # hard backstop
```
Cooperative first (the watchdog sets the child's event, and `run()` checks it at its existing safe checkpoints — `agent.py:301`, `agent.py:400`, `agent.py:441` — so `_cleanup_incomplete_messages` (`agent.py:73-93`) keeps the child's message list consistent), hard `wait_for` only as a backstop for a wedged tool.

**Child system prompt** — new file `mini_agent/config/subagent_explore_prompt.md`, loaded via `Config.find_config_file`. Must contain, verbatim in spirit:
- "You are a read-only exploration subagent. You cannot write, edit, or run anything that mutates state."
- "You have no memory of the conversation that spawned you and no way to ask a question. If the request is ambiguous, answer the most likely reading and record the ambiguity in `unresolved`."
- "**Nothing you say outside a `submit_report` tool call reaches the caller.** Prose, apologies, and offers of further help are discarded."
- "Every entry in `answers` must carry at least one `evidence` item with a real path and line range you actually read. Quotes are verified against the file; fabricated quotes are flagged in the report you hand back."
- "Do not paste file contents. Quotes are capped at 200 characters."
- Budget line, interpolated: `"You have {max_steps} steps and ~{max_input_tokens} input tokens."`

The string `"Current Workspace"` must **not** appear in this file, so `Agent.__init__` (`agent.py:39-43`) appends the workspace block automatically.

## Changes to existing files (all small)

1. **`mini_agent/logger.py:30-33`** — `def start_new_run(self, tag: str = "")`, filename `f"agent_run_{timestamp}{tag}.log"`. Fixes the same-second `"w"`-mode truncation at `logger.py:38`.
2. **`mini_agent/agent.py:54-57`** — add `self.llm_calls = 0`, `self.tokens_in = 0`, `self.tokens_out = 0`, `self.step_hook: Callable[["Agent", int], str | None] | None = None`, `self.children: list["Agent"] = []`.
3. **`mini_agent/agent.py:358-360`** — replace the assignment with accumulation, keeping `api_total_tokens` for the existing `print_stats` (`cli.py:245`):
   ```python
   if response.usage:
       self.api_total_tokens = response.usage.total_tokens
       self.tokens_in += response.usage.prompt_tokens
       self.tokens_out += response.usage.completion_tokens
   self.llm_calls += 1
   ```
4. **`mini_agent/agent.py:330`** — insert the hook immediately before `await self._summarize_messages()`:
   ```python
   if self.step_hook is not None:
       directive = self.step_hook(self, step)
       if directive == "STOP":
           break
       if directive:
           self.messages.append(Message(role="user", content=directive))
   ```
   (`break` falls through to the max-steps return at `agent.py:488-491`; `TaskTool` reads `box.report`, not `run()`'s string, so that is harmless.)
5. **`mini_agent/agent.py:59-61`** — in `add_user_message`, after the append: `for t in self.tools.values(): getattr(t, "reset_turn", lambda: None)()`.
6. **`mini_agent/cli.py:544`** — new block right after `add_workspace_tools(tools, config, workspace_dir)`:
   ```python
   task_tool = None
   if config.tools.enable_subagent:
       task_tool = TaskTool(llm_client, workspace_dir, config.tools.subagent)
       tools.append(task_tool)
   ```
   (`llm_client` already exists from `cli.py:526`. `add_workspace_tools`'s signature at `cli.py:399` is deliberately left alone so `acp/__init__.py:101` keeps working.)
7. **`mini_agent/cli.py:575`** — immediately after the `agent = Agent(...)` block: `if task_tool: task_tool.bind_parent(agent)`.
8. **`mini_agent/config.py:48-63`** — add `class SubagentConfig(BaseModel)` with `max_steps: int = 12`, `max_input_tokens: int = 60000`, `timeout_s: float = 180`, `max_spawns_per_turn: int = 8`, `mcp_allow: list[str] = []`, `prompt_path: str = "subagent_explore_prompt.md"`; add `enable_subagent: bool = True` and `subagent: SubagentConfig` to `ToolsConfig`, plus one parse line near `config.py:140`.
9. **`mini_agent/config/system_prompt.md`** — one paragraph: use `task` for open-ended search ("where is X", "how does Y work", "which files do Z") whose *intermediate* output you do not need; do it yourself when you need exact bytes to edit.

## 边界情况

1. **The child ends in prose instead of calling `submit_report`.** Obvious-and-wrong: take the child's final assistant text as the result — that is exactly the free-form return you set out to avoid, and in practice the last turn is often "Let me know if you'd like me to dig deeper!" addressed to a user who does not exist. Right: a forced salvage turn in which `agent.tools` is replaced in place with `{"submit_report": ...}` so the model has one legal move, followed by mechanical synthesis (`status="failed"`, `files_examined` harvested from the `path` arguments of the child's own `read_file` calls) if even that fails. Cost of the salvage turn is one LLM call, which is why `max_steps` is set to `step + 2`, not `step + 1`.

2. **The child hallucinates its citations.** Obvious-and-wrong: trust the report, because it validated against the pydantic schema — but the schema only proves it is *shaped* like evidence. Right: `verify_evidence` resolves every `path`, reads the cited line range, and checks the `quote` appears literally; unverifiable evidence is tagged `[QUOTE UNVERIFIED]` and its answer is force-downgraded to `confidence="low"`. This costs one buffered file read per citation and is the difference between a return contract and a return *convention*. It also gives the parent a cheap drill-down key: a verified `path:lines` is something the parent can read directly if it needs the bytes.

3. **Read-only bash enforced by first token.** Obvious-and-wrong: `cmd.split()[0] in ALLOWLIST`. That passes `sed -i 's/x/y/' f`, `find . -name '*.py' -delete`, `find . -exec rm {} \;`, `git checkout .`, `cat a > b`, `python3 -c "open('f','w')"`, and `ls; rm -rf build`. Right: reject redirection and command-substitution characters outright, split on `;`/`&&`/`||`/`|` and validate every segment, apply per-command flag rules (`sed` without `-i`, `find` without `-delete`/`-exec`/`-fprint`, `git` subcommand allowlist), and ban interpreters entirely. And say out loud that this is a guardrail against a confused model, not a sandbox against a hostile one.

4. **Timeout implemented as `asyncio.wait_for` on the parent's cancel event.** Wrong twice over. (a) A shared event means a child timing out also cancels the parent's turn, since `Agent.run` checks the same object at `agent.py:301`/`400`/`441`. (b) `wait_for` cancels the coroutine mid-tool, so a background shell registered in `BackgroundShellManager._shells` — a *class* attribute at `bash_tool.py:108-110`, shared process-wide — is orphaned. Right: a separate child `Event`, a linker task that propagates parent→child only, a watchdog that sets the child event at the deadline so cancellation lands on the existing safe checkpoints (preserving `_cleanup_incomplete_messages`, `agent.py:73-93`), a hard `wait_for(deadline + 30)` backstop, and on cleanup terminate only the shell ids that are new since the child started — never all of them, or the child's cleanup kills the parent's dev server.

5. **The child inherits the parent's `token_limit=80000` and its own compactor fires.** `_summarize_messages` is called every step at `agent.py:331`, in the child too. If it fires inside the child, the child's raw evidence is replaced by lossy prose (`agent.py:213-217`) and the citations it then submits are derived from a summary of a summary — invisible from the outside, because the report still looks well-formed. Right: construct the child with `token_limit = int(0.9 * budget.max_input_tokens)` so the budget's salvage turn always fires strictly before the compactor could. The general rule: two context mechanisms must not overlap in their trigger region, or you cannot say which one owns a failure.

6. **`redirect_stdout` to silence the child.** It works today and only today. `contextlib.redirect_stdout` swaps `sys.stdout` process-wide, not per-task; it is safe purely because the parent's tool loop is sequential (`agent.py:404` awaits each `tool.execute` in turn, `agent.py:436`), so exactly one child exists at a time. The moment anyone wraps that loop in `asyncio.gather`, the redirect starts eating sibling output and the per-child cancel linking gains a second writer. Write that invariant into a comment and assert it in the test rather than discovering it later.

7. **Recursion and fan-out.** Obvious-and-wrong: give the child the same tool list minus the writers, and forget that `task` is in that list — one prompt like "explore thoroughly" then forks a tree that bills the whole account. Right: `task` is in `_MUTATING` so `build_child_tools` filters it, plus a `depth > 0` guard inside `execute()`, plus `max_spawns_per_turn` reset through the `reset_turn()` protocol hooked into `add_user_message` (`agent.py:59-61`) — three independent stops, because the first two are static and the third is the only one that bounds a single runaway turn.

## 怎么证明它有效

Two artifacts. The unit test proves the *contract* holds; the benchmark proves the contract *pays*.

**(a) `tests/test_subagent_isolation.py` — deterministic, no API, ~5 seconds.** A `FakeLLM` implementing `generate(messages, tools) -> LLMResponse` replays a scripted turn list. Script the child to `read_file` a 50 KB fixture and then call `submit_report`. Assert:
1. `len(parent.messages)` grew by exactly 2 (the assistant tool-call message + the tool result) across the whole delegation.
2. The 50 KB fixture's marker string does **not** appear in `json.dumps([m.model_dump() for m in parent.messages])` — this is context isolation stated as an invariant, not a vibe.
3. `tiktoken` count of the tool-result content ≤ 900.
4. `set(child.tools) & _MUTATING == set()` and `"task" not in child.tools`.
5. Budget: a `FakeLLM` that *never* calls `submit_report` produces exactly `max_steps + 1` `generate` calls (the salvage turn), the last call receives a `tools` list of length 1 whose only member is `submit_report`, and the returned report has `status in {"partial","failed"}`.
6. Evidence verification: a scripted report citing `nonexistent.py:1-5` comes back tagged `[PATH NOT FOUND]` with `confidence == "low"`.

**(b) `scripts/bench_subagent.py` — the number, ~35 min wall clock.** Six tasks over this repo as the corpus (no network beyond the LLM), each with a machine-checkable rubric in `scripts/bench_tasks.yaml`: required file citations plus a keyword set. Examples: *"How does message-history summarisation decide what to keep, and what message structure results?"* (must cite `agent.py:153-233`); *"Which tools mutate the filesystem, and where is each registered?"* (`file_tools.py`, `cli.py:399-431`); *"Is the session token counter cumulative?"* (`agent.py:359-360` — ground truth: no, it is an assignment); *"What happens to in-flight messages on Esc?"* (`agent.py:73-93`, `cli.py:718-790`); *"Which config keys control MCP timeouts and where are they applied?"*; *"List every place a log file is opened for writing."* Grading is deterministic string matching over the parent's final answer — no LLM judge.

Two arms, one flag apart, same model, same tasks: **A** = `enable_subagent: false`; **B** = `enable_subagent: true` plus the one delegation paragraph in the system prompt. 3 seeds × 6 tasks × 2 arms = 36 runs.

Reported metrics, all from the accumulators added at `agent.py:54-57` and `agent.py:358-360`, walking `agent.children`:
1. **Parent context growth per task** — `parent._estimate_tokens()` (`agent.py:96`) sampled after every step; report final − initial and the peak. This is the headline; the prediction is a 3–6× reduction on the search-heavy tasks.
2. **Compaction events** — how many runs triggered `_summarize_messages`. The single strongest claim available: arm A fires it on several tasks (a `grep -rn` through the untruncated `BashTool` output plus one 32 KTok `read_file` clears `token_limit=80000` fast); arm B should fire it zero times. Delegation is the preventive strategy; showing the reactive one never has to run *is* the result.
3. **Total billed tokens across the whole tree** (parent + all children). Report it even though it will likely be a wash or slightly worse — there is a fixed ~1.5 KTok spawn tax (child system prompt + tool schemas), measured separately and stated.
4. **Evidence-compression ratio** = bytes of file content the tree examined ÷ tokens added to parent context. Expect 20–60× in B versus ~1× in A. This is the metric that actually isolates what the mechanism does.
5. **Double-work rate** = files appearing in a child's `files_examined` *and* in a later parent `read_file` call. Direct measure of the dominant failure mode.
6. **Pass rate**, reported as counts (e.g. 16/18 vs 17/18), with the explicit honest caveat: at n=18 the claim is "no regression detected", not "no regression".

Output: one `bench_results.md` table plus the raw per-run JSON.

## 深度追问

1. **Why does the child return through a tool call rather than its final assistant message?** Four reasons, in descending order of importance. (1) Size is bounded by construction — free-form return means the parent's growth per delegation is whatever the child felt like writing, which on a bad turn is a 4 KTok prose dump that costs exactly what reading the files yourself would have; you have moved the tokens, not saved them. (2) The schema is enforced by the provider's tool-input JSON Schema, so validation happens before you see it, and a violation is a repairable tool error rather than a parse failure. (3) Termination becomes explicit and separable from `finish_reason` — the flat loop's only stop signal is 'the model emitted no tool calls' (`agent.py:397`), which is indistinguishable from the model giving up. (4) Provenance has somewhere to live, so the parent can drill into `path:lines` instead of re-reading whole files. Rejected alternatives: *asking for JSON in prose* (models fence it and wrap it in commentary; you build a repair loop and you are back to parsing); *a second summariser LLM call over the child's transcript* (adds a call, adds a hallucination surface, and still returns prose with no provenance).

2. **Does the child share the FileLedger and the permission session?** No ledger exists today — `EditTool.description` claims read-before-write at `file_tools.py:230` but `execute` at `file_tools.py:254-285` enforces nothing. The design answer: the ledger must be **shared by reference**, because if the child's reads do not count for the parent, the parent has to re-read every file it wants to edit and the entire saving is refunded. The subtlety that catches people: the ledger must key on `(path, mtime, sha256)`, not on `(path, who_read_it)` — staleness is a property of the file, not of the reader. And there is a genuine hazard the ledger *cannot* fix: the child read the bytes, the parent only ever saw a 600-character summary, so 'this file has been read' is now true of the process but false of the agent doing the editing. That is precisely why `Evidence.quote` exists and is verified — a parent about to edit a region should have a verified verbatim quote of it, or it should read the file itself. Permission/approval state: shared by reference, but the child's callback must **auto-deny**, never auto-approve and never prompt. Prompting surfaces out of order in the parent's terminal with no indication of which subagent asked; auto-approving turns any bug in `build_child_tools` into a silent write. A read-only child that hits an approval gate is a filtering bug and should fail loudly.

3. **Why are subagents sequential, and why is that not the compromise it looks like?** The parent loop awaits each tool call in order (`agent.py:404`, `agent.py:436`); parallelism requires an `asyncio.gather` there, which breaks three things at once: the process-global `redirect_stdout` capture starts eating sibling output, the per-child cancel-event linking gains multiple concurrent writers, and `BackgroundShellManager._shells` — a class attribute at `bash_tool.py:108-110` — becomes shared mutable state across siblings, so the new-shell-id diff used for cleanup no longer identifies whose shell is whose. The point that matters: parallel subagents buy **latency**, not context. The context win is identical either way, because the parent's growth is bounded per-delegation regardless of when the delegations happen. Since the deliverable is the context mechanism, parallelism is the correct thing to cut — and knowing *why* it is orthogonal is the answer, not the fact that you skipped it.

4. **What stops the model from delegating everything, or nothing?** The prompt alone does not, and the interesting part is the failure taxonomy. (a) *Delegating work whose bytes you need* — 'give me the exact body of `_summarize_messages`' comes back as a 200-character quote, the parent then reads the file anyway, and you paid twice; measurable as the double-work rate. (b) *Under-specified prompts* — the model writes a one-line `prompt` that leans on context only the parent has, and the child, with a genuinely empty message list, confidently answers a different question. This is the dominant real-world failure, and it is why the `prompt` parameter description must say in capitals that the subagent sees nothing else, and why `questions` is a separate required array rather than folded into the prose. (c) *Delegating trivia* — spending a 1.5 KTok spawn tax to learn a filename. The honest position is that all three are prompt-sensitive and none are fixed by the mechanism, which is exactly why the benchmark reports total tree tokens and double-work alongside the flattering numbers.

5. **Why three budgets rather than a step count?** Because there are three orthogonal resources and each has a failure that the others do not bound. Steps do not bound tokens: `ReadTool` truncates at 32 000 tokens (`file_tools.py:147`), so two steps can consume 64 KTok, and `BashTool` truncates at nothing at all (`BashOutputResult.format_content`, `bash_tool.py:32-48`), so one `grep -rn` can exceed any of them. Tokens do not bound time: a hung `find` spends neither steps nor tokens while burning the whole wall clock. Time does not bound steps: a fast model can make thirty cheap calls inside the deadline. The second-order point is that budget exhaustion must be *graceful* — a hard abort at the limit throws away everything the child learned, so the design warns at 75% and then spends one final step with the tool set shrunk to `{submit_report}` to convert partial work into a partial answer. A `status="partial"` report with verified evidence still returns `success=True`, because marking it a failure makes the parent redo the expensive thing it just paid for.

6. **How do you know isolation paid off rather than just moved the cost around?** You cannot know it from token totals, and claiming otherwise is the tell that someone read a blog post. Totals usually get slightly *worse* under delegation because of the per-spawn system-prompt-and-schema tax. The three claims that are actually defensible are: peak parent context (a hard bound, not a heuristic), the count of compaction events (arm A destroys history, arm B never has to), and the evidence-compression ratio — bytes examined per token entering the parent. Each of those is a claim about *where* tokens live, which is the thing delegation changes; none is a claim about total spend, which it mostly does not. Pair that with a pass rate reported as raw counts at n=18 and stated as 'no regression detected', not 'no regression'. The reason to be this careful is that a subagent is forty lines of `Agent(...)` — anyone can build one, so the design is worth nothing without a number, and the number is worth nothing if it is the wrong number.

## 前置条件

1. `mini_agent/agent.py:359-360` — `self.api_total_tokens = response.usage.total_tokens` is an assignment under a comment saying 'Accumulate'. Must become a real accumulation (`self.tokens_in/tokens_out/llm_calls`) before any tree-cost measurement is possible. ~5 lines.

2. `mini_agent/logger.py:30-38` — `start_new_run` builds `agent_run_{HHMMSS}.log` and opens it with mode `"w"`, so a child spawned in the same second silently truncates the parent's log. Needs a `tag: str = ""` parameter. ~2 lines.

3. `mini_agent/agent.py:294-492` — `run()` has no per-step hook, so budget enforcement has no place to live. Needs the ~7-line `step_hook` insert at `agent.py:330` (the alternative, subclassing `Agent` and overriding `run()`, duplicates 200 lines and is worse).

## 明确不做

Not building: a real execution sandbox (the read-only bash guard is a first-token/flag/segment allowlist, not a container or seccomp boundary); parallel sibling subagents; more than one `subagent_type`; subagent-to-subagent nesting; streaming the child's progress into the parent's UI beyond a one-line status; MCP read-only auto-classification (default-deny plus a config allowlist instead); a real `FileLedger` (only the seam is reserved); and an LLM judge for the benchmark (deterministic keyword-plus-citation rubrics instead). To an interviewer: "The read-only tool set is a guardrail against a confused model, not a sandbox against a hostile one — that boundary belongs at the process level, and putting it in the tool wrapper would have been security theatre that also happens to be the wrong layer. Parallel subagents I skipped deliberately, because parallelism buys latency and the mechanism I wanted to demonstrate is a context bound, which is identical whether the children run at once or in sequence."

## 代码量

~700 LOC new production code (`report.py` ~90, `budget.py` ~60, `readonly_bash.py` ~110, `task_tool.py` ~340, config ~40, prompt file ~60 lines of prose) + ~30 LOC of edits across `agent.py`, `logger.py`, `cli.py`, `config.py` + ~180 LOC tests + ~230 LOC benchmark harness. ~1,140 total, ~730 of it production.

## 工期

3.5–4.5 days for one person: 1 day for the child runner, budgets and cancellation wiring; 1 day for the return contract, evidence verification and the render/degradation ladder; 0.5 day for the read-only bash guard and its adversarial tests; 0.5 day for the deterministic FakeLLM isolation test; 1–1.5 days for the benchmark harness, the 36 runs, and writing up the numbers honestly.
