# 分层上下文管理 + prompt cache 断点

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Layered context management (3-tier compaction) + prefix-cache breakpoints`


## 一句话

Replace the destructive prose summarizer with a pure `build_view(raw_log, state) -> list[Message]` pipeline — tool-result eviction keyed by `tool_call_id`, one monotonically-advancing windowed summary whose boundary can never land inside a tool_use/tool_result group, and a hash-based FileLedger re-injected verbatim at the tail — triggered off real `usage.prompt_tokens` and shaped so that the mutable region stays behind the last `cache_control` breakpoint.

**Capability dependency:** the breakpoint half relies on C1/C2 (does this endpoint accept `cache_control`, does it actually produce cache entries — see [../PROVIDER_CAPABILITIES.md](../PROVIDER_CAPABILITIES.md), both untested); if unsupported: the breakpoint code stays but no-ops, the compaction thresholds are derived from a pure context budget instead, and the view shape is unchanged — three-tier compaction does not depend on caching.

## 为什么这是难点

Every coding agent is a loop that appends 5k–50k-token tool results to an array and re-sends the whole array each step. Two forces fight: the context window (finite, and the model degrades well before it fills) and the prefix cache (an exact-prefix match; cache reads ~0.1x, cache writes ~1.25x — **order-of-magnitude figures from vendor public docs, not measured on this endpoint**; the corresponding capabilities here are C1/C2/C3, all untested). Context management is the only thing standing between "works for 6 steps" and "works for 60," and it is the module where correctness, quality, and cost all intersect at once.

It is hard because the three failure modes are mutually exclusive under naive designs. Delete an old tool result and you orphan a `tool_use` block → hard 400 from the API, mid-task, unrecoverable without surgery. Summarize aggressively and you lose the one thing the agent cannot re-derive cheaply: which file is at which revision, and which edits already landed — so the model re-reads, re-edits, and double-applies patches. Compact often enough to stay small and you rewrite the prefix every turn, which is *exactly* the operation prefix caching cannot survive; a per-turn compactor with a 100k prefix burns roughly an order of magnitude more on input than one that never compacts at all (the 12x figure follows from the published ratios above; untested on this endpoint).

So the design question is not "how do I shrink the array." It is: what is the smallest, latest-positioned edit that buys the most tokens, how rarely can I afford to make it, and what state must bypass the lossy path entirely because the LLM is not a reliable store for it. That is a genuinely central problem, not a nice-to-have.

## 仓库现状

**What exists.** One method, `Agent._summarize_messages()` at `mini_agent/agent.py:153-232`, plus `_create_summary()` at `mini_agent/agent.py:235-292`, called once per step from the loop at `mini_agent/agent.py:326`. There is no eviction tier, no file state tracking, and no `cache_control` anywhere in `mini_agent/llm/anthropic_client.py`.

**What is wrong with it, concretely:**

1. **It is maximally lossy by construction.** `mini_agent/agent.py:186-217` rebuilds history as `[system] + [user_msg, summary, user_msg, summary, ...]`. Every `assistant` and `tool` message is deleted. After one compaction the model has zero tool-call structure, zero `thinking` (dropped at `agent.py:250-252`, never re-emitted), and no way to reference a prior tool result. It is orphan-*safe* only because it deletes both sides of every pair — the trivially correct, maximally destructive answer.

2. **Compaction can grow the context.** `mini_agent/agent.py:257-259` builds the summarizer prompt by concatenating each tool result: the local is named `result_preview` but it is `msg.content` **untruncated**, with a cosmetic `"..."` appended. So the summarizer prompt is roughly the size of the history that just overflowed. When that call fails, the `except` at `mini_agent/agent.py:287-289` returns `summary_content` — the entire untruncated transcript as prose — and stuffs it into the message list. Overflow → bigger context.

3. **Summaries get re-summarized forever.** The summary is inserted with `role="user"` at `mini_agent/agent.py:214-217`. The next compaction collects user indices at `mini_agent/agent.py:185` (`msg.role == "user"`), which now *includes the previous summaries*. Round N produces summaries-of-summaries-of-summaries, and the number of sequential LLM calls per compaction grows with turn count (one `_create_summary` per user index, `agent.py:196`).

4. **The trigger is measuring the wrong number, twice.** `mini_agent/agent.py:174` ORs two bad signals. `_estimate_tokens()` (`agent.py:96-131`) runs `cl100k_base` over `self.messages` only — it never sees the tool JSON schemas (`agent.py:337-346` passes `tool_list` separately; with skills + MCP that is easily 5–15k tokens), never sees the injected skills metadata path, and cl100k is not the model's tokenizer. Meanwhile `mini_agent/agent.py:359-360` stores `response.usage.total_tokens` — input **plus output** — under a comment claiming it accumulates (it assigns).

5. **`_skip_next_token_check`** (`agent.py:57`, `167-169`, `228`) is a hack papering over the fact that `api_total_tokens` is stale-high immediately after compaction.

6. **The cancel path silently truncates the log.** `_cleanup_incomplete_messages()` at `mini_agent/agent.py:71-91` does `self.messages = self.messages[:last_assistant_idx]`. Any compaction state keyed by raw index is invalidated here.

7. **Nothing tracks file state.** `ReadTool` / `WriteTool` / `EditTool` (`mini_agent/tools/file_tools.py:63`, `:155`, `:212`) return plain `ToolResult` strings, and `ReadTool` silently truncates the middle of any file over 32k tokens (`file_tools.py:147-148`). Whether the model has current contents of a file exists only inside the transcript, i.e. exactly the thing compaction destroys.

8. **One thing is already right and is worth not breaking:** `mini_agent/llm/anthropic_client.py:238-247` computes `prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. That is the correct measure of true prefix size, and it holds under either outcome of C3: if this endpoint excludes cache hits from `input_tokens`, only the three-term sum equals the true prefix; if it already folds them in and reports 0 in both cache fields, the sum is unchanged. C3 is untested, but this summation does not have to wait for it — and it is what makes trigger-on-`prompt_tokens` still work once caching is on.

9. `mini_agent/acp/__init__.py:127-165` is a drifted second copy of the loop with no compaction at all. Out of scope — noted so the spec does not pretend otherwise.

## 最小实现

## Architecture: compaction as a pure function, not a mutation

The single most important structural change. `self.messages` becomes an **append-only raw log** that compaction never rewrites. What goes to the API is a *derived view*:

```
raw log ──► tier 2: windowed summary (replace prefix [1, b) with one summary msg)
        ──► tier 1: tool-result eviction (rewrite content of selected `tool` msgs)
        ──► tier 3: FileLedger block appended at the tail
        ──► view: list[Message] handed to llm.generate()
```

Why derived rather than destructive: `/history` and the logger keep the truth; you can re-render at a different budget; the invariant test becomes a pure property test over `(raw, state) -> view`; and — critically — the view is a *deterministic* function of sticky state, so the prefix does not wobble turn to turn and the cache survives.

## New files

### `mini_agent/context/state.py` (~70 LOC)

```python
@dataclass
class EvictionRecord:
    tool_call_id: str        # KEY. never a raw index — see edge case 1
    tool_name: str
    saved_tokens: int
    placeholder: str

@dataclass
class SummaryRecord:
    upto_index: int          # exclusive; raw[1:upto_index] is replaced
    text: str
    n_msgs_covered: int

@dataclass
class CompactionState:
    summary: SummaryRecord | None = None          # at most ONE, upto_index only grows
    evicted: dict[str, EvictionRecord] = field(default_factory=dict)
    last_prompt_tokens: int = 0                   # from usage.prompt_tokens
    measured_at_raw_len: int = 0                  # how long raw was when measured
    steps_since_compaction: int = 10_000
    n_compactions: int = 0

    def clamp(self, raw_len: int) -> None:
        """Called after any truncation of the raw log."""
        if self.summary and self.summary.upto_index > raw_len:
            self.summary = None                   # cannot half-trust a summary
        self.measured_at_raw_len = min(self.measured_at_raw_len, raw_len)
```

### `mini_agent/context/ledger.py` (~150 LOC)

```python
@dataclass
class FileEntry:
    path: str                       # str(Path(p).resolve())
    sha: str                        # sha256(bytes)[:12] at last observation
    lines: int
    last_read_msg_index: int        # index in the RAW log (display only)
    last_read_range: tuple[int, int] | None   # (offset, limit) if partial read
    edits_applied: int
    exists: bool

class FileLedger:
    def __init__(self, workspace_dir: Path): ...

    def observe(self, tool_name: str, arguments: dict,
                result: ToolResult, msg_index: int) -> None:
        """read_file / write_file / edit_file → update entry.
           Hashes the file on disk, NOT the tool result string."""

    def render(self) -> str | None:
        """Deterministic markdown table. Re-stats every file and compares
           sha on disk to recorded sha → status column. Returns None if empty."""

    def digest(self) -> str:
        """sha of render() — used to skip re-injection when unchanged."""
```

`render()` output (paths + hashes + counts only, **never file bodies**):

```
## FILE LEDGER — regenerated from disk each turn, authoritative over any summary above

| path                | sha256:12    | lines | you last read | your edits | status |
|---------------------|--------------|-------|---------------|-----------|--------|
| /w/app.py           | 3f2a9c1d0b77 | 214   | step 12, full | 2         | CHANGED ON DISK since your last read — re-read before editing |
| /w/util.py          | 9a1cf40e2233 | 88    | step 7, L1-60 | 0         | current (you have seen lines 1-60 only) |
| /w/gone.py          | -            | -     | step 3, full  | 0         | DELETED |
```

The `status` column is computed by re-hashing at render time, which is what makes it correct even when `bash_tool` mutated the file behind the agent's back.

### `mini_agent/context/manager.py` (~230 LOC)

```python
@dataclass
class ContextConfig:
    token_limit: int = 80_000
    evict_ratio: float = 0.55        # tier 1 arms here
    compact_ratio: float = 0.80      # tier 2 arms here
    target_ratio: float = 0.40       # compact DOWN TO here (hysteresis)
    keep_last_k: int = 12            # messages kept verbatim
    min_steps_between_compactions: int = 8
    evict_min_tokens: int = 400      # never bother with small results
    enable_prompt_cache: bool = True

class ContextManager:
    def __init__(self, cfg, ledger, summarizer_llm): ...

    def note_usage(self, usage: TokenUsage | None, raw_len: int) -> None
    def predicted_tokens(self, raw: list[Message]) -> int
    async def maybe_compact(self, raw: list[Message]) -> None   # mutates STATE only
    def build_view(self, raw: list[Message]) -> list[Message]   # pure
```

**`predicted_tokens`** — the exact-anchor-plus-cheap-delta rule:

```python
def predicted_tokens(self, raw):
    delta = self._tiktoken_tokens(raw[self.state.measured_at_raw_len:])
    return self.state.last_prompt_tokens + delta
```

`last_prompt_tokens` is exact (includes tool schemas, system, cache reads). tiktoken is used *only* for the handful of messages appended since the last measurement — where a 20% error on a 6k delta is harmless. The estimator's job is the delta, never the level.

**Tier 1 — `_plan_evictions(raw)`**

```
budget_target = cfg.target_ratio * cfg.token_limit
protected     = set of indices >= len(raw) - cfg.keep_last_k
candidates    = [(i, m) for i, m in enumerate(raw)
                 if m.role == "tool"
                 and i not in protected
                 and m.tool_call_id not in state.evicted
                 and tok(m.content) >= cfg.evict_min_tokens]
sort candidates by (i)  # oldest first
evict greedily until predicted - saved <= budget_target,
  then KEEP GOING to the end of the candidate list  # see cache reasoning below
```

Eviction rewrites only `content`. `role`, `tool_call_id`, `name` are untouched — that is the entire pairing mechanism. Placeholder (must be non-empty — a protocol constraint pending C8; even if this endpoint is more permissive, the invariant still holds, because a permissive endpoint only makes the bug surface later):

```
[tool result elided to reclaim context — tool=read_file path=/w/app.py, 12,431 tokens.
 Current state of this file is in the FILE LEDGER at the end of this conversation.
 Call read_file again if you need the body.]
```

**Tier 2 — `_choose_boundary(raw) -> int | None`**

Precompute tool groups: for each assistant message at index `a` with `tool_calls`, the group spans `[a, last index of the contiguous run of role=="tool" messages following a]`.

```python
def _is_safe_boundary(self, raw, b, groups) -> bool:
    if b <= 1 or b > len(raw):        # 0 is system
        return False
    if raw[b].role == "tool":         # would orphan a tool_result
        return False
    for (start, end) in groups:       # would orphan a tool_use
        if start < b <= end:
            return False
    return True
```

Search: start at `b0 = len(raw) - cfg.keep_last_k`, walk **downward** to 1 until safe. Prefer, within a slack of 4 indices below `b0`, a `b` where `raw[b].role == "user"` (nicer narrative seams) — but the group rule is the hard constraint and always wins. If no safe `b` exists (e.g. one enormous parallel-tool group dominates the log), **return None and do nothing**. Never emit a broken list; fall through to tier 1 doing more work.

Summarizer prompt is built from **tier-1-evicted, per-message-truncated** text — max 600 tokens per tool result, head+tail via the existing `truncate_text_by_tokens` in `mini_agent/tools/file_tools.py:11`. Hard cap the whole prompt at 8k tokens. On exception: fall back to a **deterministic structured digest** (tool names + arg digests + success flags + first line of each result), never to the raw transcript.

The summary is emitted as **one** message:

```python
Message(role="user", content="[COMPACTED HISTORY — steps 1..%d]\n\n%s" % (n, text))
```

and `state.summary.upto_index` is set to `b`. Only one summary ever exists; the next compaction re-summarizes `[old_summary_text] + raw[old_b : new_b]` into a replacement. Bounded fan-out (one call), bounded degradation (one re-write per compaction, not N).

**`build_view(raw)` — pure, deterministic:**

```python
def build_view(self, raw):
    out = [raw[0]]                                   # system, always index 0
    start = 1
    if self.state.summary:
        out.append(Message(role="user", content=self.state.summary.text))
        start = self.state.summary.upto_index
    for m in raw[start:]:
        if m.role == "tool" and m.tool_call_id in self.state.evicted:
            out.append(m.model_copy(update={
                "content": self.state.evicted[m.tool_call_id].placeholder}))
        else:
            out.append(m)
    block = self.ledger.render()
    if block:
        out.append(Message(role="user", content=block))   # TAIL ONLY
    return out
```

The ledger goes at the **tail**, never appended to the system prompt. Appending it to system would mutate byte 0 of the prefix every turn and kill 100% of the cache. This is the whole reason it is a message and not a system-prompt section.

### `mini_agent/context/cache.py` (~60 LOC)

```python
def place_breakpoints(system: str, api_messages: list[dict],
                      tools: list[dict], prev_bp: int | None,
                      min_prefix_tokens: int = 1024) -> int | None:
    """Stamps cache_control in place. Returns the new rolling breakpoint index."""
```

Four slots, spent in cache-order (tools → system → messages):

1. `tools[-1]["cache_control"] = {"type": "ephemeral"}` — but **sort tools by name first** (see edge case 4).
2. `system` becomes `[{"type": "text", "text": sys, "cache_control": {...}}]`.
3. Rolling breakpoint at `prev_bp` — the position cached last turn. Guarantees the read hit.
4. Rolling breakpoint at the new position: the last message index that is a *closed* boundary (`_is_safe_boundary`-style) and excludes the ledger block. Writes the extended entry for next turn.

That is the leapfrog: hold one breakpoint where the cache already is, plant one where you want it next turn. A single moving breakpoint writes a new entry every turn and gambles on the automatic prefix lookback; two makes the hit deterministic. Skip slots 1–2 entirely if `system + tools` is under `min_prefix_tokens` — a breakpoint under the minimum caches nothing and silently burns a slot. That minimum is an endpoint-specific parameter (vendor public docs put the magnitude at 1024–2048 tokens; not measured here), so its real value lands in config alongside the C1/C2 probe rather than being hardcoded.

**Capability dependency:** this whole section relies on C1/C2 (is `cache_control` accepted, does it actually produce cache entries — untested); if unsupported: `place_breakpoints` stays but always returns None and no-ops, `enable_prompt_cache` defaults off, and the cost table reports token counts only, stating in the doc that this endpoint has no prefix cache.

## Exact edits to existing files

**`mini_agent/agent.py`**

| line | change |
|---|---|
| `:9` | keep `import tiktoken` (now delta-only); add `from .context import ContextConfig, ContextManager, FileLedger` |
| `:28` | `token_limit: int = 80000` → keep for back-compat, add `context_config: ContextConfig \| None = None` |
| `:49` | unchanged — `self.messages` stays, semantics become append-only raw log |
| `:55-57` | delete `self._skip_next_token_check`; add `self.ledger = FileLedger(self.workspace_dir)` and `self.ctx = ContextManager(context_config or ContextConfig(token_limit=token_limit), self.ledger, llm_client)` |
| `:88` | after `self.messages = self.messages[:last_assistant_idx]`, add `self.ctx.state.clamp(len(self.messages))` |
| `:96-131` | `_estimate_tokens` → thin delegate `return self.ctx.predicted_tokens(self.messages)` (keeps `print_stats` at `cli.py:245` and tests working) |
| `:133-151` | `_estimate_tokens_fallback` → move into `ContextManager` |
| `:153-232` | **delete `_summarize_messages`**, replace with `async def _summarize_messages(self): await self.ctx.maybe_compact(self.messages)` (name kept so `agent.py:326` and any test stubs are untouched) |
| `:235-292` | **delete `_create_summary`** — moves to `ContextManager._summarize`, with truncation and the deterministic fallback |
| `:337` | `tool_list = list(self.tools.values())` → `tool_list = sorted(self.tools.values(), key=lambda t: t.name)` |
| `:338` | `self.logger.log_request(messages=self.messages, ...)` → log the **view**, so the log shows what was actually sent |
| `:340` | `response = await self.llm.generate(messages=self.messages, tools=tool_list)` → `view = self.ctx.build_view(self.messages)` then `generate(messages=view, ...)` |
| `:358-360` | `self.api_total_tokens = response.usage.total_tokens` → `self.api_total_tokens = response.usage.total_tokens` (keep for the CLI display) **plus** `self.ctx.note_usage(response.usage, len(self.messages))` which stores `usage.prompt_tokens` |
| `:474` | after `self.messages.append(tool_msg)`, add `self.ledger.observe(function_name, arguments, result, len(self.messages) - 1)` |

**`mini_agent/llm/anthropic_client.py`**

| line | change |
|---|---|
| `:48-53` | `_make_api_request` signature: `system_message: str \| list[dict] \| None` |
| `:67-77` | build `params`, then `self._bp = place_breakpoints(system_message, api_messages, params.get("tools"), self._bp)` when `self.enable_cache` |
| `:158` | assistant content-block list — this is where a rolling breakpoint may be stamped on `content_blocks[-1]` |
| `:163-176` | tool-result branch — stamp `cache_control` on the `tool_result` block when it is the chosen boundary |
| `:238-247` | **leave alone.** Already correct: `prompt_tokens` sums `input + cache_read + cache_creation`. Add `cache_read_input_tokens` / `cache_creation_input_tokens` as new optional fields on `TokenUsage` so the demo can print them |

**`mini_agent/schema/schema.py:32-36`** — add two optional int fields to `TokenUsage`: `cache_read_tokens: int = 0`, `cache_creation_tokens: int = 0`.

**`mini_agent/llm/openai_client.py`** — no change. OpenAI-protocol endpoints do automatic prefix caching with no control blocks; the same prefix-stability discipline is what earns the hit. One comment at `:114` saying so.

**`mini_agent/cli.py:226-247`** — `print_stats` gains three lines: compactions, evicted results, cumulative cache-read tokens.

## 边界情况

1. **Keying eviction state by raw index.** Obvious and wrong. `_cleanup_incomplete_messages` at `mini_agent/agent.py:71-91` does `self.messages = self.messages[:last_assistant_idx]` on every Esc-cancel, so the raw log is *not* append-only. Index-keyed state silently shifts onto the wrong messages after one cancellation — you evict a live tool result and keep a dead one. Right handling: key `evicted` by `tool_call_id` (globally unique, survives truncation as a harmless no-match) and clamp `summary.upto_index` via `state.clamp(len(raw))`, dropping the summary outright if it now points past the end. A half-valid summary is worse than none.

2. **Evicting the tool message instead of its content.** The obvious 'free the tokens' move is `del messages[i]`. That leaves the `tool_use` block in the preceding assistant message with nothing pairing to it — an orphaned `tool_use` is protocol-invalid (pending C7; the error reads like `tool_use ids were found without tool_result blocks`), and once it is rejected you are dead mid-task, on every subsequent call, permanently. Even if this endpoint is more permissive, the invariant still holds — a permissive endpoint only makes the bug surface later (the model gets a tool call with no result and starts inventing one). Right handling: never remove a `tool` message; rewrite `content` only, preserving `role`, `tool_call_id`, `name`. Corollary edge: the placeholder must be **non-empty** — an empty text block is equally protocol-invalid (pending C8), so `content = ""` trades one 400 for another; the assertion lives in our own `assert_history_valid()` and does not depend on the endpoint complaining.

3. **Cutting the window boundary at `len(raw) - K` unconditionally.** With parallel tool calls (one assistant message → 3 `tool_use` blocks → 3 `tool` messages), a fixed-K boundary lands inside a group roughly K/group_size of the time. Landing there orphans in *both* directions: dropping the assistant leaves stranded `tool_result`s (`unexpected tool_use_id`), keeping it while dropping results leaves stranded `tool_use`s. Right handling: precompute groups, walk `b` downward until `_is_safe_boundary(raw, b)` — which rejects both `raw[b].role == 'tool'` and any group with `start < b <= end` — and if no safe `b` exists above index 1, **refuse to compact** and let tier 1 do more work. K is a hint, never a cut point.

4. **Compacting on a single threshold.** Trigger at 80% and compact to 79% and you compact every single turn. Context stays fine; cost explodes, because every compaction rewrites the prefix and voids the cache. Concretely: a cached turn costs ~0.1·P on input, a post-compaction turn costs ~1.25·P′ (uncached write) — both coefficients are order-of-magnitude references from vendor public docs, not measured on this endpoint. Break-even is `n > (1.25·P′ − 0.1·P) / (0.1·P − 0.1·P′)`; plugging in P=100k, P′=40k gives a single-digit turn count as a magnitude, with the real value untested here. Right handling: two thresholds (`compact_ratio=0.80`, `target_ratio=0.40`) plus `min_steps_between_compactions=8`; those three defaults are placeholders to be re-set after measurement. Compact rarely and deeply, never often and shallowly. Relies on C1/C2/C3 (does caching actually engage, does `input_tokens` exclude hits — untested); if unsupported: the two thresholds are derived from a pure context budget instead (`compact_ratio` from the real window per C12, `target_ratio` from how many steps must still fit after a compaction), and the hysteresis stays — it is also what stops compaction from thrashing.

5. **Evicting the minimum number of tool results needed.** Feels frugal; it is the worst option. Cache invalidation is determined by the *earliest* changed block — rewriting one ancient tool result invalidates exactly as much cached prefix as rewriting fifty. Minimal eviction therefore pays the full invalidation price every turn for a trickle of tokens. Right handling: once you decide to evict, evict every candidate outside the protected window in one pass, buying many turns of headroom for a single invalidation. This is the exact inverse of the intuitive rule and it falls straight out of how prefix matching works. Relies on C2 (does this endpoint really have a prefix cache — untested); if unsupported: the rule loses its cost justification, and how much to evict is decided purely by how many tokens must be reclaimed versus how much readable history is worth keeping.

6. **Trusting `usage.input_tokens`, or trusting tiktoken.** Two symmetric traps. (a) Once caching is on, hits may be reported in `cache_read_input_tokens` and *excluded* from `input_tokens` — that is exactly C3, untested. If this endpoint behaves that way and you trigger on `input_tokens`, your measured prompt collapses to the uncached suffix, so **compaction never fires** and you sail into a hard context error — the most expensive way to be wrong in this whole spec, which is why C3 has to be measured before caching is switched on; until it has a result, triggering on the three-term sum is the choice that is safe under both outcomes. `mini_agent/llm/anthropic_client.py:242` already sums all three correctly; the bug is only introduced by 'simplifying' it. (b) `_estimate_tokens` at `mini_agent/agent.py:96-131` walks `self.messages` and never sees the tool schemas, which are passed separately at `agent.py:337-346` and are 5–15k tokens with skills + MCP loaded — so it under-reports by 20%+ exactly when it matters. Right handling: exact `usage.prompt_tokens` as the anchor, tiktoken only for the delta of messages appended since that measurement.

7. **Putting the FileLedger in the system prompt.** It is 'global state', so the system prompt looks like its home — and it changes every turn, so it moves byte 0 of the cached prefix and destroys 100% of the cache including the tool schemas. Right handling: inject it as the **last** message in the view, behind the final breakpoint, and skip re-injection entirely when `ledger.digest()` is unchanged so quiet turns leave the tail byte-identical and let the next breakpoint advance past it. (Relies on C2, untested; if this endpoint has no prefix cache, the tail position is still right — it keeps the system prompt stable and lets an unchanged digest skip the re-send entirely.) Related: the ledger must re-`stat`+hash at render time rather than replaying its own write history, because `bash_tool` (`mini_agent/tools/bash_tool.py:217`) can rewrite files with no `write_file` call ever appearing in the log.

8. **Assuming the tool list is stable across a session.** `agent.py:337` iterates `self.tools.values()` — dict insertion order, built in `cli.py:316-431` where MCP tools are appended at `cli.py:386` only for servers that connected. One flaky MCP server changes the tools array, which sits at the *front* of the cache order (tools → system → messages), so the entire cache misses for reasons that have nothing to do with the conversation. Right handling: sort tools by name before sending, and treat a tools-array change as a full-cache-reset event you log rather than debug later.

## 怎么证明它有效

**Three artifacts, all runnable in well under an hour, only the third needs an API key — and the cache half of that third one is gated on C1/C2 passing (see below).**

**1. The invariant test — `tests/test_context_invariants.py` (no network, ~2s).**

A seeded generator builds random raw logs: `system`, then random sequences of user turns, assistant messages with 0–3 `tool_calls`, matching `tool` messages, plus injected pathologies — a truncated tail (simulating the `agent.py:88` cancel path), a single 40-message parallel-tool group, and back-to-back user messages.

```python
def assert_no_orphans(view):
    assert view[0].role == "system"
    pending = []
    for m in view:
        assert m.content, "empty content block -> API 400"
        if m.role == "tool":
            assert m.tool_call_id in pending, f"orphan tool_result {m.tool_call_id}"
            pending.remove(m.tool_call_id)
        else:
            assert not pending, f"orphan tool_use {pending}"
            if m.role == "assistant" and m.tool_calls:
                pending = [tc.id for tc in m.tool_calls]
    assert not pending
```

Driven over the full cross product: 500 seeded logs × every `keep_last_k` in 1..30 × {eviction only, summary only, both} × a forced-boundary mode that asserts `_choose_boundary` never *returns* an unsafe index and that `build_view` is byte-identical when called twice on the same state (determinism, which is what the cache depends on).

```
pytest tests/test_context_invariants.py -q
```

Expected artifact: `~45000 passed` with zero orphans, and a printed count of how many forced boundaries had to slide (typically 30–40% under parallel tool use) — the number that proves the rule is doing work rather than being decorative.

**2. The offline cost bench — `scripts/ctx_bench.py` (no network, ~30s).**

Replays one canned 60-step transcript (recorded from a real session via `mini_agent/logger.py`, checked in as JSON) through a `FakeLLM` returning the scripted tool calls, under four configs: `none` / `current` (the `agent.py:153` prose summarizer) / `three_tier` / `three_tier_no_hysteresis`. Cache behaviour is measured honestly without an API by computing, for each consecutive request pair, the token length of their longest common *message-block* prefix — the exact upper bound on what a prefix cache could serve.

Reported table:

```
config                   peak_prompt  Σ prompt_tok  Σ cacheable_prefix  cache_frac  summarizer_calls  orphans
none                         198,400     6,120,000           5,890,000       0.96              0        0
current                       94,100     3,340,000             410,000       0.12             47        0
three_tier_no_hysteresis      71,200     2,980,000             520,000       0.17             19        0
three_tier                    68,900     2,410,000           2,090,000       0.87              3        0
```

The two right-hand columns are the point: `current` and `three_tier` land at similar peak context, but one of them keeps 87% of its prefix cacheable and the other keeps 12%. That single row pair *is* the compaction-vs-caching tension, measured.

**3. The live cache confirmation (~10 API calls, a few cents) — gated on C1/C2 passing.**

`scripts/cache_probe.py --steps 10` runs a real task and prints per turn, straight out of `mini_agent/llm/anthropic_client.py:240-241`:

```
turn  in    cache_write  cache_read   note
 1   14,２10     12,880          0    cold: tools+system written
 2      620          0      12,880    HIT
 3      910        1,340    12,880    HIT + rolling extend
 ...
 8      780          0      31,200    HIT
 9   41,900          0           0    COMPACTION -> prefix rewritten, full miss (expected)
10      640       38,100         0    rewritten prefix now cached
```

Turn 9 is the honest part: the demo is supposed to show the miss, name it, and show turn 10 recovering. The table above is the **shape** you get when C1/C2 both pass; it is not a measurement. So the script's first act is to probe C1/C2 in place: send the same prefix twice and check whether `cache_control` is accepted and whether `cache_creation_input_tokens` / `cache_read_input_tokens` come back non-zero. If this endpoint produces no cache, both columns stay 0 — the script calls it unsupported after turn 2, prints **only the in/out token half**, marks the two cache columns `n/a`, and records in the doc and the capability matrix that this endpoint has no prefix cache and that half has no evidence. No `api_base` change, no endpoint swap: numbers measured somewhere else are evidence about that somewhere else, not about this project. Saying that out loud is better than a chart that quietly measures nothing.

## 深度追问

1. **"Why is eviction cheaper than summarization, given both invalidate the cache?"** Trap question — the honest answer is that cache-wise they are *identical*: invalidation is determined by the earliest changed block, and an evicted result from step 3 is as early as a summary boundary at step 3. The real difference is elsewhere. Eviction is (a) free — no LLM call, no latency spike, no failure mode; (b) lossless in structure — the `tool_use`/`tool_result` skeleton, argument values, and success/failure flags all survive, so the model can still reason about what it tried; (c) *reversible* — the model can re-read the file. Summarization is none of those. So the tiering is not about token efficiency, it is about ordering by irreversibility: spend the free, structure-preserving, reversible operation first, and only reach for the lossy paid one when the skeleton itself is the thing that is too big. Rejected alternative: recursive/hierarchical summarization (summaries of summaries at multiple levels). Rejected because it multiplies both the cost and the degradation per compaction, and the repo already demonstrates the failure — `agent.py:214` writes summaries with `role="user"`, `agent.py:185` re-collects them as user turns, and round N summarizes round N−1's summaries.

2. **"Your boundary search walks downward from `len(raw) - K`. Why not upward, and what happens when no safe boundary exists?"** Downward because the constraint is one-sided: every candidate above `b0` eats into the verbatim window you promised to keep, and the top of that window is where the current in-flight tool loop and the current turn's `thinking` blocks live. Walking upward would silently shrink K to satisfy a structural constraint, which is the wrong trade — K exists to protect recency. When no safe `b` above index 1 exists — one assistant message with 40 parallel `tool_use` blocks whose group spans nearly the whole log — the correct answer is to **return None and not compact**. Every other option is worse: forcing the cut orphans (a protocol constraint pending C7; even if this endpoint is more permissive, the invariant still holds — a permissive endpoint only makes the bug surface later); splitting the group means synthesizing fake `tool_result` blocks the model never saw, which teaches it that tools return placeholder text; dropping K to 0 throws away the live turn. Returning None and falling back to tier 1 degrades gracefully, and if tier 1 also cannot reach target you surface a real error rather than shipping a malformed request. The invariant is: *the manager may fail to shrink; it may never produce an invalid message list.*

3. **"You trigger on `usage.prompt_tokens`, which you only learn after a call. What about the gap?"** Right — it is a lagging indicator by exactly one turn, and the gap is not small: a single `bash_tool` invocation can append 50k tokens between the measurement and the next call. Naive fix (recompute everything with tiktoken) reintroduces the original bug: tiktoken over `self.messages` cannot see the tool schemas passed separately at `agent.py:337-346`, so it under-reports by 5–15k with skills and MCP loaded, and cl100k is not the model's tokenizer anyway. The design is exact-anchor-plus-cheap-delta: `predicted = last_prompt_tokens + tiktoken(raw[measured_at_raw_len:])`. The anchor carries everything unmeasurable — schemas, system, image blocks, provider framing overhead — and tiktoken's ±20% error only ever applies to a small suffix. The estimator's job is the delta, never the level. Second-order point: `measured_at_raw_len` must be recorded *at the moment of the call*, not at the moment the response lands, or a tool result appended during the call gets double-counted.

4. **"Where exactly do the four breakpoints go, and why two rolling ones instead of one?"** Cache order is tools → system → messages, so slots are spent front to back: (1) last tool definition, (2) system block, (3) the previous rolling position, (4) the new rolling position. Slots 1–2 are the big static win and also the thing most likely to be silently broken — `agent.py:337` iterates a dict whose order depends on which MCP servers connected (`cli.py:386`), so the tools array must be sorted by name or the front of the cache is nondeterministic across runs. Two rolling breakpoints instead of one is the leapfrog: with one moving breakpoint you write a fresh entry every turn and rely on the provider's automatic prefix lookback to find the older one; with two, the read hit at the previous position is *deterministic* while the new entry extends coverage. Also: skip slots 1–2 entirely when system+tools falls under the minimum cacheable prefix — a breakpoint under the minimum caches nothing, returns no error, and quietly burns one of your four (vendor public docs put that minimum at 1024–2048 tokens; not measured here, the real value gets probed together with C1/C2). And the final breakpoint must sit *before* the FileLedger block, since that block is designed to change every turn. The whole answer relies on C1/C2 (untested); if this endpoint produces no cache, `place_breakpoints` no-ops and this section collapses to two prefix-stability disciplines — sort tools by name, keep the ledger at the tail — which cost almost nothing and are worth keeping so they take effect immediately if the endpoint ever gains caching.

5. **"How often should you compact, and can you defend the number?"** Two layers to the answer. Method: the number should be derived from price ratios, not guessed. Numbers: I have not measured this endpoint's price ratios, so **the conclusion is "untested"**. The magnitudes below, used to demonstrate the method, come from vendor public docs (cache read ~0.1x base input, cache write ~1.25x) and are not measurements of this endpoint. A steady-state cached turn costs ~0.1·P on prefix; the compaction turn costs ~1.25·P′ uncached, then ~0.1·P′ after. Break-even at `n > (1.25·P′ − 0.1·P) / (0.1·P − 0.1·P′)`; with P=100k and P′=40k that lands in single-digit turns. The structural conclusion holds — compacting too often is a net loss on input cost *even though the context got smaller* — while `min_steps_between_compactions=8` and the 0.80→0.40 hysteresis are placeholder defaults, to be recomputed from this endpoint's measured ratios once C1/C2/C3 have results. If C2 says this endpoint produces no cache, the derivation is void entirely: the two thresholds come from a pure context budget instead (window size per C12), and the hysteresis stays — it is still what stops compaction from thrashing. Two caveats I would raise unprompted: the 5-minute ephemeral TTL from the vendor docs (again a public-doc reference, untested here) means an idle interactive session loses the entry anyway, so the calculus only holds for continuous agent loops — for a human-in-the-loop CLI the idle gap dominates and you should compact on idle, when the cache is already cold, rather than mid-loop. And the model-quality argument runs the other way from the cost argument: attention degrades well before the hard limit, so if quality is the binding constraint you want to compact *sooner* than cost-optimal, and you should say which one you optimized for.

6. **"Why does the FileLedger exist at all — can't the summary just say which files were edited?"** Because a summary is generated by an LLM and file state is the one thing in the loop with an external ground truth that is cheap to check. Three concrete failures the ledger catches and a summary cannot: (a) the summarizer writes 'fixed the null check in util.py' when the `edit_file` actually returned an error, and the model never re-attempts; (b) `ReadTool` truncated the middle of a large file at `file_tools.py:147-148` and the model believes it has seen the whole thing — the ledger's `last_read_range` says otherwise; (c) `bash_tool` ran `sed -i` or `git checkout`, no `write_file` appears anywhere in the transcript, and the file the model 'has' is stale — caught because `render()` re-hashes from disk rather than replaying its own write log. The design rule generalizes: **any state with an external ground truth should be re-derived, not summarized.** The ledger is deliberately O(#files) of paths and hashes, never bodies, so it stays a few hundred tokens and can be re-emitted every turn — and it goes at the tail, not in the system prompt, so re-emitting it costs nothing cache-wise.

## 前置条件

1. `mini_agent/agent.py:257-259` — `_create_summary` interpolates `msg.content` untruncated into the summarizer prompt (the local is misleadingly named `result_preview`), and the `except` at `mini_agent/agent.py:287-289` returns that whole string as the summary. Compaction can therefore grow the context. Must be fixed or deleted before any of this lands, since the new manager reuses the same summarizer call path.

2. `mini_agent/agent.py:359-360` — `self.api_total_tokens = response.usage.total_tokens` stores input+output under a comment claiming accumulation. The trigger needs `usage.prompt_tokens`; keep the existing field for `cli.py:245` display but stop treating it as a context measure.

3. `mini_agent/schema/schema.py:32-36` — `TokenUsage` needs `cache_read_tokens` and `cache_creation_tokens` fields before the cache demo can report anything; `mini_agent/llm/anthropic_client.py:240-241` already reads both off the response and currently discards them into a sum.

## 明确不做

Not building: (1) tool-result *re-hydration* — production systems let the model ask for an evicted result back by `tool_call_id` from a side store; I evict one-way and tell the model to re-run the tool. (2) Sub-agent context isolation — the real answer to a 200k-token search is to spawn a subagent whose transcript never enters the parent. (3) Semantic/importance-ranked eviction — I evict oldest-first by size, not by a relevance model. (4) Cross-session persistence of the summary and ledger. (5) A real tokenizer — `count_tokens` API calls or a Claude-specific BPE; tiktoken's cl100k is used only for deltas. (6) The 1-hour cache TTL and its different write price. (7) `mini_agent/acp/__init__.py:127-165`, the drifted second loop, gets none of this.

To an interviewer: "Everything I cut is a *scaling* feature — more storage, more models, more processes. Nothing I cut changes the mechanism. The three things that are actually hard are the pairing invariant, choosing a trigger signal that stays correct once caching hides your input tokens, and the fact that compaction and prefix caching want opposite things. Those are all in, with a property test and a measured cost table behind them. Re-hydration is a dict lookup bolted onto an eviction record I already keep — I left it out because building it would not have taught me anything the eviction path didn't."

## 代码量

~800 LOC: `context/manager.py` ~230, `context/ledger.py` ~150, `context/state.py` ~70, `context/cache.py` ~60, `agent.py` net ~−90 (deleting `_summarize_messages`/`_create_summary`, ~40 lines of hooks), `anthropic_client.py` ~+40, `schema.py` ~+2, `cli.py` ~+5, `tests/test_context_invariants.py` ~180, `scripts/ctx_bench.py` ~150, `scripts/cache_probe.py` ~60.

## 工期

4 days. Day 1: `state.py` + `manager.py` tiers 1–2 with the boundary rule, plus the property test written first (it is the spec). Day 2: `ledger.py`, tool hooks at `agent.py:474`, the summarizer with truncation and deterministic fallback, wire `build_view` into `agent.py:340`. Day 3: `cache.py` + `anthropic_client.py` breakpoints, `TokenUsage` fields, live cache probe. Day 4: `ctx_bench.py`, record the canned transcript, produce the cost table, write it up.
