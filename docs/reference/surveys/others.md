## Method / provenance

All findings below were verified by downloading each repo's default-branch tarball from `codeload.github.com` on **2026-08-24** and reading the actual files. Local extraction root: `/private/tmp/claude-501/-Users-flame-Work-Mini-Agent/bf51f4d0-dbbc-47c7-b289-761931308624/scratchpad/repos/`. `git clone` was blocked by the proxy (`CONNECT tunnel failed, 503`); `gh` is not installed; unauthenticated `api.github.com` worked until rate-limited. Paths below are repo-relative (upstream-canonical).

Note: **`sst/opencode` has moved to `anomalyco/opencode`** (default branch `dev`). Verified via `curl -L` on `https://github.com/sst/opencode` → `url_effective: https://github.com/anomalyco/opencode`.

---

# The two questions you cared most about

## Cancellation / interrupt: history validity after an abort — **YES, three real implementations**

**1. `google-gemini/gemini-cli` — the strongest, most direct match.**
`packages/core/src/core/geminiChat.test.ts` (4,857 lines) contains a cluster of history-rollback tests:

- `'should roll back the un-responded user turn from history when the stream is aborted/cancelled'` (line 1603) — aborts mid-stream, asserts `chat.agentHistory.length` returns to `initialHistoryLength`.
- `'should roll back the entire multi-turn request including function responses when a continuation stream is aborted/cancelled'` (line 1700) — this is the important one. Turn 1 completes normally (history = +2). Turn 2 sends a `functionResponse` continuation that aborts mid-stream. Assertion: history rolls back **all the way past the original prompt**, not just the aborted turn:
  ```ts
  // Verify history has been rolled back entirely to initialHistoryLength (before the original prompt started)!
  expect(chat.agentHistory.length).toBe(initialHistoryLength);
  ```
- `'should roll back the un-responded user turn from history when stream consumption is broken out of early'` (1647) — covers `break` out of the async iterator, not just a thrown abort.
- Sibling invariants: `'should not fuse the next user message into a cancelled tool response'` (1108), `'should close a dangling tool response restored from a resumed session'` (1171), `'should restore the lastPromptTokenCount baseline on history rollback'` (1315), `'should sync the chat recording service on history rollback'` (1553).

**2. `charmbracelet/crush` — cancellation as a concurrency state machine.**
`internal/agent/dispatch_cancel_test.go` (447 lines) + `internal/server/agent_cancel_test.go` (227 lines). This does not test "history after abort" so much as **cancel/accept race correctness**, with the persisted message list as the assertion surface. Every test ends by listing messages and checking role + `FinishReason`:

- `TestCancel_AcceptedAfterCancelIsNotPoisoned` — a sequence high-water mark so one cancel covers exactly the prompts accepted-but-not-yet-active at cancel time; a prompt accepted *after* the cancel must run normally. Asserts 6 messages, 1 `FinishReasonEndTurn`, 2 `FinishReasonCanceled`.
- `TestRun_IdleCancelDoesNotPoisonNextPrompt` — Escape on an idle session must not leak a pending cancel into the next prompt.
- `TestRun_NormalCompletionClearsStalePendingCancel`, `TestRun_PrepareStepDrainSkipsQueuedOnPendingCancel`, `TestRun_CancelOnEntryPublishesRunComplete` (regression: a `RunID` caller blocking on `RunComplete` would hang forever on an immediately-canceled accepted run).

The comments name these as review findings and reviewer-caught regressions, i.e. these tests were written *because* the races bit them.

**3. `RooCodeInc/Roo-Code` — one targeted case.**
`src/core/task/__tests__/flushPendingToolResultsToHistory.spec.ts`: `'should not flush when task is aborted during wait'`. Roo's `apps/cli/src/commands/cli/__tests__/cancellation.test.ts` (104 lines) is *only* error-string classification (`isCancellationLikeError`, `isStreamTeardownLikeError`) — no history assertions.

**Weaker/partial:**
- `SWE-agent/mini-swe-agent` — `tests/agents/test_interactive.py:263` `_test_interruption_helper` monkeypatches `agent.query` to raise `KeyboardInterrupt` on call 1, then asserts the run still reaches `exit_status == "Submitted"` and that **exactly one** interrupt message was injected into `agent.messages`. Message-count-and-substring, not structural validity.
- `cline/cline` — `sdk/packages/core/src/extensions/context/compaction.test.ts:1576` `'does not fall back to basic compaction when agentic compaction is cancelled'` (cancellation × compaction interaction).
- `block/goose` — `crates/goose/src/agents/state_machine/tests/steering_lifecycle.rs:79` `cancellation_preserves_queued_steering_for_resume`.

**Nobody found:** `SWE-agent/SWE-agent` has no cancellation test. `anomalyco/opencode`'s abort coverage is in tools/transport (`test/tool/shell.test.ts`, `test/cli/run/stream.transport.test.ts`), not in session history.

## Context compaction: forcing truncation and asserting the message list is still valid — **YES, and cline has exactly the test you want**

**1. `cline/cline` — the single best match in the ecosystem.**
`sdk/packages/core/src/extensions/context/compaction.test.ts` is **4,377 lines** and is essentially a specification of tool-pair-atomic truncation. Verbatim test names:

- `'never lands the agentic cut in the middle of a tool pair'` (line 1781). The comment names the real bug:
  > Repro for the "No tool call found for function call output" provider error: findCutIndex used to walk back by token budget and could land between an assistant tool_use and its matching user tool_result, leaving the tool_result in the preserved tail while the tool_use was folded into the summary.

  The assertion is the exact bidirectional invariant:
  ```ts
  // Either both halves of the pair are in the preserved tail, or both
  // are folded into the summary. Never one without the other.
  for (const id of toolUseIds) expect(toolResultIds.has(id)).toBe(true);
  for (const id of toolResultIds) expect(toolUseIds.has(id)).toBe(true);
  ```
- `'drops the latest turn's tool pair atomically when over budget'` (427)
- `'removes older tool pairs atomically while preserving a newer pair'` (458)
- `'treats multi-tool assistant turns as one atomic group in basic compaction'` (500)
- `'removes matching tool results when basic compaction removes an assistant tool use'` (517)
- `'does not re-fold the output of an earlier compaction'` (875), `'re-compacts a projection that starts with a compaction summary'` (2000)
- `'compacts a single-task tool loop by cutting at an assistant boundary'` (1886) — regression for auto-compaction permanently skipping in the VS Code host.

**2. `RooCodeInc/Roo-Code` — repairs invalid history at send time, and tests the repair exhaustively.**
`src/core/task/__tests__/validateToolResultIds.spec.ts` (794 lines) tests `validateAndFixToolResultIds`, which is called in the **real send path** (`src/core/task/Task.ts:1004` and `:1082`, plus `src/core/webview/ClineProvider.ts:3019`). Cases: fix mismatched `tool_use_id` by position; filter orphaned `tool_result`s; dedupe `tool_result`s with identical valid IDs ("terminal fallback scenario"); **synthesize missing `tool_result`s** when tool_uses outnumber tool_results. Philosophically different from cline: Roo assumes history *will* go invalid and normalizes on the way out.

Separately, `src/core/context-management/__tests__/truncation.spec.ts` (431 lines) — `describe("Non-Destructive Sliding Window Truncation")` — Roo tags messages with `truncationParent` rather than deleting them, and `getEffectiveApiHistory()` filters. Tests cover rewind-past-truncation restoring hidden messages, orphaned-tag cleanup when a marker is deleted, and multiple stacked truncations. Plus 3,155 lines across `src/core/condense/__tests__/` (incl. `nested-condense.spec.ts`, `rewind-after-condense.spec.ts`).

**3. `google-gemini/gemini-cli` — golden-snapshots the post-compaction message list, plus a subprocess E2E.**
- `packages/core/src/context/system-tests/lifecycle.golden.test.ts` (296 lines) + `simulationHarness.ts`. `getGoldenState()` returns `{ tokenTrajectory, finalProjection, baseUnits }` where `finalProjection = await contextManager.renderHistory()` — i.e. **the actual message list that would be sent**, snapshotted via vitest `toMatchSnapshot()` into `__snapshots__/lifecycle.golden.test.ts.snap` (273 lines, UUIDs normalized to `<UUID>`). Scenarios: "Organic Growth with Huge Tool Output & Images", "Node Distillation of Large Historical Messages", "Async-Driven Background GC via State Snapshots".
- `hysteresis.test.ts` — asserts compaction is *blocked* when the deficit is under `coalescingThresholdTokens`, and fires above it.
- `packages/core/src/context/chatCompressionService.test.ts` (900 lines) — `describe("Reverse Token Budget Truncation")`: `'should truncate older function responses when budget is exceeded'`, `'should use high-fidelity original history for summarization when under the limit, but truncated version for active window'`, `'should return FAILED if new token count is inflated'`.
- **E2E**: `integration-tests/context-fidelity.test.ts` (287 lines, 300s timeout) — spawns the real CLI 12+ times with `experimental.stressTestProfile: true` ("Lowers thresholds to trigger GC easily"), 900 random chars per turn, forces GC, then `--resume latest` and asserts the rendered context asset extracted from the trace log is **identical turn-for-turn across the resume**, including that synthetic (summary) turn IDs are stable.

**4. `block/goose` — good unit coverage; the cross-provider compaction scenario is commented out (see §6).**
`crates/goose/src/agents/state_machine/tests/compaction_lifecycle.rs` (470 lines): `proactive_and_manual_compaction_continue_with_replaced_usage`, `a_context_error_compacts_and_the_session_survives_a_failed_retry`, `repeated_context_errors_stop_compacting`, `text_that_looks_like_a_context_error_does_not_compact`, `context_owning_provider_has_no_compaction_operation`, `a_failed_compact_command_reports_the_error_and_keeps_working`. Asserts on `history_replacements()` counts and on the outgoing request: `assert!(api.calls().last().unwrap().input_contains("<compaction>"))`.

**5. `anomalyco/opencode` — big compaction suite, no tool-pair invariant found.**
`packages/opencode/test/session/compaction.test.ts` is 1,975 lines (`session.compaction.isOverflow` / `.create` / `.prune` / `.process`). Grepping the file for `orphan|tool_use|toolCallID|pair|dangling` returns only reasoning-delta orphan handling and `reason: "tool-calls"` finish reasons — **no assertion that pruning preserves tool_use/tool_result pairing**. Notable gap given the suite's size.

**6. `SWE-agent/SWE-agent` — truncation tested, validity not.**
`tests/test_history_processors.py` (40 lines total) — `test_last_n_observations` loads a **recorded `.traj` file** and asserts the count of elided observations equals `total - 3 - 1`. Pure counting; no structural check.

---

# Per-project detail

## `charmbracelet/crush` (Go) — 215 `_test.go` files, ~50,600 lines

**Model faking: both VCR cassettes and hand-written stubs.**
- **Cassettes.** `go.mod` pins `charm.land/x/vcr v0.1.1` and `gopkg.in/dnaeon/go-vcr.v4`. `internal/agent/common_test.go:42` defines `type builderFunc func(t *testing.T, r *vcr.Recorder) (fantasy.LanguageModel, error)`; `hyperBuilder` wires a real `openaicompat` provider at `https://hyper.charm.land/v1` through `&http.Client{Transport: r}`. So **the real agent loop runs against a real provider wire protocol, recorded once and replayed in CI.**
- **Cassette contents = the request body.** 13 cassettes at `internal/agent/testdata/TestCoderAgent/deepseek-v4/*.yaml` (1.8 MB) store the full outgoing JSON `body` (system prompt, messages, model, `max_tokens`, `stream: true`) plus the complete SSE response stream. This is *de facto* request-payload snapshotting — go-vcr matches requests to select an interaction, so a change in the constructed request breaks replay.
- **Determinism scaffolding**: `coderAgent()` in `common_test.go:126` pins time (`fixedTime` → 1/1/2025), platform (`linux`), and working dir so the prompt is byte-stable across runs.
- **Stubs.** `dispatch_cancel_test.go` defines `finishStreamModel`, a minimal `fantasy.LanguageModel` yielding `TextStart/TextDelta/TextEnd/Finish` parts — explicitly documented as "enough to drive sessionAgent.Run through PrepareStep and a clean completion **without a recorded provider cassette**."

**Loop assertions.** `internal/agent/agent_test.go` (1,065 lines) has 13 cassette-backed subtests, one per tool. The `"parallel tool calls"` subtest is the pairing test: prompts for two parallel calls, then asserts `len(toolCalls) >= 2`, both `GlobToolName` and `LSToolName` present, captures each `tc.ID`, and matches `ToolResults()` back by `tr.ToolCallID`.

**Golden.** `github.com/charmbracelet/x/exp/golden` — `golden.RequireEqual` used only for **rendered UI output** (`internal/ui/diffview/diffview_test.go`, `udiff_test.go`), not for request bodies.

**CI.** `.github/workflows/build.yml` — matrix `[ubuntu-latest, macos-latest, windows-latest]`, runs `go build -race ./...` then `go test -race -failfast ./...`. The cassette-replayed agent tests run on every PR on all three OSes (`TestCoderAgent` self-skips on Windows: `t.Skip("skipping on windows for now")`).

**Sandbox.** No OS-sandbox enforcement tests. `internal/permission/permission_test.go` etc. are policy-logic only.

## `google-gemini/gemini-cli` (TS, vitest) — 783 test-named `.ts` files, ~306,900 lines; 120 `.snap` files; 521 `toMatchSnapshot()` calls

**Model faking: scripted responses with a record/replay pair, at the `ContentGenerator` seam (not HTTP).**
- `packages/core/src/core/fakeContentGenerator.ts` (164 lines) — `FakeContentGenerator` returns canned `GenerateContentResponse`s in strict sequential order, or `nonStrict` (first matching method) "Useful for non-deterministic background tasks". Loaded from a JSONL file via `FakeContentGenerator.fromFile()`, wired to the **shipping CLI flag `--fake-responses`** (`packages/cli/src/config/config.ts`, documented in `docs/reference/configuration.md`).
- `packages/core/src/core/recordingContentGenerator.ts` (125 lines) — wraps a real generator, appends `{method, response}` JSONL. Records **responses only** ("only the 'interesting' bits"; it keeps `candidates` + `usageMetadata`). **It does not record the request** — so unlike crush/opencode, replay does not pin the outgoing payload.
- ~50 `.responses` fixture files checked into `integration-tests/` (e.g. `hooks-system.compress-auto.responses`, `concurrency-limit.responses`, `api-resilience.responses`).

**Loop assertions** (`geminiChat.test.ts`, 4,857 lines) — beyond the rollback tests above:
- Parallel tool-call streaming assembly: `'repro: should not overwrite parallel tool calls when they arrive in separate streaming chunks'` (629), `'repro: should not collide when multiple tool calls with the same name arrive in the same chunk'` (685).
- Retry policy: `'should yield a RETRY event when an invalid stream is encountered'`, `'should retry when no tool call and empty response text, and throw InvalidStreamError after exhausting retries'`, `'should append nudge message to systemInstruction on retry when InvalidStreamError occurs'`, `'should retry when finishReason is MALFORMED_FUNCTION_CALL'`.
- Degenerate-stream taxonomy: `InvalidStreamError` with types `MAX_TOKENS_EXCEEDED`, `THINKING_ONLY_RESPONSE`, plus zero-width/invisible-character-only and HTML/Markdown-comment-only responses.
- `geminiChat_network_retry.test.ts` is a separate file.

**Snapshotting.** 521 `toMatchSnapshot()`. Two kinds that matter: **system prompts** (`packages/core/src/core/prompts.test.ts`, ~20 snapshot calls) and **rendered context / message list** (`context/system-tests`, above). UI is snapshotted heavily too (`packages/cli/src/ui/**/__snapshots__`, e.g. `ToolGroupMessage.compact.test.tsx.snap`). **No test snapshots the literal HTTP request body.**

**Sandbox — the best OS-level sandbox testing found in this sweep.**
`packages/core/src/services/sandboxManager.integration.test.ts` (1,259 lines) spawns **real commands through the real sandbox** and asserts they actually fail:
- `'prevents out-of-bounds access'` → `assertResult(result, sandboxed, 'failure')`
- `'supports dynamic permission expansion'` → attempt 1 fails **and** `expect(fs.existsSync(testFile)).toBe(false)`; attempt 2 with `policy: { allowedPaths: [tempDir] }` succeeds and the file exists. That negative-then-positive pair is what makes it a real enforcement test rather than an error-string check.
- `'protects forbidden paths from writes'`, `'protects forbidden directories recursively'`, `'prioritizes denials over allowances'`, `'prevents creation of forbidden files'`, `'scrubs sensitive environment variables'`, plan-mode transitions, read-only vs YOLO mode.
- **Documented platform skips**, e.g.:
  > `// Windows icacls does not reliably block read-up access for Low Integrity processes, so we skip read-specific assertions on Windows. The internal tool architecture prevents read bypasses via the C# wrapper and __read.`
  → `it.skipIf(Platform.isWindows)('protects forbidden paths from reads', ...)`
- macOS-specific setup detail: temp dirs are created in `process.cwd()` rather than `os.tmpdir()` "to avoid the seatbelt profile's global os.tmpdir() whitelist."

Additionally the **whole integration suite is run under three sandbox backends** (`package.json`): `test:integration:sandbox:none` / `:docker` / `:podman`, and `test:integration:all` chains all three.

**E2E / benchmark layer — the most developed of any project here.**
- `integration-tests/` — 114 entries, 9,543 lines of `.ts`, driven by `TestRig` (`packages/test-utils/src/test-rig.ts`) which spawns the real CLI binary as a subprocess. Includes `ctrl-c-exit.test.ts`, `checkpointing.test.ts`, `flicker.test.ts`, `concurrency-limit.test.ts`, `api-resilience.test.ts`, `context-compress-interactive.test.ts`.
- `evals/` — 37 `*.eval.ts` behavioral evals against **live models**. Nondeterminism is handled by an explicit three-tier policy in `evals/test-helper.ts:49`: `type EvalPolicy = 'ALWAYS_PASSES' | 'USUALLY_PASSES' | 'USUALLY_FAILS'`. `runEval()` (line 358): `USUALLY_PASSES`/`USUALLY_FAILS` are `it.skip`'d unless `RUN_EVALS=1`; `USUALLY_FAILS` runs as `it.fails(...)` — an expected-failure marker for incubation candidates. Also 3× retry-then-SKIP on 500/503 (`test-helper.test.ts`).
- `evals/README.md` explicitly distinguishes its layer from benchmarks: *"They are also distinct from broad industry benchmarks (like SWE-bench)."* A separate SWE-bench-style harness does exist — `.github/workflows/eval.yml` runs in a pinned container `ghcr.io/google-gemini/gemini-cli-swe-agent-eval@sha256:cd5edc4…`, trigger is **`workflow_dispatch` only** (manual). Plus `evals-nightly.yml`, `eval-pr.yml`.
- Also: `memory-tests/` + `perf-tests/` with checked-in baselines (`test:memory:update-baselines`, `test:perf:update-baselines`), and a `deflake.yml` workflow / `npm run deflake` harness that re-runs integration tests with `--retry=0` to detect flakes.

## `cline/cline` (TS, vitest) — 739 test-named `.ts` files, ~219,100 lines

**Model faking.** Handler-level mocks: `createHandlerMock.mockReturnValue({ createMessage: vi.fn(() => streamChunks([...])) })` — scripted stream chunks, no HTTP layer. Plus an **opt-in live tier**: `sdk/packages/core/src/extensions/context/compaction.live.test.ts` (345 lines) gated on `process.env.CORE_LIVE_COMPACTION_TESTS === "1"`, reads real provider credentials from the user's stored settings file, 120s default timeout, and stress-tests with `CORE_LIVE_COMPACTION_TOOL_OUTPUT_CHARS` defaulting to **1,100,000 chars** of tool output.

**Loop assertions.** The 4,377-line compaction suite covered above is the centerpiece. Also `session-compaction.test.ts`, `sdk-compaction-coordinator.test.ts`, `compact-session-script.test.ts`, `apps/cli/src/runtime/interactive/compaction.test.ts`, and `apps/vscode/src/core/hooks/__tests__/taskcancel.test.ts` (581 lines — hook contract for the cancel event, including `completionStatus: 'abandoned'`, not history validity).

**Benchmark layer — formal pass@k / pass^k, but disabled in CI.**
`evals/` has a documented four-layer architecture (`evals/README.md`, `ARCHITECTURE.md`): contract tests → smoke tests (minutes) → e2e with `cline-bench` (hours, git submodule, "12 production bug fixes") → analysis. `evals/analysis/src/metrics.ts` implements both metrics properly:
- `passAtK(trials, k)` = `1 - C(n-c, k) / C(n, k)` — "can the model do it at all"
- `passCaretK(trials, k)` = `C(c, k) / C(n, k)` — *"Can I rely on this model?" (reliability metric)*

Also `evals/analysis/patterns/cline-failures.yaml` for failure classification and `evals/baselines/` for regression detection.

**Deliberately not running:** `evals/README.md` states the smoke-test layer is *"partially disabled while the eval framework is repointed at the new SDK CLI"* and that *"the auto-running `cline-evals-regression.yml` workflow [is] off until someone wires the build step at the new SDK CLI."* **Verified**: `cline-evals-regression.yml` is absent from `.github/workflows/` (which contains only publish/test/repo-hygiene workflows).

## `RooCodeInc/Roo-Code` (TS, vitest) — 471 test-named files, ~153,900 lines

Covered above. Additional notes: no OS-sandbox tests (grep for `sandbox-exec|seatbelt|bwrap` across all `.spec.ts` returns nothing). E2E lives in `apps/vscode-e2e/src/suite/` (`task.test.ts`, `subtasks.test.ts`, `modes.test.ts`, `tools/`) driving a real VS Code instance. CI workflow is `code-qa.yml`.

## `block/goose` (Rust) — 272 files with `#[cfg(test)]`, **3,353** `#[test]`/`#[tokio::test]` functions (the 27 test-named-`.rs`-files figure badly undercounts; Rust tests are inline)

**Model faking: two distinct mechanisms.**
1. **`wiremock` — a local mock HTTP server speaking the OpenAI wire protocol.** `crates/goose/src/agents/state_machine/tests/dummy_api.rs` (990 lines) uses `wiremock::{Mock, MockServer, Request, ResponseTemplate}` and models a response taxonomy as an enum: `Reply`, `ReplyWithDistinctIds`, `ToolCall{require_advertised}`, `ToolCalls(Vec<_>)`, `Mixed{reasoning, text, call}`, `NoChoices`, `OutputLimit`, `ContextLimitError(String)`, `ServerError(String)`, `EmptyServerError`, `ReplyThenServerError`. Plus a `ProviderFeatures` struct (`reports_usage`, `preserves_thinking`, `cache_read_tokens`, `manages_own_context`) to simulate provider variation. **Because it's a real HTTP server, goose captures and asserts on the actual outgoing request**: `input_contains(needle)`, `system_contains(needle)`, `input_occurrences(needle)`, `uses_model(model)`.
2. **Content-addressed record/replay**: `crates/goose/src/providers/testprovider.rs` (333 lines). `TestProvider::new_recording(inner, path)` / `new_replaying(path)`, storing `HashMap<String, TestRecord>` where the key is `Sha256(serde_json(messages))` — so **the constructed message list is the cache key**; any drift in history construction causes a replay miss. It deliberately normalizes before hashing: strips `tool_meta`/`_meta` and `is_error: Some(false)` because *"This metadata is used for internal routing… and isn't part of the semantic input the LLM sees."*

**CI gating**: `crates/goose-cli/src/scenario_tests/scenario_runner.rs:178` — recording is hard-blocked in CI:
```rust
if std::env::var("GITHUB_ACTIONS").is_ok() {
    panic!("Test recording is not supported on CI. Did you forget to add the file {} to the repository…", file_path)
}
```
A corrupt cassette deletes itself and fails with "re-run test to record fresh data."

**Cross-provider scenario matrix**: 13 recordings across 5 providers (`anthropic`, `openai`, `azure_openai`, `google`, `groq`) at `crates/goose-cli/src/scenario_tests/recordings/`. `GOOSE_TEST_PROVIDER` narrows to one.

**Loop assertions** beyond compaction: `state_machine/tests/` has `tool_lifecycle.rs` (`execution_recovers_from_timeout_cancellation_and_filtered_output`, `elicitation_accept_decline_and_cancel`), `provider_lifecycle.rs`, `steering_lifecycle.rs`, `hooks_lifecycle.rs`, `reconstruction_isolation_lifecycle.rs`, `agent_reply.rs`, `pipeline.rs` (1,104 lines of harness, with `resume_cancelled()` and `run_with_cancel()`).

**What they deliberately don't test — verified.** `crates/goose-cli/src/scenario_tests/scenarios.rs` is 98 lines and contains only **three** active scenarios (`test_what_is_your_name`, `test_weather_tool`, `test_image_analysis`). The fourth, `test_context_length_exceeded_error`, is **fully commented out** (lines 79–97) — including its assertion `assert_eq!(result.messages.len(), 2, "One message after compaction")` — even though `context_length_exceeded.json` recordings are still checked in for `anthropic` and `azure_openai`. No comment explains why it was disabled. Skip reasons for the live ones *are* documented inline, e.g. *"Google tells me it only knows about the weather in the US, so we skip it."*

## `SWE-agent/mini-swe-agent` (Python, pytest) — 44 test files, ~10,200 lines

**Model faking: pure scripted stubs, shipped in `src/` not `tests/`.**
`src/minisweagent/models/test_models.py` (269 lines) exports three parallel fakes so the same agent loop can be tested across all three tool-calling wire formats:
- `DeterministicModel` — text-based action parsing
- `DeterministicToolcallModel` — OpenAI `tool_calls` format
- `DeterministicResponseAPIToolcallModel` — OpenAI **Responses API** (`function_call` / `function_call_output`)

Each returns `config.outputs[self.current_index]` in strict sequence. Escape-hatch actions in `_process_test_actions`: `{"raise": exc}` throws, `/sleep N` sleeps then re-queries, `/warning …` logs then re-queries.

**Loop assertions** (`tests/agents/test_default.py`): `test_step_limit_enforcement`, `test_cost_limit_enforcement`, `test_wall_time_limit_enforcement`, `test_timeout_captures_partial_output`, `test_message_history_tracking`, `test_step_adds_messages`, `test_empty_actions_handling`, `test_repeated_format_errors_terminate_cleanly`, `test_format_error_counter_resets_on_success`, `test_format_errors_count_against_cost_limit`. Retry/format-error policy is well covered; tool_use/tool_result *pairing* is not asserted structurally.

**CI runs real environments and (some) real API calls.** `.github/workflows/pytest.yaml` installs **podman, bubblewrap (`chmod u+s $(which bwrap)`), and apptainer/singularity**, then configures `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `REQUESTY_API_KEY` from secrets before `pytest -n auto`. So the environment backends (`tests/environments/test_docker.py`, `test_singularity.py`, `test_local.py`) are exercised for real. These are *isolation* backends, not deny-rule sandboxes — there is no "assert this command was blocked" test.

**Money-spending tests are opt-in and loudly fenced.** `tests/conftest.py` adds `--run-fire` ("Run fire tests (real API calls that cost money)"), and `tests/test_fire.py` opens with a boxed banner:
> ⚠️ CRITICAL WARNING ⚠️ / THIS TEST FILE SHOULD NEVER BE RUN BY AN AI AGENT. / IT REQUIRES EXPLICIT HUMAN REQUEST AND SUPERVISION.

`src/minisweagent/run/benchmarks/` holds the SWE-bench runner; it is not invoked from CI.

## `SWE-agent/SWE-agent` (Python, pytest) — 25 test files, ~2,800 lines. **Much thinner than its reputation suggests.**

**Model faking**: `sweagent/agent/models.py` ships `PredeterminedTestModel` (line 529, sequence of `str | dict`, optional `tool_calls`), `InstantEmptySubmitTestModel` (line 551, always does `touch reproduce.py` then `submit`), `ReplayModel` (line 464, replays a `.traj`), and `HumanModel`/`HumanThoughtModel`.

**Loop assertions**: `tests/test_agent.py:149` `test_run_step_by_step_checking_history` is the core loop test — hard-coded index/count assertions after each `a.step()` (`len(a.messages) == 3` → `== 5` → `== 7`; `len(a.trajectory) == 2` "we requery once because format error"), checking exact message content by index. Exit-condition tests: `test_exit_cost`, `test_exit_context`, `test_exit_model_error`, `test_exit_format`, `test_exit_blocklist`, `test_early_exit`. `test_run_autosubmit` is marked `@pytest.mark.xfail` with `# todo: fixme; Needs real environment or mocking of read_file`.

**Replay**: `tests/test_run_replay.py` (33 lines) runs `RunReplay` against a checked-in trajectory in a `python:3.11` Docker image with `_require_zero_exit_code=True`.

No cancellation test, no compaction-validity test, no snapshot library, no sandbox-enforcement test. SWE-bench evaluation lives outside the test suite entirely.

## `anomalyco/opencode` (ex-`sst/opencode`, TS, bun:test + Effect) — 646 `.test.ts` files, ~159,100 lines

**Model faking: a first-party HTTP cassette recorder.** `packages/llm/test/recorded-test.ts` imports `@opencode-ai/http-recorder` (their own package) with real cassette machinery: `HttpRecorderInternal.hasCassetteSync`, and a rich `redact` config merging `headers`, `allowRequestHeaders`, `allowResponseHeaders`, `queryParameters`, `jsonFields`. Cassettes live at `packages/llm/test/fixtures/recordings/`.

**The standout: a cross-protocol golden conformance matrix.** `recorded-golden.ts` + `recorded-scenarios.ts` (531 lines) define 7 golden scenarios — `text` ("streams text"), `tool-call`, **`tool-loop` ("drives a tool loop")**, `image`, `image-tool-result` ("reads image returned from tool result"), `reasoning`, `reasoning-continuation` ("continues encrypted reasoning") — and run them against **11 recorded provider protocol directories**: `anthropic-messages`, `anthropic-messages-cache`, `bedrock-converse`, `cloudflare-ai-gateway`, `cloudflare-workers-ai`, `gemini`, `gemini-cache`, `openai-chat`, `openai-compatible-chat`, `openai-responses`, `openai-responses-cache` — plus a **websocket transport** variant (`recorded-websocket.ts`, `transport:websocket` tag). This is the widest provider-conformance sweep of the agent loop found anywhere in this survey.

`packages/llm/test/` also has `continuation-scenarios.ts`, `tool-stream.test.ts`, `cache-policy.test.ts`, `prepare.test.ts`, `provider-error.test.ts`, `executor.test.ts`.

---

# Cross-cutting summary

| | model faked how | loop invariants | request payload pinned? | sandbox enforcement | e2e/bench in CI | cancel-history | compaction-validity |
|---|---|---|---|---|---|---|---|
| **gemini-cli** | scripted `ContentGenerator` + response-only recorder (`--fake-responses`) | very strong (rollback, retry, parallel-stream assembly) | ✗ (prompts + rendered context snapshotted, not HTTP body) | **✓ real OS sandbox, 1,259 lines, 3 backends in CI** | integration ✓ (×3 sandboxes); evals manual/nightly | **✓ strongest** | ✓ golden snapshot + subprocess E2E |
| **cline** | handler-level scripted streams; opt-in live tier | very strong on compaction | ✗ | ✗ | ✗ (evals workflow removed) | partial (cancel×compaction) | **✓ strongest (explicit tool-pair invariant)** |
| **Roo-Code** | vitest mocks | strong on tool-result ID repair | ✗ | ✗ | vscode-e2e | partial (abort-during-flush) | ✓ (repair-at-send + non-destructive truncation) |
| **crush** | **go-vcr cassettes** (real provider) + Go stubs | strong (parallel tool_call↔result by ID) | **✓ implicitly (cassette matches request)** | ✗ | ✓ `go test -race ./...` on 3 OSes | **✓ (race/state-machine focus)** | ✗ |
| **goose** | **wiremock local HTTP server** + SHA256-keyed record/replay | strong (compaction lifecycle, tool lifecycle) | **✓ (`input_contains`/`system_contains`)** | ✗ | ✓ replay-only (recording panics in CI) | partial (steering resume) | ✓ unit; ✗ cross-provider scenario **commented out** |
| **opencode** | **first-party HTTP cassette recorder** | 7 golden scenarios × 11 protocols × 2 transports | **✓ (cassettes w/ redaction)** | ✗ | ✓ | ✗ | ✓ suite exists, **no tool-pair assertion** |
| **mini-swe-agent** | 3 scripted stubs (text / toolcall / Responses API) | good (limits, format-error policy) | ✗ | ✗ (real podman/bwrap/apptainer, no deny-rule tests) | ✓ + live keys; `--run-fire` opt-in for paid | partial (KeyboardInterrupt) | ✗ (only `test_truncation_finish_reason`) |
| **SWE-agent** | `PredeterminedTestModel` / `ReplayModel` | index/count assertions | ✗ | ✗ | ✗ | ✗ | ✗ (elision counting only) |

**Headline takeaways for your project:**

1. **Cancellation-history correctness is tested by a real minority — 3 of 8 do it seriously** (gemini-cli, crush, and partially Roo-Code). gemini-cli's multi-turn continuation rollback is the single best prior art. This is *not* a solved/standard practice; having it is genuinely differentiating.
2. **Compaction-validity is better covered, but only cline asserts the actual tool_use↔tool_result atomicity invariant** — and it wrote that test only after shipping the "No tool call found for function call output" provider error. Notably opencode has a 1,975-line compaction suite with no such assertion, and goose's cross-provider version is commented out. The invariant is under-tested relative to how often it breaks.
3. **Two distinct schools on request pinning.** Cassette-based projects (crush, goose, opencode) pin the request for free because the request is the cassette match key / hash key — this catches prompt and history-construction drift automatically. Response-only fakes (gemini-cli, cline, Roo, mini-swe-agent, SWE-agent) do not, and gemini-cli compensates with explicit prompt + rendered-context snapshots.
4. **Real OS-sandbox enforcement testing is rare: gemini-cli is essentially alone**, and the pattern worth stealing is negative-then-positive (assert blocked *and* `existsSync === false`, then re-run with an expanded policy and assert success), plus per-platform `it.skipIf` with a written reason.
5. **Nondeterminism discipline exists in two shapes**: gemini-cli's reliability *policy tiers* (`ALWAYS_PASSES` gates CI, `USUALLY_*` opt-in behind `RUN_EVALS`, `USUALLY_FAILS` as `it.fails`) and cline's formal *statistics* (pass@k for capability, pass^k for reliability). Neither project runs its benchmark layer on PRs.

---

# Unverified / could not confirm

- **`charm.land/x/vcr`'s request-matching rules.** I verified crush's cassettes store the full request body and that go-vcr is the underlying library, but `charm.land/x/vcr` is an external module not vendored in the tarball, so I could not read its default matcher to confirm *which* fields (body vs URL+method only) are matched. My "request payload pinned ✓ (implicitly)" for crush rests on the cassette contents plus go-vcr convention, not on reading the matcher source. Treat as inference.
- **`@opencode-ai/http-recorder` matcher behavior** — same caveat. The package is referenced from `packages/llm/test/recorded-test.ts`; I did not locate/read its implementation inside the tarball.
- **opencode compaction test names.** `packages/opencode/test/session/compaction.test.ts` uses an Effect-based test wrapper, so my `grep 'test("'` did not enumerate individual case names. I verified the four `describe` blocks (`isOverflow`/`create`/`prune`/`process`) and ran a targeted grep for tool-pair vocabulary (which came back negative), but I did not read all 1,975 lines. The "no tool-pair assertion" claim is grep-based, not exhaustive-read-based.
- **Which gemini-cli sandbox backends map to which OS primitives.** The test file references seatbelt (macOS) and Windows Low Integrity + `icacls` in comments, which I quoted. I did not read the Linux backend implementation to confirm whether it uses bubblewrap, Landlock, or namespaces.
- **Whether gemini-cli's `sandboxManager.integration.test.ts` runs on every PR** and on which runner OSes. I confirmed the three `test:integration:sandbox:*` npm scripts and the existence of `ci.yml`, but did not read `ci.yml`'s job matrix to confirm which are wired to PR triggers.
- **cline/Roo-Code overall scale attribution.** The line counts (219k / 154k) are `find`-based on files whose *names* contain `test`/`spec`; they include webview/UI tests and are not a measure of agent-loop coverage specifically.
- **`sst/opencode` history.** I verified only that `github.com/sst/opencode` currently redirects to `github.com/anomalyco/opencode`. I did not verify why, when, or whether `anomalyco/opencode` is the same lineage the user meant.
- **Repo freshness.** Everything reflects default-branch HEAD as downloaded on 2026-08-24 (`opencode` from branch `dev`; all others `main`). No commit SHAs were captured, so these findings are not pinned to a specific revision.