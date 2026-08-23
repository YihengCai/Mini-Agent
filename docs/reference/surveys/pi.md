# `pi` — disambiguation + agent-loop testing survey

## Which "pi" is meant

**Verdict: `earendil-works/pi`** (formerly `badlogic/pi-mono`, by Mario Zechner) — an AI agent toolkit whose packages are literally `pi-ai` / `pi-agent-core` / `pi-coding-agent`, plus a CLI binary named `pi`. It sits in exactly the same category as Codex and DeepSeek's harness (terminal coding agent + agent loop), which is why it was named alongside them.

Verified:
- `GET https://api.github.com/repos/earendil-works/pi` → `full_name: earendil-works/pi`, desc **"AI agent toolkit: unified LLM API, agent loop, TUI, coding agent CLI"**, TypeScript, **95,753 stars**, `pushed_at 2026-08-23`, default branch `main`.
- `GET https://api.github.com/repos/badlogic/pi-mono` **redirects to** `earendil-works/pi` — confirming the old name people still cite.
- npm scope migrated `@mariozechner/pi-*` → `@earendil-works/pi-*`; the old scope survives as vitest aliases in `packages/coding-agent/vitest.config.ts`.

Ranked candidates I actually checked:

| # | Candidate | Real code? | Fit |
|---|---|---|---|
| 1 | `earendil-works/pi` (ex-`badlogic/pi-mono`) | yes, 1,559 files | **Best.** Coding-agent CLI, explicit `agent-loop.ts`, harness, evals package |
| 2 | `Dicklesworthstone/pi_agent_rust` | yes, Rust, 1,634★, pushed 2026-08-23 | Secondary. README self-describes as for "pi/OpenClaw users" — a Rust reimplementation of #1. Own large conformance suite (details below) |
| 3 | `Physical-Intelligence/openpi` | yes, 13,423★, Python, pushed 2026-06-16 | **Poor fit.** Robotics VLA. Whole repo has **15** test files (`src/openpi/models/pi0_test.py`, `policies/policy_test.py`, `training/data_loader_test.py`, …). No agent loop, no tool_use/tool_result, no sandbox tests |
| 4 | Inflection AI "Pi" assistant | no public harness repo found | Not applicable |
| 5 | "PI" = prompt injection | not found as an agent framework in this context | Not applicable |

Everything below is about **candidate #1** unless labeled.

---

## 1. Is the model faked, replayed, or real? — **all three layers, cleanly separated**

**(a) Scripted stub provider ("faux") — the primary mechanism.** `packages/ai/src/providers/faux.ts` (708 lines) is a first-class provider shipped *in `src`*, not in test dirs. It registers a fake API/provider (`api: "faux"`, model `faux-1`, `baseUrl: "http://localhost:0"`), takes a queue of scripted steps, and **synthesizes the streaming event sequence** rather than any HTTP:

```ts
export type FauxResponseStep = AssistantMessage | FauxResponseFactory;
export interface RegisterFauxProviderOptions {
  deferred?: { pendingFetches?: number; pollAfterMs?: number };
  tokensPerSecond?: number;
  tokenSize?: { min?: number; max?: number };
}
```

It also *simulates* prompt-cache accounting by common-prefix-diffing a serialized context per `sessionId` (`withUsageEstimate`, `commonPrefixLength`), emits `start`/deltas/`error{reason:"aborted"}`/`end`, and can chunk text at a configurable `tokensPerSecond` — so streaming-assembly and cancellation are testable without a network.

**(b) SDK-level mocks that capture the exact outgoing request.** e.g. `packages/ai/test/openai-completions-cache-control-format.test.ts` does `vi.mock("openai", ...)` with a `FakeOpenAI` whose `chat.completions.create` stores `params` into `mockState.lastParams`, then asserts on the payload. `packages/ai/test/anthropic-sse-parsing.test.ts` hand-builds a raw `text/event-stream` `Response` and injects it via `streamAnthropic(model, context, { client: createFakeAnthropicClient(response) })`.

**(c) Real live API calls — opt-in, key-gated, never in CI.** `packages/ai/test/` has a large cross-provider live matrix, all guarded:

```ts
describe.skipIf(!process.env.OPENAI_API_KEY)("openai responses cache affinity e2e", () => {
  it("handles direct OpenAI Responses requests with aligned cache-affinity identifiers", { retry: 2 }, ...
```
`packages/ai/test/abort.test.ts` alone gates ~12 providers this way (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_OAUTH_TOKEN`, `MISTRAL_API_KEY`, `TOGETHER_API_KEY`, `BASETEN_API_KEY`, `MINIMAX_API_KEY`, `XIAOMI_API_KEY`, `hasAzureOpenAICredentials()`, `hasBedrockCredentials()`, `hasCloudflare*Credentials()`), each `{ retry: 3 }`.

**No cassettes / VCR / recorded HTTP replay, and no local mock HTTP server speaking a provider wire protocol** anywhere I could find. The SSE tests fabricate a `Response` object instead of binding a socket.

**Isolation is enforced by construction.** Root `./test.sh` rebuilds the environment with `env -i`, an ephemeral `HOME`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_ASKPASS=$(type -P false)`, `PI_NO_LOCAL_LLM=1`, `AWS_EC2_METADATA_DISABLED=true`, and prints `"Running tests without API keys in isolated home"`. `packages/coding-agent/vitest.config.ts` sets `env: { PI_OFFLINE: "1" }` with a comment "Tests run offline by default; opt in with `allowNetwork()`", and `test/test-network-env.ts` is a 4-line `vi.stubEnv("PI_OFFLINE", undefined)`.

---

## 2. What is asserted about the LOOP itself

The core file is `packages/agent/test/agent-loop.test.ts` — **1,607 lines / ~47 KB**, using a hand-rolled `MockAssistantStream extends EventStream`. Verified test names include:

- **Truncation → tool safety:** `"should not execute tool calls from a length-truncated assistant message"`
- **Parallel tool calls + result ordering (the sharpest assertion in the repo):**
  `"should emit tool_execution_end in completion order but persist tool results in source order"`
  ```ts
  expect(parallelObserved).toBe(true);
  expect(toolExecutionEndIds).toEqual(["tool-2", "tool-1"]);
  expect(toolResultIds).toEqual(["tool-1", "tool-2"]);
  expect(turnToolResultIds).toEqual(["tool-1", "tool-2"]);
  ```
- **Execution-mode contract:** sequential forced by *any* tool with `executionMode: "sequential"` even under a parallel default; parallel allowed only when all tools opt in (3 tests)
- **Steering/queue injection:** `"should inject queued messages after all tool calls complete"`
- **Turn termination:** `terminate=true` from tool results, from `beforeToolCall`-blocked calls, from `afterToolCall`; mixed batches; `shouldStopAfterTurn`; `prepareNextTurn` snapshot semantics
- **Hook ordering:** `transformContext` before `convertToLlm`; mutated `beforeToolCall` args executed *without* revalidation
- **`agentLoopContinue`:** throws with empty context; continues without re-emitting user-message events

Session-level loop behavior lives in `packages/coding-agent/test/suite/`:
- `agent-session-retry-events.test.ts` — retry-after-transient-error, multi-failure recovery, retry exhaustion event, `retry: false`, non-retryable errors, `abortRetry` cancelling the retry sleep, "waits for the full loop when retry recovery produces tool calls", exact event order for text turns and tool turns, streaming deltas in `message_update`, and **`"emits agent_end for aborted runs and persists the aborted assistant message"`** (the cancellation → history-validity assertion)
- `agent-session-queue.test.ts` — steering vs follow-up delivery, one-at-a-time vs `all` batching, `deliverAs`, `nextTurn` injection, `pendingMessageCount` mutation before `message_start`, follow-ups queued during `agent_end`
- `agent-session-compaction.test.ts` — 24 tests: manual/auto/threshold compaction, `abortCompaction` mid-compaction, compact-and-retry on `length` stop (and "stops after one compact-and-retry when a second response is also truncated"), overflow recovery capped at one retry, stale pre-compaction usage ignored, extension notification on auto-compaction failure
- `agent-session-prompt.test.ts` — throws when prompted during streaming without a `streamingBehavior`, during manual compaction, without a model, without configured auth
- Retry *policy* proper is unit-tested with fake timers in `packages/ai/test/provider-retry.test.ts` (429 + `retry-after-ms`, `x-should-retry: false`, `maxRetryDelayMs` cap and its rejection message, abort cancelling the sleep with `expect(vi.getTimerCount()).toBe(0)`)

**tool_use/tool_result pairing** is asserted at the provider layer: `packages/ai/test/tool-call-without-result.test.ts` (359 lines) repeats `"should filter out tool calls without corresponding tool results"` across ~25 provider/auth combos — but those are **live-API** tests (`{ retry: 3, timeout: 30000 }`, OAuth tokens resolved at module top level), so they're skipped in CI.

**Regression discipline:** `packages/coding-agent/test/suite/regressions/` holds **67** files named `<issue>-<slug>.test.ts`, mandated by `AGENTS.md`. Loop-relevant ones: `3688-tree-cancel-compacting`, `5998-blocked-tool-terminate`, `6647-compaction-retries-transient-stream-drop`, `7253-manual-compact-during-response`, `7150-rpc-prompt-during-compaction`, `8328-zero-usage-auto-compaction`, `3317-network-connection-lost-retry`, `6904-dns-transport-retry`, `1717-2113-agent-session-event-settlement`, `7925-toolcall-start-metadata`, `5208-late-bash-output`.

---

## 3. Snapshot / golden testing — **none**

- **Verified zero `.snap` files and zero `__snapshots__` directories** in the entire 1,559-file tree. No `toMatchSnapshot`-based golden files, no `jest-image-snapshot`, no `insta`-style approvals.
- Request bodies **are** asserted, but as explicit structural expectations against a captured payload object, not snapshots — see `expectAnthropicCacheMarkers(params)` in `openai-completions-cache-control-format.test.ts` checking `cache_control: { type: "ephemeral" }` on the system message, the tools array, and the last user message.
- Rendered-UI testing exists but is assertion-based, not golden: `packages/tui/test/virtual-terminal.ts` implements `VirtualTerminal implements Terminal` on top of **`@xterm/headless`** (`disableStdin`, `allowProposedApi`), and `packages/tui/test/tui-render.test.ts` (832 lines) drives real components against it and reads back the emulated cell buffer. Note `packages/tui` uses **`node:test` + `node:assert`**, not vitest: `"test": "node --test --test-reporter=dot ... test/*.test.ts"`.
- Reusable **conformance suites** substitute for snapshots in the storage layer: `packages/agent/src/harness/session/testing/conformance.ts` (1,016 lines, `node:assert/strict`) is a backend-agnostic case list run against each `SessionRepo` implementation (also `packages/telemetry/src/testing/conformance.ts`, `packages/server/src/testing/`).

---

## 4. Sandbox / permission testing — **no OS-sandbox-block assertions exist**

This is a genuine gap, verified by exhausting every path matching `sandbox|seatbelt|landlock|bubblewrap|permission` in the tree:

```
packages/coding-agent/examples/extensions/permission-gate.ts
packages/coding-agent/examples/extensions/sandbox/{index.ts,package.json,package-lock.json,.gitignore}
packages/coding-agent/src/bun/restore-sandbox-env.ts
packages/coding-agent/test/restore-sandbox-env.test.ts
```

- OS sandboxing is an **example extension only**, not core: `examples/extensions/sandbox/index.ts` (321 lines) wraps the built-in bash tool with `SandboxManager` from **`@anthropic-ai/sandbox-runtime`** — "sandbox-exec on macOS, bubblewrap on Linux", config at `.pi/sandbox.json` with `network.allowedDomains` / `filesystem.denyRead|allowWrite|denyWrite`. **It has no test file.**
- The one "sandbox" test is unrelated to blocking: `restore-sandbox-env.test.ts` (77 lines) mocks `node:fs` and asserts that a Bun bug workaround (oven-sh/bun#27802 — empty `process.env` inside sandboxes) rehydrates env from `/proc/self/environ`. It asserts `readFileSync` was called with `"/proc/self/environ"`; it never runs a sandboxed command.
- Permission-adjacent tests that *do* exist are policy-level, not OS-level: `trust-manager.test.ts`, `trust-selector.test.ts`, `regressions/8261-subagent-project-trust.test.ts`, `5109-exclude-tools.test.ts`, `2835-tools-allowlist-filters-extension-tools.test.ts`, `3592-no-builtin-tools-keeps-extension-tools.test.ts`.
- **CI platform coverage:** `.github/workflows/ci.yml` is a single job on **`ubuntu-latest` only** (Node 22, `npm ci --ignore-scripts` → `npm run build` → `npm run check` → `npm test`), with no secrets. So macOS/Windows are never exercised by the unit suite even though Windows-specific tests exist (`bash-close-hang-windows.test.ts`, `package-manager-ssh.test.ts`). The only cross-platform CI is `build-binaries.yml`'s `smoke-test-binaries` matrix (`ubuntu-latest`, `macos-latest`, `windows-latest`), which runs exactly `pi --help` and `pi --version` on release tags.

---

## 5. End-to-end / benchmark layer — `packages/evals`, real models, **manual only**

A dedicated private workspace `@earendil-works/pi-evals` adapts a **real `AgentSession`** to **`vitest-evals@0.15.0`** (getsentry). `packages/evals/README.md`:

> "Pi evals are behavioral, model-backed checks for Pi workflows. They adapt a real `AgentSession` to `vitest-evals`, run it in isolated temporary project and agent directories, and attach native Pi session artifacts."

- Invocation: `npm run eval -- --provider openai --model gpt-5.6-sol` (or `PI_PROVIDER`/`PI_MODEL`); auth comes from Pi's normal `ModelRuntime` including subscription credentials. **Not wired into any GitHub workflow** — `ci.yml` runs only `npm test`, and evals' own `package.json` splits `"eval": "node scripts/run-evals.mjs"` from `"test": "vitest run --config vitest.test.config.ts"` (the latter unit-tests the eval infrastructure itself).
- Config: `include: ["src/**/*.eval.ts"]`, `fileParallelism: false`, `testTimeout: 120000`, custom reporters (`vitest-evals/reporter` + `src/vitest-evals/reporter.ts`).
- **Not** SWE-bench / exercism / terminal-bench. The checked-in suites are internal: `src/smoke.eval.ts` (asks for the capital of France, asserts `result.usage.provider === process.env.PI_PROVIDER`, `totalTokens > 0`, `errors == []`) and `src/extensions.eval.ts`. The harness supports multi-step runs `[{type:"prompt"}, {type:"reload"}, {type:"prompt"}]` for "create an extension then use it" scenarios.
- **Nondeterminism handling is explicitly designed, and it's the most interesting part:**
  - `evalHarnessTable("...", { baseline, candidate, repetitions: 6 })` × `describe.for(...)` for baseline-vs-candidate A/B
  - Grouping key = repetition + non-empty `input.id`, else a **SHA-256 hash of strict canonical JSON input**
  - "the reporter computes **pass-rate lift** from each run's recorded average judge score, treating a score of at least `1` as passing. Lift is the candidate pass rate minus the baseline pass rate, in percentage points."
  - Tokens/latency/estimated cost tracked as separate **paired candidate-minus-baseline deltas**; missing judge scores reported as *incomplete observations*, missing telemetry as *unavailable*
  - `judgeThreshold: null` is mandated for comparative suites: "This keeps a low score as an observation instead of making the Vitest invocation fail… `expect.soft(...)` still fails the test and is not a scoring mechanism."
  - Randomization deferred to Vitest's built-in sequence shuffling
  - Artifacts: a gitignored `.eval/` dir, `runs.jsonl` index, and **native Pi session JSONL snapshotted before the temp workspace is deleted**, registered against the Vitest task in an eval-only `afterEach`
  - Methodology explicitly outsourced to [`adewale/skill-eval-harness`](https://github.com/adewale/skill-eval-harness/)

Release-time E2E is **manual and documented in `AGENTS.md`**: `npm run release:local -- --out /tmp/pi-local-release`, then run the Node and Bun binaries from outside the repo — `--help`, `--version`, `--list-models`, `-p "Say exactly: ok"`, plus interactive startup driven under tmux — "at least one real prompt with the intended default provider… Failures are release blockers unless the user explicitly accepts the risk."

---

## 6. What they deliberately do NOT test (with the documented reasoning)

Direct quotes from `AGENTS.md`:
- > "Never run `npm run build` or `npm test` unless requested by the user."
- > "**Never run the full vitest suite directly: it includes e2e tests that activate when endpoint/auth env vars are present.** For all non-e2e tests, run `./test.sh` from the repo root."
- > "For `packages/coding-agent/test/suite/`, use `test/suite/harness.ts` + the faux provider. **No real provider APIs, keys, or paid tokens.**"

`packages/coding-agent/test/suite/README.md`:
- > "Do not use real provider APIs, real API keys, network calls, or paid tokens. Keep these tests CI-safe and deterministic."
- > "Do not use or extend the legacy `test/test-harness.ts` path unless a missing capability forces it" — a live deprecation of the older 471-line faux harness in favor of `test/suite/harness.ts`.

So, deliberately untested-in-CI / untested-at-all:
1. **Live provider conformance** — the entire `packages/ai` live matrix (tool_use/tool_result pairing, abort mid-stream, cache affinity, reasoning replay) never runs in CI; cost and flakiness are the stated reasons, mitigated by `{ retry: 2..3 }` when a human runs them with keys.
2. **Evals / behavioral quality** — never in CI, cost-gated, and comparative suites are designed *not* to fail the build (`judgeThreshold: null`).
3. **OS sandbox enforcement** — no test asserts a command was blocked; sandboxing is example-code with an external dependency.
4. **Non-Linux unit runs** — CI is ubuntu-only; macOS/Windows get `--help`/`--version` only, at release.
5. **Interactive TUI end-to-end** — replaced by a documented *manual* tmux recipe (`tmux new-session -d -s pi-test -x 80 -y 24` → `./pi-test.sh` → `capture-pane`), plus the checked-in `pi-test.sh` / `pi-test.ps1` / `pi-test.bat`.
6. **Golden snapshots** — apparently a conscious avoidance; zero snapshot artifacts exist across ~3.9 MB of test code.

---

## 7. Scale and what's actually covered

Computed from the git tree blob sizes (`GET /repos/earendil-works/pi/git/trees/main?recursive=1`, `truncated: false`, 1,559 blobs):

| | files | bytes | est. lines¹ |
|---|---|---|---|
| `*.test.ts` / `*.test.mjs` / `*.eval.ts` | **467** | **3,897,498** | **~130,000** |
| `packages/*/src/**/*.ts` (non-test) | 535 | 4,044,941 | ~135,000 |

¹ estimated at ~30 bytes/line, calibrated on four files I downloaded (29.7 / 33.3 / 32.2 / 26.8 B per line). **Test code is roughly 1:1 with source by volume.**

Per package (test files / bytes):

```
coding-agent  238  1,688,720      ai   136  1,148,644
tui            33    566,542   agent    23    283,383
session-backends 11   64,104   server     7     51,770
client          6     35,558   evals      6     21,867
protocol        3     23,484  telemetry   2      8,908
root scripts    2      4,518
```

Plus 565 total files matching test/fixture/mock patterns (including `test/fixtures/skills/*`, `before-compaction.jsonl`, `large-session.jsonl`, `fake-external-editor.mjs`).

**Where coverage is actually measured:** exactly one place, and it points straight at the loop. `packages/agent/vitest.harness.config.ts` defines a second lane (`npm run test:harness` / `coverage:harness`) with:

```ts
include: ["test/harness/**/*.test.ts"],
coverage: { provider: "v8", all: true,
  include: ["src/harness/**/*.ts", "src/agent.ts", "src/agent-loop.ts"],
  reporter: ["text", "html", "lcov"], reportsDirectory: "coverage/harness" }
```

No other package config I fetched (root `vitest.base.ts`, `ai`, `coding-agent`, `evals`, `agent` default) declares coverage. Test runners: **vitest 4.1.9** everywhere except `packages/tui`, which is **`node:test`**.

---

## Secondary candidate #2 — `Dicklesworthstone/pi_agent_rust` (Rust reimplementation)

Verified from its `README.md` (183 KB, fetched raw) — a very different, evidence-artifact-heavy philosophy. Quoted lines:
- > "**Vendored corpus (223, plus one intentionally excluded negative test fixture)**: deterministic conformance, compatibility matrix, and scenario suites."
- > "**Release-binary live-provider E2E**: real `target/release/pi` execution against a non-mocked provider/model path." — driven by a `ext_release_binary_e2e` harness against **`ollama` + `qwen2.5:0.5b`** (a local model, i.e. free and reproducible), e.g. `--jobs 10 --timeout-secs 600 --extension-policy balanced --out-json tests/ext_conformance/reports/release_binary_e2e/...`
- > "**Capability-based context (`Cx`)**: Async functions receive an explicit context that controls what they can do (HTTP, filesystem, time). This makes testing deterministic."
- Committed JSON/MD evidence artifacts under `tests/ext_conformance/reports/**`, `tests/evidence_bundle/index.json`, `tests/full_suite_gate/certification_verdict.json`, with dated "historical run snapshot" claims (`223/223 tested extensions passed`, `123/123 must-pass gate`) and an explicit "Benchmark Methodology and Claim Integrity" section.
I did **not** verify these numbers against the actual test tree (GitHub API rate-limited me at that point) — they are README claims, not source I read.

---

## Unverified / could not confirm

- **`pi_agent_rust` test tree.** `GET /repos/Dicklesworthstone/pi_agent_rust/git/trees/main?recursive=1` returned `API rate limit exceeded` (unauthenticated; `gh` is not installed in this environment and WebFetch on `github.com` was blocked by network policy: *"Unable to verify if domain github.com is safe to fetch"*). Its file counts, line counts, and whether `tests/ext_conformance/` actually contains the 223-case corpus are **unconfirmed** — only the README text is verified.
- **Exact count of live-gated vs. mocked files in `packages/ai/test/`.** GitHub code search requires auth, so I could not grep for `skipIf` repo-wide. I read 6 of the 136 `ai` test files; the gating pattern is verified in `abort.test.ts`, `openai-responses-cache-affinity-e2e.test.ts`, and `tool-call-without-result.test.ts` but I did not enumerate all of them.
- **Line counts** for the 3.9 MB of tests are **estimated from blob sizes**, not counted; the file counts and byte totals are exact.
- **Whether any vitest config outside `packages/agent/vitest.harness.config.ts` declares coverage.** I fetched root/`ai`/`coding-agent`/`evals`/`agent`×2; I did not fetch `client`, `protocol`, `server`, or `session-backends/sqlite-node` configs.
- **`packages/coding-agent/examples/extensions/permission-gate.ts` contents** — I confirmed the path exists and that no test file references "permission" in its name, but did not read the file or prove no test covers it indirectly.
- **Inflection AI's "Pi"** — I found no public agent-harness repository; absence here is "not found", not "does not exist".

Sources: [earendil-works/pi](https://github.com/earendil-works/pi) · [badlogic/pi-mono (redirects)](https://github.com/badlogic/pi-mono) · [Physical-Intelligence/openpi](https://github.com/Physical-Intelligence/openpi) · [Dicklesworthstone/pi_agent_rust](https://github.com/Dicklesworthstone/pi_agent_rust) · [@mariozechner/pi-coding-agent on npm](https://www.npmjs.com/package/@mariozechner/pi-coding-agent) · [pi.dev](https://pi.dev/) · [getsentry/vitest-evals](https://github.com/getsentry/vitest-evals) · [adewale/skill-eval-harness](https://github.com/adewale/skill-eval-harness/)