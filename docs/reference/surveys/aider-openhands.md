# How aider and OpenHands test their agent loops — verified findings

All paths below were fetched from GitHub during this session (raw.githubusercontent.com / github.com tree pages / api.github.com). `gh` and `curl` were unavailable (no network from Bash; `curl: (56) CONNECT tunnel failed`), so everything went through WebFetch.

**Repo-state note (important, and probably news):** `All-Hands-AI/OpenHands` `main` is **no longer the Python monolith**. It is now a TypeScript/Electron/React app (`package.json`, `vite.config.ts`, `playwright.config.ts`, `electron/`, `src/`). The Python agent lives in a separate repo, **`OpenHands/agent-sdk`** (importable as `openhands.sdk`). The Python layout you asked about (`tests/unit`, `tests/runtime`, `evaluation/`) exists at tag **`0.62.0`**, the last release of the old structure. I covered all three.

---

## 0. The headline answer: three *different* fake-LLM designs, all verified

| Mechanism | Where | Matching key | Verdict |
|---|---|---|---|
| **Ordered index + content assertion** | OpenHands `tests/integration/conftest.py` (deleted Oct 2024) | `prompt_{id:03}.log` by counter, then *normalized string equality* against the recorded prompt | The interesting one — ordered dispatch **plus** a regression check on the request |
| **Ordered queue, request ignored** | `openhands.sdk.testing.TestLLM` (agent-sdk) | `deque.popleft()`, incoming messages discarded | Current OpenHands design |
| **Local HTTP server speaking OpenAI wire protocol, script injected over an admin API** | OpenHands `tests/e2e/mock-llm/scripts/mock-llm-server.py` | ordered trajectory registered/activated per test via `/admin/trajectory/*` | Current OpenHands e2e design |
| **Monkeypatch the method, not the wire** | aider | `coder.send = mock_send` — a closure that sets `partial_response_content` | Simplest; no protocol emulation at all |

---

## 1. OpenHands — the recorded-response matching mechanism (your key question)

### 1a. The historical prompt-hash-style cassette system — `tests/integration/conftest.py`

Verified it existed and was removed. Commit history for that exact path (`api.github.com/repos/All-Hands-AI/OpenHands/commits?path=tests/integration/conftest.py`):

- `84a578ad20cef8c5475f2305e374918eba56e2fd` (2024-10-16) — **"[test] remove integration tests from CI & move them into evaluation (#4447)"**
- `0a03c802f57a8abc82d59950a1778715bce294b3` (2024-09-26) — "Refactor llm.py (#4057)" ← last live version, which I read
- `70723581ac3d9e6be4f37eabd71867b09d0d2a0e` (2024-08-21) — "(test) enhance conftest to lessen false positives in integration tests (#3512)"
- `01ae22ef57d497f7cfdfba96a99b96af06be5a05` (2024-08-19) — "Rename OpenDevin to OpenHands (#3472)"

**The mechanism (verified against that blob):**

- **Not a hash.** In-file comment: *"this assumes all `response_(*).log` filenames are in numerical order, starting from one"*.
- Filename patterns: `prompt_{"{0:03}".format(id)}.log` and `response_{"{0:03}".format(id)}.log`. Plus a `user_responses.log` for scripted human turns.
- Signature: `def get_mock_response(test_name: str, messages: str, id: int) -> str:` — it reads **the single file for the current counter id**, not a glob-and-search.
- A module-level counter advances per completion call: `cur_id += 1`.
- Matching is **exact string equality after aggressive normalization**, via `filter_out_symbols()`, which strips: hostname patterns, poetry path specifics, size params, SHA256 hashes, whitespace/newlines, and non-alphanumeric characters. On mismatch it prints a diff.
- Mock directory is built from `MOCK_ROOT_DIR` (composed of `script_dir`, `test_runtime`, `DEFAULT_AGENT`) joined with the test name — so cassettes are keyed by **(runtime, agent, test)**, which is why they had a runtime dimension at all.
- Injection point: `monkeypatch.setattr('openhands.llm.llm.litellm_completion', partial(mock_completion, test_name=test_name))` — they patch the *litellm entry symbol inside their own module*, not litellm itself.
- Required env: `SCRIPT_DIR`, `PROJECT_ROOT`, `WORKSPACE_BASE`, `TEST_RUNTIME` (asserted at import).
- `patch_completion` also stubs cost (`1` USD) and disables vision.

> **Design takeaway for your fake-LLM:** they got ordered dispatch *and* request-body regression detection out of one artifact. The prompt file is simultaneously the routing key and a golden snapshot of the request. The normalization list (hostnames, paths, hashes) is the honest cost of that: it exists purely to stop non-semantic drift from failing the diff.

*Caveat:* two passes over the file gave contradictory answers on the no-match path — one reported `raise SecretExit('\n\n***** Mock response for prompt is not found *****\n')`, the other reported a printed diff with implicit `None`. Both may be true on different branches (missing file vs. content mismatch). See "unverified" below.

### 1b. Current: `openhands.sdk.testing.TestLLM`

`https://github.com/OpenHands/agent-sdk/blob/main/openhands-sdk/openhands/sdk/testing/test_llm.py` (11,797 bytes; the module is just `__init__.py` + `test_llm.py`).

- `class TestLLM(LLM):` and `class TestLLMExhaustedError(Exception):` — *"Raised when TestLLM has no more scripted responses."*
- Factory: `def from_messages(cls, messages: list[Message | Exception], *, model: str = "test-model", ...)`
- Dispatch: `item = self._scripted_responses.popleft()` then `self._call_count += 1`. **Incoming messages are ignored entirely** (args carry `# noqa: ARG002`).
- Errors: `if isinstance(item, Exception): raise item` — same contract as `unittest.mock` `side_effect`.
- Fabricates a real litellm object: `ModelResponse(id=f"test-response-{self._call_count}", choices=[Choices(message=litellm_message, index=0, finish_reason="stop")], ...)`, with tool calls serialized as `{'id': tc.id, 'type': 'function', 'function': {'name': tc.name, 'arguments': tc.arguments}}`.
- **Does not record received requests.**

### 1c. Current e2e: a mock LLM **HTTP server**

`tests/e2e/mock-llm/scripts/mock-llm-server.py` (15,110 bytes) — *"Mock OpenAI-compatible LLM server"*, port 9999.

- Endpoints: `/v1/chat/completions`, `/chat/completions`, `/completions`, `""`, `/` (health), plus a control plane: **`/admin/reset`**, **`/admin/trajectory/register`**, **`/admin/trajectory/activate`**, **`/admin/requests`**.
- Supports SSE streaming (`if body.get("stream"): ... self._send_streaming(raw, include_usage=...)`) and tool calls (`tool_calls = message.get("tool_calls") or []`).
- It **wraps `TestLLM`**: `from openhands.sdk.testing import TestLLM, TestLLMExhaustedError`; `MockLLMHandler.test_llm = TestLLM.from_messages(list(msgs))` on activate, and `response = self.test_llm.completion([])` per request — **zero per-request branching**.
- Exhaustion → `_send_error(500, "server_error", f"Mock LLM exhausted after {self.test_llm.call_count} calls")`.
- `/admin/requests` returns `{"requests": payload}` — the recorded completion request bodies. This is how a Playwright test asserts on *what the agent sent* (e.g. that image content was included). State is **in-memory only** (`_completion_requests: list = []`, `_named_trajectories: dict[str, ...] = {}`).
- Trajectory entries are `Message(role="assistant", content=[TextContent(...)], tool_calls=[MessageToolCall(...)])` built by `build_trajectory()`.

> **Design takeaway:** this is the cleanest separation I found — the fake-LLM *policy* (ordered script) is a library object; the *transport* is a thin OpenAI-protocol shim; the *test* controls both via an admin API rather than by patching. It works across process boundaries, which is why it can drive a full Electron/Docker stack.

### 1d. Trajectory replay (a different thing entirely)

`tests/runtime/test_replay.py` @ `0.62.0` replays **recorded agent actions**, not LLM responses:
- `replay_trajectory_path=str((Path(__file__).parent / 'trajs' / f'{trajectory_name}.json').resolve())`
- Fixtures: `basic.json`, `basic_gui_mode.json`, `wrong_initial_state.json`, `basic_interactions.json`
- Assertion: `assert state.agent_state == AgentState.FINISHED`

---

## 2. What OpenHands asserts about the loop

**Tool_use/tool_result pairing — the strongest example I found.** `tests/unit/memory/test_conversation_memory.py` @ `0.62.0` tests `ConversationMemory.process_events()`:
- `test_matched_tool_calls_are_unchanged` — *"All tool calls have matching responses, should remain unchanged"*
- `test_partial_matched_tool_calls_retains_matched` — *"When there are both matched and unmatched tools calls in a message, retain the message and only matched calls"*
- `test_tool_response_without_call_is_removed` — orphaned `tool` messages get dropped
- `test_process_events_partial_history` — the condensed/truncated-history case, i.e. dangling tool calls after compaction

No real LLM: `prompt_manager = MagicMock(spec=PromptManager)`.

**Controller loop.** `tests/unit/controller/` @ `0.62.0`: `test_agent_controller.py`, `test_agent_controller_loop_recovery.py`, `test_agent_delegation.py`, `test_is_stuck.py`, plus `state/`. Faking is `agent = MagicMock(spec=Agent)` with `mock_agent.step = agent_step_fn`. Verified assertions:
- `assert state.iteration_flag.current_value == 3` (max-iteration extension)
- `assert controller.state.agent_state == AgentState.ERROR` (budget exceeded)
- `assert state.last_error == 'AgentStuckInLoopError: Agent got stuck in a loop'`
- Named tests include `test_context_window_exceeded_error_handling`, `test_step_max_budget`, `test_budget_reset_on_continue`, `test_condenser_metrics_included`, `test_react_to_content_policy_violation`.

**Compaction.** `tests/unit/memory/condenser/test_condenser.py`, `test_conversation_window_condenser.py`; `tests/unit/memory/test_view.py`.

**Retry.** `tests/unit/llm/test_api_connection_error_retry.py`, `test_llm.py`, `test_acompletion.py`, `test_llm_fncall_converter.py` (the non-native-function-calling prompt↔tool conversion).

**Current SDK (`agent-sdk`) — `tests/sdk/agent/` is 44 files and reads like a catalogue of loop failure modes:** `test_fix_malformed_tool_arguments.py`, `test_nonexistent_tool_handling.py`, `test_tool_call_recovery.py`, `test_tool_call_compatibility.py`, `test_agent_context_window_condensation.py`, `test_parallel_executor.py` / `test_parallel_executor_locking.py` / `test_parallel_execution_integration.py`, `test_reasoning_only_responses.py`, `test_non_multimodal_image_input.py`, `test_sanitize_json_control_chars.py`, `test_message_while_finishing.py`, `test_astep_releases_state_lock.py`, `test_agent_init_state_invariants.py`.

`tests/sdk/agent/test_tool_call_recovery.py` fakes with `with patch("openhands.sdk.llm.llm.litellm_completion", return_value=resp):` and asserts recovery behaviour: a reasoning-only or empty response gets a corrective nudge — `assert len(corrective_nudges) == 1`, `assert "function call" in nudge_text.text`.

**Crash-recovery / unmatched actions** — `tests/sdk/conversation/test_get_unmatched_actions.py` (9 tests). Identifies `ActionEvent`s with no matching `ObservationEvent` / `UserRejectObservation` / `AgentErrorEvent`, keyed by `tool_call_id`, so a restart doesn't re-execute a tool. Tests include `test_crash_recovery_scenario_prevents_duplicate_execution`, `test_agent_error_event_matching_by_tool_call_id`, `test_non_executable_action_is_not_considered_unmatched`. Also `tests/sdk/conversation/test_interrupt.py` and `test_condense.py`.

---

## 3. OpenHands — sandbox / runtime / CI

**Runtime tests run the real thing inside Docker.** `tests/runtime/` @ `0.62.0` — 17 test files, largest: `test_bash.py` (60,215 B), `test_browsing.py` (49,869 B), `test_aci_edit.py` (25,765 B), `test_stress_remote_runtime.py` (18,038 B). `tests/runtime/README.md`: `poetry run pytest ./tests/runtime`, configured by `TEST_RUNTIME` ∈ {`docker`, `local`, `remote`, `runloop`, `daytona`}, plus `TEST_IN_CI`, `RUN_AS_OPENHANDS`, `SANDBOX_BASE_CONTAINER_IMAGE`.

`.github/workflows/ghcr-build.yml` @ `0.62.0`:
```
poetry run pytest -n 5 -raRs --reruns 2 --reruns-delay 3 -s ./tests/runtime
```
- Matrix base images: `nikolaik/python-nodejs:python3.12-nodejs22` and `ubuntu:24.04`; runner `blacksmith-4vcpu-ubuntu-2404`; Python 3.12.
- Env: `TEST_RUNTIME=docker`, `TEST_IN_CI=true`, `SANDBOX_RUNTIME_CONTAINER_IMAGE=$image_name`; **two jobs differing only by `RUN_AS_OPENHANDS` = `false` (root) vs `true`**.
- `--ignore=tests/runtime/test_browsergym_envs.py`.
- Note `--reruns 2 --reruns-delay 3`: flakiness is absorbed, not eliminated.

`.github/workflows/py-tests.yml` @ `0.62.0`: `PYTHONPATH=".:$PYTHONPATH" poetry run pytest --forked -n auto -s ./tests/unit`, Python 3.12, `--cov=openhands --cov-branch`, `MERGE_COVERAGE_FILES: true`. Plus a **CLI-runtime smoke**: `TEST_RUNTIME=cli poetry run pytest -n 5 --reruns 2 --reruns-delay 3 -s tests/runtime/test_bash.py`.

**On "did the OS sandbox actually block it?" — I found no such test in either project.** OpenHands isolation is *container-level*, and the tests assert tool behaviour inside the container rather than that an escape was denied. What exists instead is a **policy** layer, `tests/sdk/security/`: `test_confirmation_policy.py`, `test_security_analyzer.py`, `test_llm_security_analyzer.py`, `test_toolshield_llm_analyzer.py`, `test_shell_ast.py`, `test_shell_parser.py`, `test_shell_parser_node_shapes.py`, plus `defense_in_depth/` (`test_adversarial.py`, `test_shell_parser_bypasses.py`, `test_shell_semantics.py`, `test_policy_rails.py`, `test_ensemble.py`, `test_field_cap.py`) and `grayswan/`. That is "we parsed the command and refused it", not "the kernel refused it".

**Platform skipping** is filesystem/OS capability based, not sandbox based — `agent-sdk/tests/platform_utils.py` defines `symlink_or_skip()`, `require_case_sensitive_fs()` (macOS/Windows), `supports_posix_execute_bits()` (`os.name == "nt"`), `can_fork_test_process()` (no `os.fork()`, or under pytest-xdist), `set_address_space_limit_if_available()` (no `resource.RLIMIT_AS`).

`agent-sdk/.github/workflows/tests.yml`: Python `'3.13'`; runners `blacksmith-2vcpu-ubuntu-2404`, `ubuntu-latest`, `windows-latest`. Path-scoped job triggers (`openhands-sdk/**` → sdk tests, `openhands-tools/**` → tools tests, `tests/**` → cross). SDK runs `-n auto`; **tools run `--forked` serialized**. Coverage is measured but toothless: `--cov-fail-under=0`.

---

## 4. OpenHands — e2e and benchmarks

**Mock-LLM Playwright suite** (current `main`), per `AGENTS.md` and `.github/workflows/mock-llm-e2e.yml`:
- Configs: `playwright.mock-llm.config.ts`, `playwright.mock-llm-docker.config.ts`, `playwright.live.config.ts`.
- Commands: `npm run test:e2e:mock-llm`, `npm run test:e2e:mock-llm:docker`, `npm run test:e2e:live`.
- The config *"starts the full `agent-canvas` stack via `bin/agent-canvas.mjs` — the same binary that `npx @openhands/agent-canvas` executes when users install the npm package."* Three processes: mock LLM on 9999, full stack on 18300, public-mode static server on 18301. Isolated state dir `.tmp/mock-llm-state`, random 32-byte session key per run.
- **Determinism by construction:** `workers: 1`, `mode: "serial"` per describe block, *"Each spec is self-contained (configures its own LLM profile, resets mock LLM in `afterEach`)."*
- Spec dirs under `tests/e2e/mock-llm/`: `settings/`, `conversations/`, `automations/`, `onboarding/`, `regressions/`, `mcp/`, `home/`, `files/`, `backends/`, `skills/`.
- **Selective execution:** `tests/e2e/mock-llm/test-mapping.json` maps source paths → test subdirectories, resolved by `scripts/resolve-affected-tests.mjs`; on failure or `__ALL__` it falls back to the full suite. `regressions/` is *always* included.
- CI: `ubuntu-24.04`, 30 min job / 10 min Playwright timeout, Node pinned `24.15.x`, results retained 14 days, PR comment via `upsert-pr-comment.mjs`, report via `render-mock-llm-report.mjs`. A custom `DoneMarkerReporter` writes an `.all-passed` marker and the workflow polls it — a run that hangs in teardown but left the marker is still green.

**Live e2e** needs a real key (`LIVE_E2E_LLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `LLM_API_KEY`); helpers in `tests/e2e/live/utils/agent-server-conversation.ts`.

**Benchmarks.** `evaluation/benchmarks/` @ `0.62.0` has **31** harnesses, including `swe_bench`, `multi_swe_bench`, `swe_perf`, `visual_swe_bench`, `terminal_bench`, `aider_bench`, `commit0`, `the_agent_company`, `webarena`, `visualwebarena`, `miniwob`, `gaia`, `agent_bench`, `algotune`, `nocode_bench`. **Not in normal CI** — `.github/workflows/run-eval.yml` triggers on a PR *label* (`run-eval-1`, `run-eval-2`, …), on `release: published`, or `workflow_dispatch`; instance counts 1 / 2 / 50 / 100 (releases default 50); results posted back to the PR or to `MASTER_EVAL_ISSUE_NUMBER`, plus Slack.

**agent-sdk live-LLM integration tests** — `tests/integration/`, run by `run_infer.py`:
```
uv run python tests/integration/run_infer.py --llm-config '{"model": "litellm_proxy/anthropic/claude-sonnet-4-5-20250929"}'
```
- *"real LLM calls to test end-to-end functionality"*.
- Naming convention **is** the policy: `t*.py` integration tests are *"**REQUIRED** … must pass for releases"*; `b*.py` behaviour tests are *"**OPTIONAL** … Failures don't block releases"*; `c*.py` condenser stress tests are *"OPTIONAL, NON-BLOCKING"*. Examples: `t01_fix_simple_typo`, `t03_jupyter_write_file`, `t07_interactive_commands`, `b01_no_premature_implementation`, `c02_hard_context_reset`, `c04_token_condenser`.
- `.github/workflows/integration-runner.yml`: triggered by PR label `integration-test` / `behavior-test` or manual dispatch; model matrix default `gpt-5.5,deepseek-v4-flash,minimax-m2.7,gemini-3.1-pro,claude-sonnet-4-6`; condenser tests limited to 2 models to cut cost; `fail-fast: false`; results merged by `consolidate_json_results.py` → `generate_markdown_report.py`. **No pass threshold in the workflow** — success rates are reported, and a human decides. There is also an `early_stopper.py` in `tests/integration/`.
- The SDK README says integration tests run **nightly on a schedule**, decoupled from path-based selection.

---

## 5. aider

### 5a. Layout and scale
`tests/`: `__init__.py`, `basic/`, `browser/`, `fixtures/`, `help/`, `scrape/`. Verified sizes for `tests/basic/` (33 files, **~536 KB** of test source):

`test_commands.py` 90,081 · `test_main.py` 62,437 · `test_coder.py` 55,504 · `test_repo.py` 29,480 · `test_reasoning.py` 26,048 · `test_io.py` 24,607 · `test_models.py` 22,037 · `test_onboarding.py` 19,872 · `test_repomap.py` 18,820 · `test_editblock.py` 17,579 · `test_wholefile.py` 12,313 · … down to `test_run_cmd.py` 238.

`tests/browser/test_browser.py` (1 file). `tests/scrape/`: `test_scrape.py`, `test_playwright_disable.py`. `tests/help/test_help.py`. `tests/fixtures/`: `languages/`, `sample-code-base/`, `sample-code-base-repo-map.txt`, `chat-history-search-replace-gold.txt`, `chat-history.md`, `watch.py|.js|.lisp`, `watch_question.js`.

Coverage is concentrated on **edit-format parsing, `/commands`, CLI arg handling, git repo interaction, and repo-map** — not on a long agentic loop, because aider's loop is short by design.

### 5b. How aider fakes the model — monkeypatch the method, not the wire

This is the deliberately cheap option, and it's worth seeing exactly how cheap. From `tests/basic/test_coder.py`:

```python
def mock_send(*args, **kwargs):
    coder.partial_response_content = "ok"
    coder.partial_response_function_call = dict()
    return []

coder.send = mock_send
coder.run(with_message="hi")
```

For edit tests, the closure writes a literal SEARCH/REPLACE block into `partial_response_content`:

```python
def mock_send(*args, **kwargs):
    coder.partial_response_content = f"""
Do this:

{str(fname)}
<<<<<<< SEARCH
=======
new
>>>>>>> REPLACE

"""
    coder.partial_response_function_call = dict()
```

No HTTP, no litellm, no response object. `Model("gpt-3.5-turbo")` is instantiated but only for tokenizer/metadata. 8 distinct `mock_send` closures in `test_coder.py` alone; everything else is `MagicMock` on `io`, `repo`, `scraper`.

At the layer below, `tests/basic/test_sendchat.py` **does** patch `litellm.completion` — verifying `mock_completion.call_count == 3` after rate limits, distinguishing retryable (500) from non-retryable (400), and asserting `called_kwargs['tools'][0]['function'] == mock_function`.

`tests/basic/test_reasoning.py` (26 KB) fakes at a third layer again: `with patch.object(model, "send_completion", return_value=(mock_hash, mock_completion))`, with local `class MockCompletion` and `class MockStreamingChunk` (`self.choices[0].delta.content = ...`). It's the one place aider tests **streaming assembly** — asserting reasoning precedes content, `REASONING_START`/`REASONING_END` tags are emitted, and the last `update` call carries `final=True`. Tests: `test_send_with_reasoning_content{,_stream}`, `test_send_with_think_tags{,_stream}`, `test_remove_reasoning_content`, `test_simple_send_with_retries_removes_reasoning`.

### 5c. Loop assertions — `sanity_check_messages`

aider's whole "is the history still valid" story is one 20-line function, `aider/sendchat.py`, fetched in full:

```python
def sanity_check_messages(messages):
    """Check if messages alternate between user and assistant roles.
    System messages can be interspersed anywhere.
    Also verifies the last non-system message is from the user.
    Returns True if valid, False otherwise."""
```
It raises `ValueError("Messages don't properly alternate user/assistant:\n\n" + turns)` on a violation. A sibling `ensure_alternating_roles()` *repairs* history by inserting empty opposite-role messages.

Three tests in `test_coder.py` call it as the post-condition of an abnormal exit:
- `test_keyboard_interrupt_handling` — sanity-check, `list(coder.send_message("Test message"))` with an interrupt injected, sanity-check again
- `test_token_limit_error_handling` — `mock_send` sets `partial_response_content = "Partial response"` then `raise FinishReasonLength()`; history must still be valid
- `test_message_sanity_after_partial_response` — plus `self.assertEqual(coder.cur_messages[-1]["role"], "assistant")`

Note what's absent: aider has **no tool_use/tool_result ids to pair**, because SEARCH/REPLACE blocks travel in message *content*. The whole class of dangling-tool-call bugs doesn't exist for them. That is the single biggest reason their fake-LLM can be this simple.

### 5d. Repo-map tests, incl. a golden file

`tests/basic/test_repomap.py` — ~40 tests. Mostly substring presence (`self.assertIn("function1", initial_map)`, `self.assertIn(symbol, result, f"Key symbol...not found in repo map")`), covering ~39 languages (`test_language_c/cpp/d/dart/elixir/gleam/haskell/java/javascript/kotlin/lua/php/python/ruby/rust/typescript/tsx/zig/csharp/elisp/elm/go/hcl/arduino/chatito/clojure/commonlisp/pony/properties/r/racket/solidity/swift/udev/scala/ocaml/ocaml_interface/matlab/bash`) via `tests/fixtures/languages/`.

**One true golden test**: `test_repo_map_sample_code_base` does `self.assertEqual(generated_map_str, expected_map)` against `tests/fixtures/sample-code-base-repo-map.txt`. Plain `assertEqual` on a checked-in text file — **no snapshot library** (no syrupy/snapshottest/jest-style). Same pattern for `tests/fixtures/chat-history-search-replace-gold.txt`.

**aider snapshots nothing about the request body sent to the model.** No test I found asserts on the assembled prompt. That's a real gap relative to the old OpenHands `prompt_NNN.log` design.

### 5e. Benchmark — exercism, edit-format success

`benchmark/` (22 files; `benchmark.py` is 34,621 B). Per `benchmark/README.md`, based on *"the [Exercism](https://github.com/exercism/python) coding exercises"*, run in Docker:
```
./benchmark/docker.sh
pip install -e .[dev]
./benchmark/benchmark.py a-helpful-name-for-this-run --model gpt-3.5-turbo --edit-format whole --threads 10 --exercises-dir polyglot-benchmark
./benchmark/benchmark.py --stats tmp.benchmarks/YYYY-MM-DD-HH-MM-SS--a-helpful-name-for-this-run
```

Metrics emitted: `pass_rate_1`, `pass_rate_2`, `pass_num_1`, **`percent_cases_well_formed`**, `error_outputs`, `num_malformed_responses`, `syntax_errors`, `indentation_errors`, `exhausted_context_windows`, `lazy_comments`, `test_timeouts`, `prompt_tokens`, `completion_tokens`, `seconds_per_case`, `test_cases` (e.g. `225`).

The edit-format metric you asked about:
```
pct_well_formed = 1.0 - res.num_with_malformed_responses / res.completed_tests
```
incremented by `if results.get('num_malformed_responses'): res.num_with_malformed_responses += 1`.

`tries: int = typer.Option(2, '--tries', '-r', ...)` — hence `pass_rate_1` (first shot) and `pass_rate_2` (after seeing test failures). This two-number reporting **is** their nondeterminism handling, alongside running exercises *"in a random order"* and up to 10 threads. There is no n-runs/seed/pass@k machinery.

Per-exercise results land in `.aider.results.json`. Tests execute via `subprocess.run()` with per-language commands: `'.py': ['pytest']`, `'.rs': ['cargo', 'test', '--', '--include-ignored']`, `'.go': ['go', 'test', './...']`, `'.js': ['/aider/benchmark/npm-test.sh']`.

**Manual, never in CI.** `benchmark/test_benchmark.py` (1,535 B) tests only `cleanup_test_output()` — separator-line normalization. That's the extent of the benchmark harness's own unit tests.

### 5f. aider CI
`.github/workflows/`: `ubuntu-tests.yml`, `windows-tests.yml`, `pre-commit.yml`, `docker-build-test.yml`, `docker-release.yml`, `release.yml`, `pages.yml`, `issues.yml`, `check_pypi_version.yml`, `windows_check_pypi_version.yml`.

Both test workflows: matrix `["3.10", "3.11", "3.12", "3.13", "3.14"]`, env `AIDER_ANALYTICS: false`, command is bare **`pytest`** — no deselects, no markers, no split. Ubuntu installs `libportaudio2` and uses `fetch-depth: 0` (the git tests need real history). `paths-ignore`: `aider/website/**`, `README.md`, `HISTORY.md`, `.github/workflows/*` except itself.

Since the whole suite runs unconditionally on 5 Python versions × 2 OSes, **every test must be fast and offline** — which is itself the enforcement mechanism for "no real API calls in tests."

---

## 6. What each project deliberately does *not* test

**aider** (inferred from structure, not from a written rationale — no ADR found):
- No assertion on the request body / prompt sent to the model.
- No sandbox tests — aider has no sandbox; shell commands go through confirmation prompts, and there's no OS-level enforcement to assert on.
- No benchmark in CI; `benchmark/` is manual and cost-bearing.
- No end-to-end test that runs a real model.
- Loop validity is reduced to "roles alternate and the last one is user."

**OpenHands** — some of this *is* written down. `AGENTS.md` states the testing rules explicitly:
- *"Avoid duplicating test cases or logic. Do not assert the same condition more than once."*
- *"Do not mock the hook. Instead, mock the underlying service that the hook depends on."*
- *"Avoid brittle visual-presentation assertions. Functional CSS contracts such as style scoping and selector transformation may be tested directly."*
- Behaviour quality (`b*`) and condenser resilience (`c*`) are **explicitly non-blocking** — they measure, they don't gate.
- Coverage is explicitly not gated: `--cov-fail-under=0`.
- Deleting the whole prompt-cassette integration suite from CI (PR #4447, *"remove integration tests from CI & move them into evaluation"*) is the loudest statement of all: exact-prompt-match cassettes were too brittle to keep in the merge path, and got demoted to an eval you opt into with a label.

---

## Unverified / could not confirm

- **`get_mock_response` no-match behaviour.** Two reads of the same blob disagreed: one reported `raise SecretExit('\n\n***** Mock response for prompt is not found *****\n')`, the other reported "prints a diff, returns `None` implicitly". Likely two different branches (missing file vs. content mismatch) but I could not get verbatim source — WebFetch's summarizer **refused** a full verbatim reproduction request on copyright grounds, so all `conftest.py` details above come from targeted Q&A over the file rather than a raw read. Everything quoted from it is a short fragment the summarizer surfaced. **If this matters for your design, re-read `tests/integration/conftest.py` @ `0a03c802f5` directly.**
- **aider benchmark temperature/seed.** One pass reported "no temperature parameter is set in the provided code excerpt" — that is inconclusive (the excerpt may have been truncated). Not confirmed either way.
- **Exact line counts.** I have byte sizes, not LOC, for aider. For OpenHands `tests/unit` I got directory names but the file listing 403'd (api.github.com rate limit) — I can name 20 subdirectories (`agenthub`, `app_server`, `controller`, `core`, `evaluation`, `events`, `experiments`, `frontend`, `integrations`, `io`, `llm`, `mcp`, `memory`, `microagent`, `resolver`, `runtime`, `security`, `server`, `storage`, `utils`) but not a total file count.
- **Which specific test files agent-sdk's `tests.yml` excludes on Windows** — the workflow says it excludes some; I didn't extract the list.
- **`evaluation/integration_tests`** (the destination named in PR #4447) does not exist at `0.62.0` — `evaluation/` contains only `benchmarks/`, `static/`, `utils/`, `README.md`, `__init__.py`. Where the cassettes actually landed, and whether they survived at all, is unconfirmed.
- **Anthropic-protocol emulation.** The mock LLM server self-describes as OpenAI-compatible; whether anything speaks `/v1/messages` was not checked.
- **`agent-sdk` repo identity.** `raw.githubusercontent.com/OpenHands/agent-sdk/main/...` fetches fine and `api.github.com/repos/OpenHands/agent-sdk/...` works; `All-Hands-AI/software-agent-sdk` 404s, but one fetched page titled itself "OpenHands/software-agent-sdk". Almost certainly an org/repo rename. All agent-sdk paths I cite were fetched under `OpenHands/agent-sdk`.
- **`tests/e2e/mock-llm/reporters/`, `utils/`, `regressions/`** contents were not enumerated; `DoneMarkerReporter` is named in the workflow but I did not read its source.

**Sources:** [Aider-AI/aider](https://github.com/Aider-AI/aider) · [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) · [OpenHands/agent-sdk](https://github.com/OpenHands/agent-sdk) · [OpenHands org repositories](https://github.com/orgs/OpenHands/repositories) · [OpenHands SDK docs](https://docs.openhands.dev/sdk/getting-started)