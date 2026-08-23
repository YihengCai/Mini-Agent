I have enough verified material. Writing up.

## DeepSeek agent harness — how it tests its agent loop

### Disambiguation (verified)

The thing that exists is **`deepseek-ai/deepseek-harness`** — "DeepSeek Harness: Everything is a Plugin." Verified via `GET https://api.github.com/orgs/deepseek-ai/repos`:

```
"full_name": "deepseek-ai/deepseek-harness",
"description": "DeepSeek Harness: Everything is a Plugin.",
"language": "TypeScript",  "stargazers_count": 187068,  "updated_at": "2026-08-23T14:25:49Z"
```

It is a **product agent CLI/SDK, not an eval scaffold** — a pnpm monorepo of ~9,161 files, default branch `master`, built on a DI/plugin runtime called Cordis (`packages/*/*`, `apps/cli`, `apps/web`, `examples/*`). There is **no SWE-bench / terminal-bench / eval harness in the repo** (see §5).

Not verified / not official: `deepseekharness.dev` and `0xsline/awesome-deepseek-harness` surfaced in search but are third-party; I did not fetch or rely on them.

---

### 1. Is the model faked, replayed, or real? — **all four, in named tiers**

Four distinct model boundaries, each with a real file:

| Strategy | Artifact | What it is |
|---|---|---|
| **Scripted in-process stub** | `packages/core/agent-loop/tests/mock-adapter.ts` | `class MockAdapter extends LlmAdapter`, driven by a script array. Entries are `StreamChunk[]`, a function of the request, `'hang'`, `'hang-slow'` (50 ms to notice abort — "a stand-in for slow real-world teardown"), or `{hangAfter: chunks}`. Records `requests: GenerateOptions[]` for assertions. |
| **Local HTTP server on the provider wire** | `packages/llm/llm-deepseek/tests/mock-server.ts` | Real `node:http` server on `127.0.0.1:0` speaking OpenAI-compatible chat-completions SSE. Behaviors: `{kind:'sse'}`, `{kind:'http-error'}`, `{kind:'close-early'}` (→ `response.destroy()` mid-stream). Also implements the **Files API** (`POST/GET/DELETE /files`, multipart parse). Captures `requests[]` and `headers[]`. |
| **Scriptable wire-fault server (shared package)** | `packages/test-support/llm-mock-server/src/index.ts` (26 KB) + `cli.ts`, run via `pnpm run mock:llm` | Per the Agent Note `2026-07-25-scriptable-llm-wire-fault-server.md`: socket reset, post-header disconnect, partial disconnect, stall, valid empty completion, clean truncated stream, malformed payloads, HTTP failures, slow streaming, max-token. Has a `random` entry with a **logged unsigned 32-bit seed** and caller-supplied weights for reproducible stress. Note: `connection_refused` is a *CLI listener-lifecycle phase*, "because a bound request handler cannot refuse its own TCP connection." |
| **Cassette replay** | `packages/test-support/llm-replay/` | Not a VCR library — the fixture **is a projected persisted session log** (`<scenario>/session.jsonl`). Replay reconstructs each `stream()` call by grouping `assistant/chunk` events by `(turn, step)`. Recording = "run the real agent once and harvest the `.jsonl`". |
| **Live API** | `vitest.e2e.config.ts` | Real `https://api.deepseek.com`. |

Two replay subtleties worth stealing:

- **Non-reconstructable failures get a sidecar.** A pure throw before any chunk (HTTP 401 — the log has only `turn/end {error}`, no chunks) and cancel/hang (timing, not content) can't be derived from `assistant/chunk`. Those scenarios add `<scenario>/replay.override.json`, either a bare `ReplayEntry[]` or `{patches:[{at, entry}]}` by 0-based call index. A `hang` entry may name `readyFile` — replay writes an empty marker after its prefix chunks reach the loop, "so an external driver can cancel deterministically without observing a presentation update."
- **Nested subagents bind by first-call order.** Parent is `session.jsonl`, children `session.1.jsonl`…. Live session ids are random each run, so scripts are ordered by header `createdAt` and "the first live session to make any call claims the first script." More live sessions than recorded scripts **fails loud**.
- **`{{fromRequest:<regex>}}`** placeholders in scripted strings resolve against the live request at stream time (last match wins, first capture group) — for echoing back a randomly minted goal id.

The stated policy (`docs/testing.md`) is explicitly anti-mock:

> "Mock only the expensive or non-deterministic boundary (LLM adapter, network, clock); keep everything downstream real."

and, memorably:

> "**The with-key policy: inference is cheap here.** We are DeepSeek — do not ration real-API tests. A no-key test proves plumbing; only a with-key run proves the agent works against a real model."

---

### 2. What is asserted about the LOOP itself

`packages/core/agent-loop/tests/` is 21 files, ~**500 KB** of test source against `src/` of ~**80 KB** — roughly 6:1.

| File | Size | Subject |
|---|---|---|
| `loop.spec.ts` | 63 KB | main loop |
| `contract-regressions.spec.ts` | 55 KB | permanent regression pins |
| `scope-lifecycle.spec.ts` | 46 KB | |
| `cancel.spec.ts` | 43 KB | cancellation |
| `resume.spec.ts` | 42 KB | |
| `interception.spec.ts` | 35 KB | |
| `tool-calls.spec.ts` | 34 KB | scheduler |
| `request-reconstruction.spec.ts` | 30 KB | request purity |

**Cancellation / message-history validity after cancel** — `cancel.spec.ts` has ~35 cases naming the exact race window. Real titles:

- `cancel() on an idle agent with nothing queued is a no-op; the next prompt runs (F2 leak guard)`
- `cancel({ keepInbox: true }) latches a waking send landing in the abort-to-idle window`
- `a whenIdle() waiter registered BEFORE a pre-step cancel resolves (F1 hang guard)`
- `cancel drops a half-streamed tool call and keeps the completed text before it`
- `retry discards the failed attempt; the final message cites only its own chunks`
- `cancel from a synchronous agent/status(running) listener drops the turn (window 2)`
- `preserves the first user cancellation when lifecycle teardown races it`

**Parallel tool calls** — `tool-calls.spec.ts` asserts a scheduler contract, not just "it works":

- `runs parallel-safe siblings concurrently (all start before any completes)`
- `an exclusive call between two parallel-safe calls forms a barrier (3 groups)`
- `commits tool/result in model order even when a later call settles first`
- `derived history pairs calls in model order regardless of tool/call log interleaving`
- `maxParallelToolCalls: 1 is fully serial (no second start before the first settles)`
- `stops replenishing after abort, commits started results, and parks accepted additional contexts`
- `stops new dispatches and drains started bodies before surfacing the first failure` (failure quiescence)

**tool_use/tool_result pairing** — `packages/compaction/compaction/tests/tool-pairing.spec.ts` exports `toolPairingBalancedBefore` / `toolPairingBalancedAfter` and tests: `requires every result from a multiple-call assistant message`, `throws for an orphan result during a rebuild`, `retries an orphan result in an appended tail without committing partial cache state`.

**Request purity — the standout.** `request-reconstruction.spec.ts` header:

> "every request the loop sends is a pure function of the session log — messages derive at the step/start boundary and the header is the latest request/header snapshot."

Its capstone test is literally named:

```
it('THEOREM: every request rebuilds byte-equal from the session log alone', ...)
```

Plus `each step request within a turn append-extends the previous, frozen end to end` (KV-cache prefix stability as a *test*), `a compaction replace rewrites the resend, and the log explains it`, and `a mutation attempt on the frozen request content throws into the step (loud, not silent)`.

**Retry** — `packages/llm/llm-retry/tests/` (~70 KB), incl. `transport-recovery.spec.ts` which routes the mock wire-fault server through the **real** `dsh-llm-deepseek` adapter + `dsh-agent-loop` + `dsh-llm-retry`:
`recovers from a true refused connection after the endpoint starts during backoff`, `retries a wire-valid content-less completion without committing an empty message`, `exposes a clean partial EOF as non-default-retryable STREAM_CLOSED`, `turns a stalled body into TIMEOUT`, `stops after the configured transport retry budget is exhausted`.

**Property-based** — `agent-loop/tests/properties.spec.ts` uses `fast-check`. Invariants: every sent message appears exactly once; turn numbers strictly increase; status follows `idle→running→idle`. Determinism is structural, not seeded: *"schedules are driven through the `agent/status` settle signal (no wall-clock sleeps), so a flake is a finding, not timing noise."*

**Runtime invariants injected into every test.** `scripts/test-invariants.ts` (274 lines) is the global `setupFiles` for *all four* vitest configs. Each package ships `src/invariant.ts`; the setup globs `../packages/*/*/src/invariant.ts` (168 companions) and mounts the owning package's companion into every ordinary Cordis root, with one topology test mounting all of them.

---

### 3. Snapshot / golden testing

No `jest`/`playwright`-style image diffing for transcripts and **only 3 `__snapshots__/.snap` files** in the whole repo. Instead there's a hand-built record/replay system: **582 snapshot files** (241 `.jsonl`, 152 `.json`, 141 `.md`).

Layout, per scenario directory:

```
input.json                    # deterministic ACP JSON-RPC driver script
behavior.json
session.jsonl                 # recorded model transcript = replay cassette
session.1.jsonl               # subagent child sessions
stdout.expected.jsonl         # normalized JSON-RPC golden
system-prompt.expected.md     # ← exact system prompt
tool-schemas.expected.json    # ← exact tool schemas
replay.override.json          # optional, for throws/hangs
workspace/                    # seed files
```

**Yes, they snapshot the exact request content — but deliberately in exactly one scenario per "header class."** From `docs/testing.md`:

> "One ACP scenario (`text-turn`) pins full system-prompt/tool-schema content; other fixtures tokenize it so an edit churns one line."

Non-pinning fixtures store `"system":"{{system}}","tools":"{{tools}}"`. Normalizers in `packages/test-support/acp-snapshot/src/normalize.ts` (449 lines): `normalizeStdout` (JSON-RPC ids → first-seen sequence; UUIDs and *every native/JS filesystem spelling* of the generated cwd → tokens, longest-first), `scrubSystemPrompts`, `scrubToolSchemas`, `scrubRequestHeaders`, and `stabilizeFixtureMessageIds` — which carries committed UUIDs into unchanged messages so a re-record doesn't churn every line.

Three modes via `DSH_SNAPSHOT`, wired in `vitest.snapshot.config.ts`:
- `replay` (default, keyless) — parallel, `fileParallelism` on
- `record` — calls the real API, rewrites model fixtures; **serial** ("record spends real API quota per scenario")
- `refresh` — keyless, replays committed scripts, rewrites expected outputs; **serial** ("refresh write-back harvests volatile values from fixtures already on disk, so concurrent writers would corrupt goldens")

Only `record` loads `.env`; replay/refresh never do.

Scenario coverage: **84 ACP scenarios** in `examples/acp-agent/tests/snapshots/`, including `cancel`, `cancel-tool-calls`, `parallel-tool-calls`, `packed-chunks`, `empty-response-retry`, `error-finish`, `max-tokens-continue`, `escalation-approved`/`-rejected`, `partial-landlock-child-failure`, `missing-sandbox-runner`, and a full hook matrix (`hook-cc-*`, `hook-codex-*`).

Rendered-UI goldens exist too but are **text, not pixels**: `apps/web/tests/snapshots/*.expected.md` (142 files) compared under Chromium via Playwright — CI forces read-only `DSH_SNAPSHOT=replay`, "never writing expected outputs." TUI journeys use JSONL-driven scenarios under `apps/cli/tests/snapshots/`.

Policy (`docs/testing.md`): *"Every non-trivial model-, protocol-, or human-visible change adds or updates a keyless scenario in the same PR… Package tests, e2e assertions, mock/test-only compositions, and PR rationale do not replace the assembled transcript."*

---

### 4. Sandbox / permission testing — **yes, real kernel denials**

`packages/sandbox/sandbox-local/tests/` has `seatbelt.e2e.ts`, `landlock.e2e.ts`, `bwrap.e2e.ts`, `packed-install.e2e.ts`. These spawn real confined processes and assert the *world*, not the error message:

```ts
const { result, confined } = runConfined(sandbox, `echo hi > ${workdir}/denied.txt`,
  { mode: 'read-only', workspaceRoot: workdir })
expect(result.status).not.toBe(0)
expect(confined.enforcement).toBe('full')
// The wrap's denialSignatures must be what the kernel actually prints.
expect(result.stderr.toLowerCase()).toContain('operation not permitted')
expect(existsSync(join(workdir, 'denied.txt'))).toBe(false)
```

Test name: `read-only denies a write — the file must NOT exist, and the kernel speaks the advertised dialect`. Landlock's variant additionally probes the running kernel (`launcherPath() --probe`), parses `partially enforced`, and asserts every wrap reports **exactly** that level.

Each test forces the *other* rungs off so a leg proves exactly one mechanism: `sandbox.internals = { probeBwrap: () => false, probeLandlock: () => 'unusable' }`. Workspaces are created under `homedir()` not `tmpdir()`, because "Seatbelt's wholesale temp-directory grants" would otherwise make `workspace-write` pass vacuously.

**CI (`.github/workflows/sandbox.yml`, master-push only, outside the PR verdict)** fans out over OS×kernel, not node versions:

| leg | runner |
|---|---|
| `ubuntu-latest` | bwrap (installs bubblewrap, `sysctl kernel.apparmor_restrict_unprivileged_userns=0` best-effort) |
| `ubuntu-24.04` | landlock (builds the musl launcher from `native/landlock-run`) |
| `ubuntu-24.04-arm` | landlock |
| `macos-latest` | seatbelt (+ runs the whole unit suite for darwin parity) |

**The false-green guard is the best detail here.** The e2e files `describe.skipIf` themselves when the runner is missing — which on the one platform that exists to prove it would be a silent pass. So CI greps vitest's own summary:

```bash
echo "$out" | grep -qE 'Test Files[[:space:]]+2 passed \(2\)'
```

with `NO_COLOR: 1` set because "vitest force-enables ANSI color under GITHUB_ACTIONS even without a TTY, which would thread escape codes through the summary line the run-guard greps."

**Platform skips** are explicit lists in `vitest.config.ts`, not globs. On win32, excluded: `packages/shell/bash-local`, `bash-sandbox`, `tool-bash`, `packages/hooks/*`, `terminal-bash`, `sandbox-local`. Deliberately **kept**: pwsh suites — "PowerShell ships with Windows, so they run natively here." Windows-only sources (`sandbox-windows-acl`, koffi/Win32) are excluded from the *Linux* coverage lane only. There's also a Wine-based Windows leg plus a native Windows leg in `ci.yml`.

---

### 5. End-to-end / benchmark layer

**Real-API e2e**: 136 `*.e2e.ts` files, ~1.27 MB. `vitest.e2e.config.ts` — `testTimeout: 120_000`, `retry: 2`, `maxWorkers` from `DSH_E2E_MAX_WORKERS` (default 4, CI uses 14). Each suite self-skips without its credential.

`.github/workflows/e2e.yml` (`name: E2E (real DeepSeek API)`) runs on push/PR/`workflow_dispatch` and nightly `cron: '17 0 * * *'` ("00:17 UTC = 08:17 Asia/Shanghai — off the top-of-hour cron stampede"). Two guards worth noting:

- **Preflight hard-fails on a missing key**, because "the e2e suites self-skip when the key is absent, so a missing/misconfigured secret would otherwise pass as 'all skipped'."
- A large security comment: *"SECURITY — NEVER change this trigger to `pull_request_target`."* The fork/Dependabot skip keys on `pull_request.user.login`, not `github.actor`, because "a maintainer reopening a Dependabot PR would make github.actor human while the PR is still keyless."

E2E runs with `DSH_EXAMPLE_MODE=lib` — built artifacts under plain `node`, "the shape a real consumer runs."

**Anti-cheating assertion policy** (`docs/testing.md`) — the sharpest paragraph in the repo:

> "**Verify the world, not the self-report.** An e2e assertion re-runs the command or re-reads the file externally; a keyword probe on the agent's own output lets a cheating agent pass. Assert untouched files are byte-identical."

**Benchmarks: there is no benchmark harness in this repo.** Verified by path scan of all 7,903 blobs: zero matches for `terminal-bench`, zero for `swe-bench`/`swebench`, and the only `bench` hit is `BENCHMARK.md`, which is **3 lines** in full:

> "Follow `docs/user/guide/python-sdk.md` to install the SDK and run the `jsonrpc-agent` minimal variant. Use separate workspaces and session IDs for independent benchmark tasks."

So: it tells you how to *drive* the harness from a benchmark runner you supply, and the only nondeterminism guidance is "separate workspaces and session IDs per task." No pass@k, no n-runs, no seeds, no task set, no verifier. The reported V3.2 agentic scores were not produced by anything in this repo.

Nondeterminism is instead handled by **eliminating** it: replay for goldens, event-signal settling instead of sleeps in property tests, `retry: 2` only in the real-API lane.

---

### 6. What they deliberately do NOT test (documented)

The `.agents/notes/` corpus is 744 English notes with a decision-status taxonomy: `implemented` 561, `archived` 144, `proposed` 26, **`rejected` 11**. Every package README also ends with a `## Known Limitations and Deferred Work` section.

**Proposed but not implemented** (`.agents/notes/proposed/testing/`):
- `2026-06-11-mutation-testing.md` — Stryker. The rationale is a direct critique of their own gate: *"The per-file 100% coverage gate proves every line executes under test — not that any assertion would notice if the line were wrong. Under agent-written tests, coverage pressure can produce execution-without-assertion."*
- `2026-06-11-deterministic-and-stress-testing.md`

**Rejected**, with reasoning recorded (`.agents/notes/rejected/simplification/`): `truncate-interrupted-turns`, `drop-durable-step-boundaries`, `assembled-assistant-messages-only`, `fold-compaction-package-split`, `builtin-timer-promises-for-hand-rolled-sleeps`, `dependency-swaps-rejected-by-nih-audit`; and `rejected/feature/evaluate-landstrip-for-windows-sandbox-rung`.

**Explicit coverage debt**, in-config with TODO owners: a long `TODO(gui)` block excluding `packages/client/ui-*`, `packages/client/web`, `packages/host/webserver`, `packages/extensions/*` — "Client/web UI files whose remaining branches need a browser-grade harness the jsdom lane doesn't cover yet." Also excluded: `packages/typert/generator/src/*.ts`, because "per-file coverage would put whole-workspace compiler analysis under v8 instrumentation — the coverage lane's longest tail."

**Named untested boundaries**: snapshot harvest requires raw JSONL (`persistenceCompression: 'none'`) — "compressed JSONL and SQLite compositions have no snapshot-harvest path." Also: `retry.spec.ts` deliberately does *not* retry `STREAM_CLOSED`; extending it "requires a separate decision with its own cost, latency, and duplicate-generation trade-offs."

And a caveat on their own gate, from `docs/testing.md`: *"An uncovered line is often dead code the gate is correctly flagging for deletion, not a missing test to bolt on. Line coverage is necessary, never sufficient — it proves lines ran, not that the feature works as shipped."*

---

### 7. Scale

Computed from the git tree blob sizes (9,161 entries, `truncated: false`):

| | files | bytes |
|---|---|---|
| `*.spec.ts(x)` | 872 | 12.4 MB |
| `*.e2e.ts` | 136 | 1.27 MB |
| `*.snapshot.ts` | 22 | 215 KB |
| **all test source** | **1,030** | **13.9 MB** |
| everything under `tests/` (incl. fixtures) | 1,859 | 17.2 MB |
| `packages/*/*/src/**.ts(x)` | 1,381 | 10.5 MB |

**Test source outweighs product source ~1.3:1 by bytes.** Top packages by spec count: `client` 251, `core` 58, `llm` 40, `host` 33, `session` 32, `subagent` 28, `fs` 21, `sandbox` 21, `shell` 19, `hooks` 18.

**Coverage gate: per-file 100% statements/branches/functions/lines** on `packages/*/*/src`, `provider: 'v8'`. Comment in `vitest.config.ts`: *"100% or it doesn't merge (docs/testing.md: excessive tests are welcome). Per-file so a well-covered big file can't subsidize a bare one."* Every `/* v8 ignore */` must carry a reason. A custom CJS reporter (`scripts/coverage-uncovered-locations.cjs`) prints `path:line:col` for each uncovered statement, "because the built-in threshold ERRORs name only the file."

Pool config: everything runs `pool: 'forks'` — *"Node 24 has aborted in its CJS lexer (v8::ToLocalChecked Empty MaybeLocal in cjs_lexer::Parse) from worker threads on macOS, Linux, and Windows."* Eight named `processBoundTests` get their own project.

CI lanes (`ci.yml`, 490 lines, PR-gated): `node 24 / static`, `node 24 / coverage`, `node 24 / snapshots and artifacts`, `node-compat` (22.19 + 26), `python 3.10 keyless SDK`, `python-runtime`, `windows / wine blocking`, `windows-native`, converging on an `all-checks-passed` job that fails on `failure || cancelled || skipped`.

---

### Unverified / could not confirm

- **Which scaffold produced DeepSeek's published SWE-bench Verified / Terminal-Bench 2.0 numbers.** Web search surfaced claims about Terminus 2, mini-swe-agent, and OpenHands, and an arXiv id `2512.02556` for a V3.2 report. **I did not fetch the paper or any of those repos**, so treat all of it as unconfirmed. What I *can* state from evidence: nothing in `deepseek-ai/deepseek-harness` implements those benchmarks.
- **Test line counts** are byte-size-derived from the git tree; I read full text for ~20 files only. File counts and byte totals are exact; any per-file line count I did not print is not asserted.
- **Whether the e2e/sandbox lanes are actually green in CI right now** — I read workflow definitions, not run history.
- `packages/core/agent-loop/tests/loop.spec.ts` (63 KB), `resume.spec.ts`, `scope-lifecycle.spec.ts`, `interception.spec.ts` — listed with real sizes from the tree but **not fetched**; I did not read their test titles.
- `.github/workflows/e2b-e2e.yml`, `pi-ai-provider-e2e.yml`, `landlock-run.yml`, `expected-filenames.yml` exist in the tree but I did not open them.
- Transient infrastructure note: `api.github.com/repos/...` and some `raw.githubusercontent.com` fetches intermittently returned `CONNECT tunnel failed, response 503` through the local proxy; I retried each up to 6× and every file reported above returned HTTP 200. Nothing in this report is reconstructed from memory.

**Sources:**
- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
- [deepseek-harness/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md)
- Files read raw at `https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/<path>`: `vitest.config.ts`, `vitest.shared.ts`, `vitest.e2e.config.ts`, `vitest.snapshot.config.ts`, `package.json`, `BENCHMARK.md`, `docs/testing.md`, `scripts/test-invariants.ts`, `packages/core/agent-loop/tests/{mock-adapter.ts,cancel.spec.ts,contract-regressions.spec.ts,tool-calls.spec.ts,properties.spec.ts,request-reconstruction.spec.ts}`, `packages/llm/llm-deepseek/tests/mock-server.ts`, `packages/llm/llm-retry/tests/transport-recovery.spec.ts`, `packages/compaction/compaction/tests/tool-pairing.spec.ts`, `packages/sandbox/sandbox-local/tests/{seatbelt.e2e.ts,landlock.e2e.ts}`, `packages/test-support/acp-snapshot/{README.md,src/normalize.ts}`, `packages/test-support/llm-replay/README.md`, `.github/workflows/{sandbox.yml,e2e.yml,ci.yml}`, `.agents/notes/proposed/testing/2026-06-11-mutation-testing.md`, `.agents/notes/implemented/testing/2026-07-25-scriptable-llm-wire-fault-server.md`
