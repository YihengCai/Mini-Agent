# 别人怎么测 agent 循环 —— 调查综述

> 2026-08-24，5 个 agent 各自读真实仓库源码得出；每条结论都带仓库路径。逐项目原始报告在 [surveys/](./surveys/)。
> 用途：给 [../specs/00-measurement-rig_CN.md](../specs/00-measurement-rig_CN.md) 提供外部参照系。**引用前请重新核实**——仓库状态会变。

# How real agent projects test their agent loops — synthesis for Mini-Agent

## 1. Taxonomy: five ways to fake the model

### A. Ordered script (in-process stub)
A queue of pre-built response objects; `popleft()` per call; **the incoming request is ignored**.

- **OpenHands** `openhands.sdk.testing.TestLLM` — `TestLLM.from_messages([...])`, `item = self._scripted_responses.popleft()`, args carry `# noqa: ARG002`. Raises `TestLLMExhaustedError` when drained. `Exception` entries in the list are raised (same contract as `unittest.mock` `side_effect`).
- **mini-swe-agent** ships three parallel stubs *in `src/`*, not `tests/`: `DeterministicModel`, `DeterministicToolcallModel`, `DeterministicResponseAPIToolcallModel` — same loop, three tool-call wire formats. Escape hatches inside the script: `{"raise": exc}`, `/sleep N`, `/warning …`.
- **SWE-agent** `PredeterminedTestModel` (`sweagent/agent/models.py:529`), `InstantEmptySubmitTestModel` (:551).
- **gemini-cli** `packages/core/src/core/fakeContentGenerator.ts` — strict sequential, plus a `nonStrict` mode ("first matching method") explicitly added because *background tasks are non-deterministic*. Exposed as a shipping CLI flag `--fake-responses`.
- **pi** `packages/ai/src/providers/faux.ts` (708 lines) — registered as a real provider (`api: "faux"`), synthesizes the full streaming event sequence, simulates prompt-cache accounting by common-prefix-diffing per `sessionId`.

**Buys:** ~50 lines, zero deps, instant, trivially readable failures.
**Costs:** silently wrong the moment call *order* changes — which is exactly what an out-of-band compaction call does. Asserts nothing about the request unless you bolt that on separately. gemini-cli's `nonStrict` mode is the scar tissue from this.

### B. Predicate / rule router
Script entries are functions of the request, or the fake dispatches on request shape.

- **DeepSeek harness** `packages/core/agent-loop/tests/mock-adapter.ts` — a script array whose entries may be `StreamChunk[]`, **a function of the request**, `'hang'`, `'hang-slow'`, or `{hangAfter: n}`. Records `requests: GenerateOptions[]`.
- **goose** `dummy_api.rs` models a response *taxonomy* as an enum (`Reply`, `ToolCall{require_advertised}`, `ContextLimitError(String)`, `ReplyThenServerError`, `NoChoices`, …) rather than raw payloads.
- **DeepSeek** replay adds `{{fromRequest:<regex>}}` placeholders resolved against the live request at stream time (last match wins, first capture group) — for echoing back randomly minted ids.

**Buys:** survives extra/reordered calls; lets one fake serve heterogeneous callers.
**Costs:** predicates drift into a second implementation of your prompt logic if you let them get clever.

### C. Prompt-hash / content-keyed cassettes
The request *is* the lookup key.

- **goose** `crates/goose/src/providers/testprovider.rs` — `HashMap<String, TestRecord>` keyed by `Sha256(serde_json(messages))`. Deliberately normalizes before hashing (strips `tool_meta`/`_meta`, `is_error: Some(false)`) because *"isn't part of the semantic input the LLM sees."* Recording **panics in CI**: `if std::env::var("GITHUB_ACTIONS").is_ok() { panic!("Test recording is not supported on CI…") }`.
- **OpenHands (historical)** `tests/integration/conftest.py` — the most instructive dead system in this survey. `prompt_{id:03}.log` / `response_{id:03}.log`, dispatched by an **ordered counter**, then the recorded prompt was checked for **normalized string equality** against the live one via `filter_out_symbols()` (strips hostnames, poetry paths, SHA256s, whitespace, non-alphanumerics). One artifact served as both routing key and golden request snapshot. **Deleted from CI in PR #4447 (2024-10-16), "remove integration tests from CI & move them into evaluation."** The normalization list is the honest cost.

**Buys:** prompt/history-construction drift is caught *for free* — a miss means you changed the request.
**Costs:** every non-semantic change (cwd, timestamps, tool ordering) is a false failure until normalized. OpenHands abandoned it; goose kept it by hashing a narrower, normalized object.

### D. Local HTTP server speaking the provider wire protocol
- **codex** — `wiremock::MockServer`, matched on `path_regex(".*/responses$")`; the whole fake-model layer is `codex-rs/core/tests/common/responses.rs` (1,790 lines) of typed SSE event constructors (`ev_function_call`, `ev_output_text_delta`, `ev_reasoning_summary_text_delta`, `sse_failed(id, code, message)`). Also a raw `tokio_tungstenite` WebSocket server and a **per-chunk gated** SSE server (`streaming_sse.rs`, each chunk holds a `oneshot::Receiver<()>`) so tests can prove tools start before `response.completed`.
- **goose** — wiremock; because it's real HTTP it can assert on the outgoing body: `input_contains(needle)`, `system_contains(needle)`, `input_occurrences(needle)`, `uses_model(model)`.
- **OpenHands (current e2e)** — `tests/e2e/mock-llm/scripts/mock-llm-server.py`, port 9999, OpenAI-compatible + SSE, with an **admin control plane**: `/admin/reset`, `/admin/trajectory/register`, `/admin/trajectory/activate`, `/admin/requests`. It wraps `TestLLM` — policy is a library object, transport is a thin shim, tests drive both over HTTP rather than by patching. This is what lets it drive a full Electron/Docker stack.
- **DeepSeek** — `packages/test-support/llm-mock-server` is a *wire-fault* server: socket reset, post-header disconnect, partial disconnect, stall, clean-but-truncated stream, malformed payloads, slow streaming; a `random` mode with a **logged u32 seed** and caller-supplied weights.

**Buys:** exercises your real HTTP client, retry, SSE parsing, timeouts. Crosses process boundaries. Only way to test wire faults.
**Costs:** ports, lifecycles, flakes; you now maintain a partial provider implementation.

### E. Recorded-and-replayed streams (VCR)
- **crush** — `charm.land/x/vcr` + `gopkg.in/dnaeon/go-vcr.v4`. `hyperBuilder` wires a **real** provider through `&http.Client{Transport: r}`; 13 cassettes at `internal/agent/testdata/TestCoderAgent/deepseek-v4/*.yaml` (1.8 MB) storing full request bodies + SSE responses. Determinism scaffolding pins time (`fixedTime` → 1/1/2025), platform (`linux`), cwd so the prompt is byte-stable.
- **opencode** — first-party `@opencode-ai/http-recorder` with a redaction config (`headers`, `allowRequestHeaders`, `jsonFields`). Runs **7 golden scenarios × 11 provider protocol dirs × 2 transports** (`anthropic-messages`, `bedrock-converse`, `openai-responses-cache`, `gemini-cache`, …) — the widest provider-conformance sweep found anywhere.
- **DeepSeek** — cassette *is* a projected persisted session log (`session.jsonl`), reconstructed by grouping `assistant/chunk` events by `(turn, step)`. Non-reconstructable cases (a throw before any chunk; a cancel/hang, which is timing not content) get a sidecar `replay.override.json` with `{patches:[{at, entry}]}` by call index. Nested subagents bind by first-call order; **more live sessions than recorded scripts fails loud**.

**Buys:** real payloads; request pinning for free (cassette match key); wide protocol matrices cheaply.
**Costs:** re-recording needs keys and money; cassettes rot; a prompt change invalidates the corpus. Everyone who does this hard-blocks recording in CI.

### F. Live API only
Universally **opt-in and fenced**, never the default lane:
- codex `live_cli.rs`: two `#[ignore]` fns, header says *"so CI stays deterministic and free."*
- pi: `./test.sh` runs under `env -i` with an ephemeral `HOME` and `PI_OFFLINE=1`; `AGENTS.md` says *"**Never run the full vitest suite directly: it includes e2e tests that activate when endpoint/auth env vars are present.**"*
- mini-swe-agent `tests/test_fire.py` opens with a boxed banner: *"⚠️ THIS TEST FILE SHOULD NEVER BE RUN BY AN AI AGENT."*
- DeepSeek is the loud exception on *policy* — *"We are DeepSeek — do not ration real-API tests"* — but still keeps replay as the default keyless lane and hard-fails preflight if the key is missing, *because the e2e suites self-skip and a missing secret would otherwise pass as "all skipped."*

**Mini-Agent is here today, which is why `tests/test_agent.py` (187 lines) has zero asserts — you can't assert on a live model, so the test degenerated into a smoke run.**

---

## 2. Comparison table

| Project | What fakes the model | Loop-level assertions | Snapshot testing | Sandbox tests | E2E / benchmark | In CI? |
|---|---|---|---|---|---|---|
| **codex** (Rust) | wiremock local HTTP (Responses API) + raw WS server + gated SSE server; no cassettes | **Global** pairing invariant on *every* request; `<turn_aborted>` marker in next request; compaction shape; retry backoff measured in wall-clock (180–220 ms windows); parallel-tool wall-clock proof | `insta`; snapshots **canonicalized+redacted request-body diffs** and 691 vt100 TUI screens | Real Landlock/bwrap/seatbelt/Windows-token denials; runtime capability probe, not blanket skip | No task benchmark at all; "e2e-benchmarks" = `codex --help` startup latency | ✅ `bazel test //...` on macOS+Linux+Windows |
| **DeepSeek harness** (TS) | 4 tiers: MockAdapter stub → wire-fault server → session-log replay → live | `THEOREM: every request rebuilds byte-equal from the session log alone`; ~35 named cancel-race cases; parallel-tool scheduler barriers; fast-check properties | 582 fixture files; **one** scenario pins full system prompt + tool schemas, rest tokenize them | Real seatbelt/landlock/bwrap denials; CI greps vitest's own summary to defeat false-green skips | 136 `*.e2e.ts` vs live API, nightly cron; `BENCHMARK.md` is 3 lines, no harness | ✅ (sandbox lane master-push only) |
| **gemini-cli** (TS) | Scripted `ContentGenerator` + a **response-only** recorder; `--fake-responses` is a shipped flag | **Best cancellation coverage found**; retry/nudge; parallel tool calls arriving in split chunks; degenerate-stream taxonomy | 521 `toMatchSnapshot()`; system prompts + **rendered post-compaction message list** | **Best OS-sandbox tests found**: negative-then-positive with `existsSync === false`; documented per-platform `skipIf` | integration suite ×3 sandbox backends; `evals/` with policy tiers; SWE-bench-ish harness is `workflow_dispatch` only | ✅ integration; evals manual/nightly |
| **cline** (TS) | Handler-level scripted streams; opt-in live tier (1.1 M chars of tool output) | **Best compaction coverage found** — explicit bidirectional tool-pair invariant | — | — | `pass@k` *and* `pass^k` implemented; **workflow deleted** | ⚠️ evals disabled |
| **crush** (Go) | **go-vcr cassettes** of a real provider + tiny Go stubs for cancel tests | Cancel/accept race state machine (6 tests, written from reviewer-caught regressions); parallel tool_call↔result by ID | Golden only for rendered UI diffs | — | — | ✅ `go test -race ./...` on 3 OSes, cassette-backed |
| **goose** (Rust) | wiremock + **SHA256(messages)-keyed** record/replay | Compaction lifecycle; tool lifecycle; steering resume | — | — | 13 recordings × 5 providers; recording panics in CI | ✅ replay-only |
| **opencode** (TS) | First-party HTTP cassette recorder | 7 golden scenarios × 11 protocols × 2 transports | Cassettes w/ redaction | — | — | ✅ |
| **Roo-Code** (TS) | vitest mocks | `validateAndFixToolResultIds` — *repairs* invalid history in the real send path, 794 lines of tests | — | — | vscode-e2e | ✅ |
| **OpenHands** | `TestLLM` ordered queue → mock HTTP server w/ admin API; historical prompt-cassettes (deleted) | `ConversationMemory.process_events()` pairing; `get_unmatched_actions` crash-recovery by `tool_call_id`; stuck detection | — | Container isolation + *policy* parsers; no kernel-denial test | 31 benchmark harnesses (`swe_bench`, `terminal_bench`, …), PR-**label**-triggered; `t*`=blocking, `b*`/`c*`=non-blocking | ✅ unit + Docker runtime; benchmarks label-gated |
| **pi** | `faux` provider in `src/`; live matrix `describe.skipIf`'d | Tool-result **source-order vs completion-order**; execution-mode contract; compaction retry caps | **Zero `.snap` files in the whole repo** | ❌ (sandbox is an untested example extension) | `packages/evals` vs live models, baseline-vs-candidate w/ pass-rate lift; **not in any workflow** | ✅ unit, ubuntu-only |
| **aider** | `coder.send = mock_send` — a closure setting `partial_response_content`. No HTTP, no litellm | `sanity_check_messages()` (roles alternate, last is user) after KeyboardInterrupt and `FinishReasonLength` | One golden text file, `assertEqual`, no library | ❌ | exercism, `pass_rate_1/2`, `percent_cases_well_formed`; **manual** | ✅ bare `pytest`, 5 Pythons × 2 OSes |
| **mini-swe-agent** | 3 scripted stubs (text / toolcall / Responses API) | Step/cost/wall-time limits; format-error counter reset; partial output on timeout | — | Real podman/bwrap/apptainer installed in CI, but no deny-rule assertions | SWE-bench runner not in CI; `--run-fire` for paid tests | ✅ incl. real API keys |
| **SWE-agent** | `PredeterminedTestModel`, `ReplayModel` | Hard-coded `len(a.messages) == 3 → 5 → 7`; exit-condition matrix | — | — | Outside the test suite | ✅ (25 files, ~2.8k lines total) |

---

## 3. The honest coverage gaps — and why they favor you

**Cancellation → history validity: 3 of ~13 projects test it seriously.**

- **gemini-cli** is the only one with a real rollback contract. `packages/core/src/core/geminiChat.test.ts:1700`, `'should roll back the entire multi-turn request including function responses when a continuation stream is aborted/cancelled'`, with the comment *"Verify history has been rolled back entirely to initialHistoryLength (before the original prompt started)!"* Siblings: `'should not fuse the next user message into a cancelled tool response'`, `'should close a dangling tool response restored from a resumed session'`.
- **crush** tests cancel/accept **races** (`TestCancel_AcceptedAfterCancelIsNotPoisoned`, `TestRun_IdleCancelDoesNotPoisonNextPrompt`), asserting persisted role + `FinishReason` counts. The comments name these as reviewer-caught regressions — i.e. written after the bug.
- **codex** persists a `<turn_aborted>` marker and asserts it appears in the *next* request body.
- Everyone else: **aider** reduces it to "roles alternate and the last one is user" (`sanity_check_messages`, ~20 lines, 3 tests). **Roo-Code** has one case (`'should not flush when task is aborted during wait'`). **mini-swe-agent** counts injected interrupt messages. **SWE-agent, opencode, cline, pi**: nothing structural.

**Compaction → message-list validity: better, but the sharp invariant exists in exactly one place.**

- **cline** `sdk/packages/core/src/extensions/context/compaction.test.ts:1781`, `'never lands the agentic cut in the middle of a tool pair'`, written *after* shipping the provider error `"No tool call found for function call output"`. The assertion is the bidirectional check:
  ```ts
  for (const id of toolUseIds) expect(toolResultIds.has(id)).toBe(true);
  for (const id of toolResultIds) expect(toolUseIds.has(id)).toBe(true);
  ```
- **codex** achieves it differently and better: `validate_request_body_invariants` runs inside `impl Match for ResponseMock` — **on every request in every one of ~1,200 core integration tests**, checking `function_call`↔`function_call_output`, `custom_tool_call`↔`custom_tool_call_output`, `tool_search_call`↔`tool_search_output`, with `local_shell_call` as an alternate parent. Nobody writes a compaction-pairing test there because every test *is* one.
- **gemini-cli** golden-snapshots `await contextManager.renderHistory()` — the literal post-compaction message list.
- **Roo-Code** assumes history *will* go invalid and normalizes at send time.
- **opencode** has a **1,975-line** compaction suite with **no tool-pair assertion** (grep-verified). **goose**'s cross-provider compaction scenario `test_context_length_exceeded_error` is **fully commented out** at `scenarios.rs:79–97`, assertion and all, with recordings still checked in and no explanation. **SWE-agent** counts elided observations and calls it a day.

**Also broadly missing:** request-body snapshotting outside cassette users (codex and gemini-cli are the only response-fake projects that snapshot the request at all); OS-sandbox denial tests (gemini-cli essentially alone).

**Read this as opportunity, not as permission to skip.** A 4.1k-LOC Python agent whose test suite asserts *"after cancellation, every `tool_call_id` in an assistant message has exactly one matching `role="tool"` message, and vice versa"* and *"compaction preserves that same invariant"* is doing something gemini-cli, cline and codex each do in one of three forms and that opencode, goose, pi, SWE-agent and aider do not do at all. That is a differentiated portfolio artifact, not catch-up work.

---

## 4. What Mini-Agent should copy — concretely

Grounded in the code I read: `mini_agent/agent.py` is 496 lines; `run()` at :294; `_cleanup_incomplete_messages()` at :73; `_summarize_messages()` at :153; `_create_summary()` at :235; `LLMClientBase.generate(messages, tools=None)` at `mini_agent/llm/base.py:41` with a separate `_prepare_request(messages, tools)` abstract method.

### 4.0 The decision that matters: how a fake response is matched to a request

Your compactor calls, at `agent.py:275`:
```python
response = await self.llm.generate(
    messages=[Message(role="system", content="You are an assistant skilled at summarizing…"),
              summary_msg]
)   # tools omitted → tools=None
```
called **once per summarized round** inside `_summarize_messages()`, which itself runs at the **top of every step** (`agent.py:326`) before the main `generate(messages, tools=tool_list)`.

So a single step can issue `1 + N` calls to the same `generate()`, where `N` is data-dependent (number of user turns with execution history) and only fires above a token threshold. **A flat ordered list is the wrong choice for you.** It is the design that broke gemini-cli badly enough that they added a `nonStrict` mode with the comment *"Useful for non-deterministic background tasks"* — your compactor is exactly that background task.

Prompt-hashing (goose-style) is also wrong here at 4.1k LOC: your summary prompt embeds tool output previews, so the hash changes whenever tool output changes, and you'd spend your first week writing `filter_out_symbols()` — the normalizer that made OpenHands delete the whole system.

**Take the middle: routed ordered queues, dispatched by a cheap predicate on the request.** This is DeepSeek's `MockAdapter` (script entries may be functions of the request) reduced to its minimum useful form.

```python
# tests/fakes/fake_llm.py  (~120 lines total)
Route = Callable[[list[Message], list | None], bool]

def is_compaction(messages, tools) -> bool:
    return tools is None                      # today's only discriminator; assert it

def is_agent_turn(messages, tools) -> bool:
    return tools is not None

class FakeLLM(LLMClientBase):
    def __init__(self, *, turns: list[LLMResponse | Exception],
                 summaries: list[str] | None = None):
        self.queues = {"agent": deque(turns), "compact": deque(summaries or [])}
        self.requests: list[tuple[list[Message], list | None]] = []   # every call, recorded

    async def generate(self, messages, tools=None):
        self.requests.append((deepcopy(messages), tools))
        assert_tool_pairing(messages)                    # ← §4.1, runs on EVERY call
        route = "compact" if tools is None else "agent"
        q = self.queues[route]
        if not q:
            raise FakeLLMExhausted(f"no scripted {route} response; call #{len(self.requests)}")
        item = q.popleft()
        if isinstance(item, Exception):
            raise item                                    # OpenHands TestLLM contract
        return item if isinstance(item, LLMResponse) else LLMResponse(content=item)
```

Three properties worth naming in your writeup:
- **`FakeLLMExhausted`** (OpenHands `TestLLMExhaustedError`; DeepSeek's *"more live sessions than recorded scripts fails loud"*) — an under-scripted test must fail, never silently return a default.
- **Two independent queues** mean compaction tests script `summaries=[...]` and agent tests never think about it — the extra call cannot shift the main sequence.
- **`self.requests`** is the entire assertion surface for §4.2–4.3. codex (`ResponseMock.requests()`), DeepSeek (`requests: GenerateOptions[]`), and OpenHands (`/admin/requests`) all record; gemini-cli's recorder deliberately does not record requests and it is their one acknowledged blind spot.

Add a `Route`-based escape hatch later only if you need it (e.g. a subagent that also passes tools). Do not start there.

### 4.1 A global invariant check on every request — **borrowed from codex**

`codex-rs/core/tests/common/responses.rs:1673`, invoked from `impl Match for ResponseMock` with the comment *"Enforce invariant checks on every request body captured by the mock. Panic on orphan tool outputs or calls to catch regressions early."* Bidirectional:
```rust
for cid in &function_call_outputs {
    assert!(function_calls.contains(cid) || local_shell_calls.contains(cid),
        "function_call_output without matching call in input: {cid}");
}
for cid in &function_calls {
    assert!(function_call_outputs.contains(cid), "Function call output is missing for call id: {cid}");
}
```

Python, ~20 lines, and it is the single highest-leverage thing in this document:
```python
def assert_tool_pairing(messages: list[Message]) -> None:
    called = [tc.id for m in messages if m.role == "assistant" for tc in (m.tool_calls or [])]
    answered = [m.tool_call_id for m in messages if m.role == "tool"]
    assert len(set(called)) == len(called), f"duplicate tool_call ids: {called}"
    assert set(answered) <= set(called), f"orphan tool results: {set(answered) - set(called)}"
    assert set(called) <= set(answered), f"unanswered tool calls: {set(called) - set(answered)}"
```
Every test you ever write becomes a pairing fuzzer for free. Note the third assertion is the one that only holds *between* steps — put it behind a flag if you want to allow the in-flight window, but assert it unconditionally on the compaction route.

### 4.2 The two invariant tests the ecosystem mostly lacks — **borrowed from gemini-cli + cline**

**(a) Cancel-history validity.** gemini-cli's `initialHistoryLength` rollback pattern, applied to `_cleanup_incomplete_messages` at `agent.py:73`:

```python
async def test_cancel_at_step_boundary_preserves_completed_step():
    agent = build_agent(FakeLLM(turns=[tool_turn("write", {...}), text_turn("done")]))
    agent.cancel_event = CancelAfterNCalls(fake, n=1)   # cancel is checked at run():318
    await agent.run()
    assert_tool_pairing(agent.messages)                 # must still hold
    assert agent.messages[-1].role == "tool"            # completed step survives
```
Read `_cleanup_incomplete_messages` carefully first: it finds the last `role == "assistant"` message and truncates from there **unconditionally**, with no check for whether that step was already complete. Cancelling at the top of step N+1 — after step N appended its assistant message *and* all its tool results — therefore deletes a fully valid, fully paired step. That's a defensible design (drop the last turn wholesale, like gemini-cli's rollback) but it is undocumented and untested, and the test above is what turns it into a *decision*. Write the test, decide which behavior you want, and say so in the docstring. That single paragraph is the portfolio.

**(b) Compaction preserves pairing.** cline's `'never lands the agentic cut in the middle of a tool pair'`. Your `_summarize_messages` (`agent.py:186–223`) replaces each `execution_messages` range wholesale with one `role="user"` summary, which keeps pairs balanced *within* a round — good. Assert it anyway, over a generated history: N user turns × M tool calls each, threshold forced low, then `assert_tool_pairing(agent.messages)`.

The more interesting assertion is on the **shape**, and it will probably fail today. Post-compaction your history is `[system, user1, summary(role="user"), user2, summary(role="user"), …]` — consecutive user-role messages. `mini_agent/llm/anthropic_client.py:127–167` emits messages one-for-one with no merging, and separately emits **one user message per tool result** (line 163–167 is inside the per-message loop), so *N* parallel tool calls also produce *N* consecutive user messages on the wire. Write the wire-shape assertion against `_prepare_request` — it's offline, it's free, and it's the kind of finding a fake-LLM rig exists to surface:
```python
def test_wire_payload_has_alternating_roles():
    payload = client._prepare_request(agent.messages, tools=None)
    roles = [m["role"] for m in payload["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles
```
(Whether the Anthropic-compatible endpoint actually rejects this is flagged in §5 — but the invariant is worth pinning regardless.)

### 4.3 Request-body golden snapshots — **borrowed from codex, normalized like OpenHands learned to**

You already have the seam: `_prepare_request(messages, tools)` on `LLMClientBase`. Use **syrupy** (`pytest --snapshot-update`), and copy codex's `format_request_body_diff_snapshot` discipline from `codex-rs/core/tests/common/context_snapshot.rs`:

1. **Canonicalize** — recursively sort every dict's keys, so serialization order can't churn the snapshot.
2. **Redact** — regex UUIDs → `<UUID>`, cwd/tmpdir → `<CWD>`, epoch ms → `<UNIX_MS>`. This is precisely `filter_out_symbols()`, and it is *the* recurring maintenance cost of golden request testing; budget for it up front rather than discovering it.
3. **Pin narrowly.** DeepSeek's rule: *"One ACP scenario (`text-turn`) pins full system-prompt/tool-schema content; other fixtures tokenize it so an edit churns one line."* Have **one** snapshot with the full system prompt and full tool schemas; every other snapshot substitutes `"system": "{{system}}", "tools": "{{tools}}"`. Without this, every prompt tweak rewrites your whole corpus and you will stop trusting the diffs — which is how OpenHands ended up deleting theirs.

Two snapshots earn their keep immediately: the first agent-turn request body, and the post-compaction history rendered through `_prepare_request` (gemini-cli snapshots exactly this — `finalProjection = await contextManager.renderHistory()`).

### 4.4 A run-recorder that writes replayable fixtures — **borrowed from DeepSeek + gemini-cli, gated like goose**

You already have `mini_agent/logger.py` with `log_request` / `log_response` called at `agent.py:340` and `:361`. That is 80% of a recorder.

- Make the log a **JSONL of `{route, request, response}`** and add `FakeLLM.from_log(path)` that rebuilds both queues by route. This is DeepSeek's *"the fixture is a projected persisted session log"* and gemini-cli's `RecordingContentGenerator` — except, unlike gemini-cli, you record the **request too**, so replay can assert it.
- **Hard-block recording in CI**, goose-style (`scenario_runner.rs:178` panics under `GITHUB_ACTIONS`). A missing fixture in CI must fail with *"re-run locally to record"*, never silently record.
- Keep the live suite as a separate, key-gated lane and **fail preflight when the key is absent** — DeepSeek's reasoning: *"the e2e suites self-skip when the key is absent, so a missing/misconfigured secret would otherwise pass as 'all skipped'."* This is the same false-green class as your current `test_agent.py` passing with zero asserts.

### What to skip
- **Sandbox denial tests** — you have no sandbox layer (`BashTool` is 617 lines of subprocess). gemini-cli is nearly alone in doing this; pi ships sandboxing as an *untested example extension*. Not a gap for you.
- **A benchmark harness** — codex has none in-repo; DeepSeek's `BENCHMARK.md` is literally 3 lines; pi's evals and cline's `pass@k`/`pass^k` are both out of CI. Nobody runs task benchmarks on PRs. Don't start there.
- **Cassettes/VCR** — the redaction and re-recording tax only pays off across many providers (opencode: 11 protocols). You have two clients.

**Day-1 ordering:** `FakeLLM` + `assert_tool_pairing` (§4.0–4.1) → the two invariant tests (§4.2) → one request snapshot (§4.3) → recorder (§4.4). Steps 1–2 are ~250 lines and already put you ahead of most of this table on the one thing that matters.

---

## 5. Unverified / could not confirm

**From my own reading of Mini-Agent (this session):**
- Whether the Anthropic-compatible endpoint actually **rejects** consecutive same-role messages. I verified from source that `anthropic_client.py:127–167` emits messages 1:1 with no merging and one user message per tool result, and that `_summarize_messages` produces consecutive `role="user"` entries. The claim that this 400s is from model knowledge, not fetched from Anthropic's docs in this session. The *shape* assertion in §4.2b is worth writing either way; verify the API contract before calling it a bug.
- I read `agent.py` selectively (lines 56–100, 153–300, 294–420) and did not read the tool-execution tail (:420–496), `cli.py` (838 lines), or `retry.py`. Claims about cancellation checkpoints cover the two I saw (`run():318`, `:395`); there may be others.
- `_summarize_messages`'s behavior when the last round is mid-execution: I reasoned it can't be, because compaction runs at the top of a step when history is balanced. Not empirically verified — that's a test to write, not a fact to assert.

**Carried forward from the surveys, unresolved:**
- **OpenHands `get_mock_response` no-match path** — two reads of the same blob disagreed (`raise SecretExit(...)` vs. print-diff-and-return-`None`). WebFetch's summarizer refused verbatim reproduction on copyright grounds, so all `conftest.py` detail is Q&A over the file, not raw source. Re-read `tests/integration/conftest.py` @ `0a03c802f5` if the exact semantics matter.
- **crush's cassette matcher** — `charm.land/x/vcr` is not vendored; whether it matches on request **body** or only URL+method was inferred from cassette contents + go-vcr convention. The "request pinned for free" claim for crush (and for opencode's `@opencode-ai/http-recorder`, same caveat) is inference.
- **opencode's compaction suite** — "no tool-pair assertion" is grep-based over 1,975 lines using an Effect test wrapper that defeated case-name enumeration; not an exhaustive read.
- **goose's commented-out `test_context_length_exceeded_error`** — verified commented out at `scenarios.rs:79–97` with recordings still checked in; **no comment explains why**. Motive unknown.
- **Where OpenHands' deleted prompt-cassettes landed** — PR #4447 says "moved into evaluation," but `evaluation/integration_tests` does not exist at tag `0.62.0`. Possibly they simply died.
- **Line counts** for DeepSeek, pi, cline, Roo-Code are byte-size-derived from git trees, not counted. File counts and byte totals are exact.
- **CI green-ness** — every project's CI claim reads workflow *definitions*, not run history. gemini-cli's `ci.yml` job matrix specifically was not read, so "sandbox tests run on every PR" for gemini-cli is unconfirmed (codex's Bazel matrix and crush's 3-OS matrix were read directly).
- **Repo pinning** — only codex is pinned to a SHA (`c9b19deb09`). The `others` survey captured no SHAs; findings reflect default-branch HEAD as of 2026-08-24. Note `sst/opencode` now redirects to `anomalyco/opencode` (branch `dev`), and `All-Hands-AI/OpenHands` `main` is no longer the Python monolith — the Python agent moved to `OpenHands/agent-sdk`, and the layout described above lives at tag `0.62.0`.
- **DeepSeek benchmark provenance** — nothing in `deepseek-ai/deepseek-harness` implements SWE-bench or Terminal-Bench; which scaffold produced their published numbers was not established.