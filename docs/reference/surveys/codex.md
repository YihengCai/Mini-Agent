# OpenAI Codex CLI (`openai/codex`) — how the agent loop is tested

**Verified against a real `--depth 1` clone of `main` @ `c9b19deb09c1841ce7acc33ddb96276030936a29`** (commit date 2026-08-23, "Distinguish Guardian review threads from subagents (#40221)"). Every path and snippet below was read from that checkout or fetched from `raw.githubusercontent.com/openai/codex/main/...`.

---

## 0. Scale (measured, `codex-rs/`, excluding `codex-rs/vendor/`)

| Bucket | Files | Lines |
|---|---|---|
| All `.rs` under `codex-rs/` | 3,279 | 1,458,189 |
| Under a `tests/` dir (integration) | 493 | 332,149 |
| Sibling `*_tests.rs` unit modules | 609 | 287,662 |
| Everything else (prod + inline `#[cfg(test)]`) | 2,177 | 838,378 |
| `.snap` (insta) files | 745 | 11,741 |

Per-crate integration test surface (files / lines / `#[tokio::test]` sites):

| Dir | Files | Lines | `#[tokio::test]` |
|---|---|---|---|
| `codex-rs/core/tests` | 143 | 132,615 | 1,227 |
| `codex-rs/app-server/tests` | 136 | 97,569 | 944 |
| `codex-rs/exec-server/tests` | 27 | 13,069 | 153 |
| `codex-rs/exec/tests` | 19 | 5,287 | 56 |
| `codex-rs/linux-sandbox/tests` | 5 | 1,909 | 35 |
| `codex-rs/tui/tests` | 9 | 1,274 | 4 |

Repo-wide grep counts: `#[tokio::test]` **7,777**; `#[test]` **8,090**; `#[ignore]` **26**; `wiremock` **1,156**; `insta::assert_snapshot` **334** / `assert_snapshot!` **486**; `test_case` **447**; `pretty_assertions` **1,398**; `mockito` **0**.

Snapshots by crate: `tui` 691, `core` 52, `ext` 1, `cli` 1.

Test entry points are single aggregated binaries: `codex-rs/core/tests/all.rs` is 7 lines and just does `mod suite;`; `codex-rs/core/tests/suite/mod.rs` (177 lines) lists ~160 modules with `#[cfg(...)]` gates.

---

## 1. Is the model faked, replayed, or real? — **Faked, four different ways. No cassettes, no VCR.**

`grep -i -e cassette -e vcr` across `codex-rs/` and `sdk/` returns **zero** hits. `mockito`: zero. Everything is a live local server speaking the OpenAI **Responses API** wire protocol.

**(a) `wiremock` HTTP mock — the default.** `codex-rs/core/tests/common/responses.rs` (1,790 lines) is the whole fake-model layer.

- `start_mock_server()` boots a `wiremock::MockServer` with `body_print_limit(BodyPrintLimit::Limited(80_000))` and pre-mounts an empty `GET /models` "so tests remain hermetic when the client queries it."
- Mocks match `method("POST").and(path_regex(".*/responses$"))`; compaction uses `.*/responses/compact$`; models uses `.*/models$`.
- SSE bodies are built from typed event constructors, not raw strings: `sse(vec![...])` plus `ev_response_created`, `ev_completed`, `ev_completed_with_tokens`, `ev_assistant_message`, `ev_message_item_added`, `ev_output_text_delta`, `ev_reasoning_item`, `ev_reasoning_summary_text_delta`, `ev_reasoning_text_delta`, `ev_function_call`, `ev_function_call_with_namespace`, `ev_custom_tool_call`, `ev_apply_patch_custom_tool_call`, `ev_apply_patch_exec_command_call_via_heredoc`, `ev_exec_command_call`, `ev_tool_search_call`, `ev_web_search_call_done`, `ev_image_generation_call`, `ev_model_verification_metadata`, `sse_failed(id, code, message)`.
- Mount helpers: `mount_sse_once`, `mount_sse_once_match`, `mount_sse_sequence`, `mount_response_once`, `mount_response_sequence`, `mount_compact_json_once`, `mount_compact_user_history_with_summary_sequence`, `mount_models_once_with_etag`, `mount_models_once_with_delay`.
- Every mount returns a `ResponseMock` that **records every request**: `requests()`, `single_request()`, `last_request()`, `saw_function_call(call_id)`, `function_call_output_text(call_id)`. `ResponsesRequest` wraps `wiremock::Request` and transparently zstd-decodes the body (`decode_body_bytes` / `is_zstd_encoding`).

**(b) Raw WebSocket server.** Same file: `start_websocket_server(connections: Vec<Vec<Vec<Value>>>)` / `start_websocket_server_with_headers` bind a `TcpListener` on `127.0.0.1:0` and use `tokio_tungstenite::accept_hdr_async_with_config` to serve `ws://.../v1/responses`, recording each request frame and streaming back the matching event sequence, with `DeflateConfig`/permessage-deflate config and configurable `accept_delay` + response headers. Used by `client_websockets.rs`, `agent_websocket.rs`, `websocket_fallback.rs`, and the WS half of `retry_after.rs`.

**(c) Hand-rolled gated SSE server.** `codex-rs/core/tests/common/streaming_sse.rs` (714 lines) — `start_streaming_sse_server(responses: Vec<Vec<StreamingSseChunk>>)`. Each chunk carries `gate: Option<oneshot::Receiver<()>>`, so a test can hold a partial SSE stream open and assert on interleaving. Doc comment: *"Starts a lightweight HTTP server that supports: GET /v1/models -> empty models response; POST /v1/responses -> SSE stream gated per-chunk, served in order."* This is what `tool_parallelism::shell_tools_start_before_response_completed_when_stream_delayed` uses.

**(d) SDK-level local proxies.** TypeScript: `sdk/typescript/tests/responsesProxy.ts` spins a `node:http` server, records `{body, json, headers}` per request, and yields SSE from a generator (`infiniteShellCall()` in `abort.test.ts`). Python: `sdk/python/tests/app_server_harness.py` has `MockResponsesServer` with `enqueue_sse`, `enqueue_assistant_message`, `requests()`, `single_request()`, `wait_for_requests()` and a `CapturedResponsesRequest` with `body_json/input/message_input_texts/header`.

**Real API calls exist but are opt-in only.** `codex-rs/core/tests/suite/live_cli.rs` header:

> `//! Optional smoke tests that hit the real OpenAI /v1/responses endpoint. They are #[ignore] by default so CI stays deterministic and free. Developers can run them locally with 'just test -p codex-core --test all --run-ignored only live_cli' provided they set a valid OPENAI_API_KEY.`

Two `#[ignore]` fns. Python SDK equivalent: `sdk/python/tests/test_real_app_server_integration.py` is `pytest.mark.skipif(not RUN_REAL_CODEX_TESTS, reason="set RUN_REAL_CODEX_TESTS=1 ...")`.

Determinism is also forced from production code via `codex_core::test_support` (`codex-rs/core/src/test_support.rs`), called from `#[ctor]` in `codex-rs/core/tests/common/lib.rs`:

```rust
#[ctor]
fn enable_deterministic_unified_exec_process_ids_for_tests() {
    codex_core::test_support::set_thread_manager_test_mode(/*enabled*/ true);
    codex_core::test_support::set_deterministic_process_ids(/*enabled*/ true);
}
```

---

## 2. What is asserted about the LOOP itself

### 2a. tool_use/tool_result pairing — asserted **globally, on every single request**, not per-test

This is the standout design. `codex-rs/core/tests/common/responses.rs` implements `wiremock::Match` for `ResponseMock`:

```rust
impl Match for ResponseMock {
    fn matches(&self, request: &wiremock::Request) -> bool {
        self.requests.lock().unwrap().push(ResponsesRequest(request.clone()));
        // Enforce invariant checks on every request body captured by the mock.
        // Panic on orphan tool outputs or calls to catch regressions early.
        validate_request_body_invariants(request);
        true
    }
}
```

`validate_request_body_invariants` (line 1673) decodes the body (zstd-aware), pulls `input[]`, and enforces a **bidirectional** pairing invariant across four call kinds — `function_call`/`function_call_output`, `custom_tool_call`/`custom_tool_call_output`, `tool_search_call`/`tool_search_output`, plus `local_shell_call` as an alternate parent for `function_call_output`:

```rust
for cid in &function_call_outputs {
    assert!(function_calls.contains(cid) || local_shell_calls.contains(cid),
        "function_call_output without matching call in input: {cid}");
}
...
for cid in &function_calls {
    assert!(function_call_outputs.contains(cid),
        "Function call output is missing for call id: {cid}");
}
```

It also asserts orphan outputs with empty `call_id` were *dropped* rather than sent (`.expect("orphan function_call_output with empty call_id should be dropped")`), with a carve-out for `tool_search_output` where `"execution": "server"`. Net effect: ~1,200 core integration tests each act as a pairing-invariant fuzzer for free.

### 2b. Cancellation / message-history validity after interrupt

`codex-rs/core/tests/suite/abort_tasks.rs` (371 lines, gated `#[cfg(not(target_os = "windows"))]`):

- `interrupt_long_running_tool_emits_turn_aborted`
- `root_turn_suspension_preserves_unfinished_turn_history`
- `interrupt_tool_records_history_entries` — kills a `sleep 60` `exec_command` mid-flight via `Op::Interrupt`, then submits a follow-up turn and asserts the synthesized `function_call_output` matches `^Wall time: ([0-9]+(?:\.[0-9])?) seconds\naborted by user$` and that elapsed ≥ 0.1s.
- `interrupt_persists_turn_aborted_marker_in_next_request` — doc comment: *"After an interrupt we persist a model-visible `<turn_aborted>` marker in the conversation history. This test asserts that the marker is included in the next `/responses` request."* Asserts exactly 2 POSTs and that `requests[1].message_input_texts("user")` contains `<turn_aborted>`.

Related: `guardian_review_cancellation.rs`, `turn_state.rs`, `pending_input.rs` (one test `#[ignore = "TODO(aibrahim): flaky"]`).

### 2c. Compaction / truncation

- `compact.rs` (4,900+ lines), `compact_remote.rs`, `compact_remote_parity.rs` (legacy-vs-V2 parity, fixed CWD `/tmp/codex_remote_compaction_parity_workspace`, fixed base64 image, fixed summary constant), `compact_resume_fork.rs`.
- `truncation.rs` (848 lines): 10 tests including `tool_call_output_truncated_only_once`, `token_policy_marker_reports_tokens`, `byte_policy_marker_reports_bytes`, `mcp_image_output_preserves_image_and_no_text_summary`, `exec_command_output_not_truncated_with_custom_limit`.
- `token_budget.rs`, `rollout_budget.rs`, `audio_truncation.rs`.
- Two compaction tests are deliberately disabled with reasons: `compact.rs:3612` and `compact_remote.rs:3066`, both `#[ignore = "behavior change covered in follow-up compaction PR"]` with a preceding comment *"Current main behavior ... is known-incorrect."*

### 2d. Retry policy — real backoff, measured, plus tracing-based telemetry capture

`codex-rs/core/tests/suite/retry_after.rs` (1,728 lines, 24 tests). It installs a custom `tracing_subscriber::Layer` (`RetryTelemetryLayer`) that sniffs `codex_otel.trace_safe` events for `retry.attempt` / `retry.delay_ms` / `retry.layer` / `retry.operation` fields and `codex_http_client::transport` events for resumption. Delays are asserted against real wall-clock windows:

```rust
const FIRST_RETRY_MIN_DELAY: Duration = Duration::from_millis(180);
const FIRST_RETRY_MAX_DELAY: Duration = Duration::from_millis(220);
const SECOND_RETRY_MIN_DELAY: Duration = Duration::from_millis(360);
const SECOND_RETRY_MAX_DELAY: Duration = Duration::from_millis(440);
```

Coverage is a 3×N matrix: transport (HTTP request / SSE stream / compact-v2 / WebSocket) × failure kind (`503 server_is_overloaded`, rate-limit message, connection failure) × `Retry-After` header present/absent. Named assertions include `responses_http_uses_local_backoff_despite_retry_after` (server says `Retry-After: 1`, client must use its own ~200ms backoff), `sse_overload_with_retry_after_is_terminal`, `connection_failures_increment_retry_telemetry_without_consuming_retry_budget`, `websocket_rate_limit_with_nested_retry_after_is_terminal`. Retry counts are injected via config: `config.model_provider.request_max_retries = Some(1); config.model_provider.stream_max_retries = Some(0);`.

Also `stream_no_completed.rs`: `retries_on_early_close`, `connection_failure_pauses_retry_budget_until_provider_is_reachable`. And `stream_error_allows_next_turn.rs::continue_after_stream_error`.

### 2e. Parallel tool calls

`codex-rs/core/tests/suite/tool_parallelism.rs` (435 lines): `read_file_tools_run_in_parallel`, `shell_tools_run_in_parallel`, `mixed_parallel_tools_run_in_parallel`, `tool_results_grouped`, `shell_tools_start_before_response_completed_when_stream_delayed`. Parallelism is proven by wall-clock (`run_turn_and_measure` returns a `Duration`), and the last one uses the gated streaming SSE server to prove tools start before `response.completed` arrives.

### 2f. Streaming assembly / tool lifecycle

`items.rs`, `client.rs` (3,845 lines), `realtime_conversation.rs`, `otel.rs`, `hooks.rs`, `review.rs`, `codex_delegate.rs`, `pending_input.rs` all consume `ev_output_text_delta` / `ev_reasoning_summary_text_delta` and assert the assembled item. `tool_lifecycle.rs` asserts hook-visible history: `tool_start_receives_conversation_history`, `tool_start_receives_rewritten_payload_and_post_hook_history`, `tool_start_is_not_called_when_pre_tool_hook_prevents_execution`. `safety_buffering.rs` asserts `SafetyBufferingEvent` from `response.metadata` with and without header gating.

---

## 3. Snapshot / golden testing — **yes, they snapshot the exact request body**

Library: **`insta` 1.46.3** (`codex-rs/Cargo.toml:352`). `codex-rs/core/tests/common/lib.rs` has a `#[ctor]` that sets `INSTA_WORKSPACE_ROOT` to `<repo>/codex-rs` so snapshots resolve identically under Cargo and Bazel.

**Two distinct golden formats, both in `codex-rs/core/tests/common/context_snapshot.rs` (787 lines):**

**(i) Model-context "shape" snapshots** — `format_request_input_snapshot` / `format_labeled_requests_snapshot`, with `ContextSnapshotRenderMode ∈ {RedactedText, FullText, KindOnly, KindWithTextPrefix{max_chars}}` and toggles `strip_capability_instructions()`, `strip_agents_md_user_context()`, `strip_response_item_ids()`. Real content of `core/tests/suite/snapshots/all__suite__compact__mid_turn_compaction_shapes.snap`:

```
Scenario: True mid-turn continuation compaction after tool output: ...

## Local Compaction Request
00:message/developer:<PERMISSIONS_INSTRUCTIONS>
01:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
02:message/user:function call limit push
03:function_call/test_tool
04:function_call_output:unsupported call: test_tool
05:message/user:<SUMMARIZATION_PROMPT>

## Local Post-Compaction History Layout
00:message/developer:<PERMISSIONS_INSTRUCTIONS>
01:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
02:message/user:function call limit push
03:message/user:<COMPACTION_SUMMARY>\nAUTO_SUMMARY
```

**(ii) Full request-body diff snapshots** — `format_request_body_diff_snapshot` (used at `core/tests/suite/compact_remote.rs:1284`). It takes the entire JSON body, strips transport metadata, recursively **sorts every object's keys** (`canonicalize_json_snapshot_value`) so serde insertion order can't churn the snapshot, redacts dynamic values (a `UUID_RE` → `<UUID>`, plus `<SANDBOX>`, `<UNIX_MS>`, temp paths), pretty-prints both, and emits only changed lines via `similar::TextDiff`. Real content of `all__suite__compact_remote__remote_manual_compact_api_auth_prompt_cache_key_request_diff.snap` shows removed `client_metadata` (with the nested `x-codex-turn-metadata` JSON string re-redacted field-by-field), `include: ["reasoning.encrypted_content"]`, `service_tier: "priority"`, `store`, `stream`, `tool_choice`.

`context_snapshot.rs` even has **unit tests for the redaction machinery itself** (`redacted_text_mode_normalizes_turn_metadata_dynamic_json_strings`, `redacted_text_mode_normalizes_system_skill_temp_paths`, `full_text_mode_preserves_unredacted_text`, …).

**(iii) Rendered-UI snapshots** — 691 `.snap` files in `codex-rs/tui/`, rendering a real vt100 screen. Snapshot header from `codex_tui__chatwidget__tests__app_server_mcp_startup_failure_renders_warning_history.snap`:

```
source: tui/src/chatwidget/tests/mcp_startup.rs
expression: normalize_snapshot_paths(term.backend().vt100().screen().contents())
---
⚠ MCP client for `alpha` failed to start: handshake failed
⚠ MCP startup incomplete (failed: alpha)
› Ask Codex to do anything
  gpt-5.6-sol default · /tmp/project
```

`AGENTS.md` makes this a hard rule: *"**Requirement:** any change that affects user-visible UI (including adding new UI) must include corresponding `insta` snapshot coverage."*

Separately, app-server protocol has generated-schema fixture tests (`codex-rs/app-server-protocol/schema/json/v2/*.json` + `schema/typescript/v2/*.ts`, regenerated with `just write-app-server-schema`, verified by `typescript_schema_fixtures_match_generated` / `json_schema_fixtures_match_generated`).

---

## 4. Sandbox / permission testing — **yes, real OS-level denial assertions, `#[cfg(target_os)]`-gated, and they run in CI**

### Linux (bubblewrap + Landlock/seccomp)

`codex-rs/linux-sandbox/tests/suite/landlock.rs` (1,129 lines) opens with `#![cfg(target_os = "linux")]`. It resolves the real helper binary via `env!("CARGO_BIN_EXE_codex-linux-sandbox")` and calls the **production** `codex_core::exec::process_exec_tool_call`. Tests that assert denial:

- `test_root_write` — `#[should_panic]`, `echo blah > <tempfile>` outside writable roots.
- `sandbox_blocks_curl` / `_wget` / `_ping` / `_nc` / `_ssh` / `_getent` / `_dev_tcp_redirection` — via `assert_network_blocked`, which panics with `"Network sandbox FAILED - {cmd:?} exited 0"` if exit code is 0.
- `sandbox_blocks_git_and_codex_writes_inside_writable_root`, `sandbox_blocks_codex_symlink_replacement_attack`, `sandbox_blocks_explicit_split_policy_carveouts_under_bwrap`, `sandbox_blocks_root_read_carveouts_under_bwrap`, `sandbox_keeps_parent_repo_discovery_while_blocking_child_metadata`, `sandbox_reenables_writable_subpaths_under_unreadable_parents`.
- Hardening assertions: `test_no_new_privs_is_enabled`, `sandboxed_command_has_no_effective_or_permitted_capabilities`, `sandbox_inner_stage_rejects_retained_capabilities`, `bwrap_populates_minimal_dev_nodes`, `bwrap_preserves_writable_dev_shm_bind_mount`.
- `test_timeout` — `#[should_panic(expected = "Sandbox(Timeout")]`.

**Runtime capability probe rather than blanket skip:** `should_skip_bwrap_tests()` runs `bash -lc true` under the sandbox and treats `"bubblewrap is unavailable: no system bwrap was found"` or `"Can't mount proc on /newroot/proc" + Operation not permitted/Permission denied/Invalid argument` as "skip"; a probe *timeout* also skips (*"Probe timeouts are not actionable ... skip rather than fail the whole suite"*); anything else `panic!`s. Same idea in `codex-rs/exec/tests/suite/sandbox.rs::linux_sandbox_test_env()` → *"Skipping test: Landlock is not enforceable on this host."*

### macOS (seatbelt)

`codex-rs/exec/tests/suite/seatbelt.rs` (776 lines, 13 tests) is gated at the module level in `codex-rs/exec/tests/suite/mod.rs`:

```rust
mod sandbox;
#[cfg(target_os = "macos")]
mod seatbelt;
```

It spawns real children through `spawn_command_under_sandbox` (which calls production `codex_core::exec::build_exec_request`) and asserts denial for: `seatbelt_deny_globs_block_writes_to_existing_and_new_files`, `seatbelt_blocks_renaming_ancestors_of_explicit_protected_files`, `seatbelt_blocks_renaming_dynamic_glob_ancestors`, `seatbelt_blocks_protected_reads_and_writes_through_symlink_aliases`, `seatbelt_globstar_protects_files_and_ancestors_at_every_depth`, `seatbelt_blocks_atomic_rename_exchange_and_destination_replacement`, `seatbelt_protects_directory_trees_created_after_policy_application`, `seatbelt_enforces_brace_alternation_and_escaped_deny_globs`, plus a negative-control `seatbelt_ancestor_protection_does_not_block_unlinking_regular_files`.

**The self-nesting problem is handled by an env-var skip.** `codex-rs/core/tests/common/lib.rs:537`:

```rust
macro_rules! skip_if_sandbox {
    () => {{
        if ::std::env::var($crate::sandbox_env_var()) == Ok("seatbelt".to_string()) {
            eprintln!("{} is set to 'seatbelt', skipping test.", $crate::sandbox_env_var());
            return;
        }
    }};
    ...
}
```

`AGENTS.md` explains why: *"when you spawn a process using Seatbelt (`/usr/bin/sandbox-exec`), `CODEX_SANDBOX=seatbelt` will be set on the child process. Integration tests that want to run Seatbelt themselves cannot be run under Seatbelt."* Companion macro `skip_if_no_network!` keys off `CODEX_SANDBOX_NETWORK_DISABLED`. Full skip-macro set in `lib.rs`: `skip_if_sandbox!`, `skip_if_no_network!`, `skip_if_test_condition!`, `skip_if_remote!`, `skip_if_no_remote_env!`, `skip_if_wine_exec!`, `skip_if_target_windows!`, `skip_if_host_windows!`, `codex_linux_sandbox_exe_or_skip!`.

### Windows

`codex-rs/core/tests/suite/windows_sandbox.rs` is `#[cfg(target_os = "windows")]` in `mod.rs`. 4 tests, e.g. `windows_restricted_token_rejects_exact_and_glob_deny_read_policy` asserts a *refusal to run at all*:

```rust
assert_eq!(err.to_string(),
  "unsupported operation: windows unelevated restricted-token sandbox cannot enforce deny-read restrictions directly; refusing to run unsandboxed");
```

Plus `windows_elevated_enforces_deny_read_and_protects_setup_marker`, `windows_elevated_unified_exec_enforces_managed_deny_reads`. Serialized via `serial_test::serial(codex_home)` and, in `.config/nextest.toml`, a dedicated `windows_sandbox_legacy_sessions` test-group with `max-threads = 1` (*"These tests create restricted-token Windows child processes and private desktops. Serialize them to avoid exhausting Windows session/global desktop resources in CI."*).

### exec-server FS sandbox

`codex-rs/exec-server/tests/file_system_unix.rs` has `assert_sandbox_denied(&std::io::Error)` accepting `InvalidInput | PermissionDenied` or a message containing `"Permission denied"`, applied to reads outside workspace roots and symlink escapes (`symlink(&outside_dir, allowed_dir.join("link"))`).

### Approval / permission policy — a 27-test scenario table

`codex-rs/core/tests/suite/approvals.rs` is **4,247 lines**, gated `#[cfg(not(target_os = "windows"))]`. A declarative `ScenarioSpec { name, approval_policy, sandbox_policy, action, sandbox_permissions, features, model_override, outcome, expectation }` table is fanned out by `test_case`:

```rust
#[test_case(ScenarioGroup::DangerFullAccess ; "danger_full_access")]
#[test_case(ScenarioGroup::ReadOnly ; "read_only")]
#[test_case(ScenarioGroup::WorkspaceWrite ; "workspace_write")]
#[test_case(ScenarioGroup::ApplyPatch ; "apply_patch")]
#[test_case(ScenarioGroup::UnifiedExec ; "unified_exec")]
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn approval_matrix_covers_group(group: ScenarioGroup) -> Result<()>
```

`ActionKind ∈ {WriteFile, FetchUrl, FetchUrlNoProxy, RunCommand, RunCommandWithPolicy, RunCommandWithPrefixRule, RunUnifiedExecCommand, ApplyPatchFreeform, ApplyPatchShell}`; `Expectation ∈ {FileCreated, FileCreatedNoExitCode, PatchApplied, FileNotCreated, NetworkSuccess, NetworkSuccessNoExitCode, NetworkFailure, CommandSuccess, CommandSuccessNoExitCode, CommandFailure}`.

The key point: `FileNotCreated::verify` asserts nonzero exit, matches the stdout message, **and** `assert!(!path.exists(), "command should not create {path:?}, but file exists")` — i.e. the OS denial is verified end-to-end through a full agent turn. And the expected message for `read_only_never_reports_sandbox_failure` is the raw OS error, branched per platform:

```rust
message_contains: if cfg!(target_os = "linux") {
    &["Permission denied|Read-only file system"]
} else {
    &["Permission denied|Operation not permitted|operation not permitted|Read-only file system"]
},
```

Also: `network_approval.rs`, `request_permissions.rs`, `request_permissions_tool.rs`, `skill_approval.rs`, `cyber_exec_policy.rs`, `exec_policy.rs`, `safety_check_downgrade.rs`, `catalog_permission_messages.rs`, `permissions_messages.rs`, `unified_exec_zsh_fork_approvals.rs`, `extension_sandbox.rs`, `multi_exec_server_sandbox.rs`, `codex-rs/exec/tests/suite/approval_policy.rs`.

### Does it run in CI?

Yes. `.github/workflows/blocking-ci.yml` is the single required gate; it calls `bazel.yml`, which runs **`bazel test //...`** (minus `//third_party/v8:all` and `//codex-rs/v8-poc:v8-poc-unit-tests`, with `--test_tag_filters=-argument-comment-lint`) on:

- `macos-15-xlarge` × `aarch64-apple-darwin`, `x86_64-apple-darwin` → **seatbelt tests run**
- `ubuntu-24.04` × `x86_64-unknown-linux-gnu`, `x86_64-unknown-linux-musl` → **landlock/bwrap tests run**
- Windows: 4 cross-compiled `x86_64-pc-windows-gnullvm` shards on self-hosted runners (PR), plus a native `x86_64-pc-windows-msvc` job on `main` only.
- **Explicitly disabled:** `ubuntu-24.04-arm` (`aarch64-unknown-linux-musl`, `aarch64-unknown-linux-gnu`), commented out with *"2026-02-27 Bazel tests have been flaky on arm in CI. Disable until we can investigate and stabilize them."*

`codex-rs/core/BUILD.bazel` sets `test_tags = ["no-sandbox"]` (Codex's own tests must escape Bazel's sandbox to build their own), `integration_test_timeout = "long"`, `test_shard_counts = {"core-all-test": 16, "core-unit-tests": 8}`, `test_threads = select({macos: 1, default: 0})`, and pulls real helper binaries into runfiles (`codex-linux-sandbox`, `bwrap`, `codex-windows-sandbox-setup`, `codex-command-runner`, `codex`, `test_stdio_server`).

---

## 5. End-to-end / benchmark layer — **there is no agent-quality benchmark in this repo**

Searched the whole tree for `swe-bench` / `SWE-bench` / `terminal-bench` / `exercism` / `pass@`. The only hits are incidental (`pnpm-lock.yaml`, `network-proxy/src/config.rs`, `cli/src/doctor.rs`, `login/src/server.rs`, `tui/src/bottom_pane/app_link_view.rs`, `windows-sandbox-rs/src/setup.rs`) — **none are a benchmark harness**. No `evals/` directory exists.

What "e2e benchmark" means here is **startup latency**, not task success. `codex-rs/BUILD.bazel`:

```python
test_suite(
    name = "e2e-benchmarks",
    tags = ["manual"],
    tests = ["//codex-rs/cli:codex-help-bench"],
)
```

and the entire suite is `codex-rs/cli/e2e_benches/codex_help.rs` — a `divan` bench that runs `codex --help` 20 times. `justfile`: `bench-e2e` (opt build, `--cache_test_results=no`), `bench-e2e-smoke`, `bench` (`cargo bench --workspace --bench '*'`), `bench-smoke` (`just bench -- --test`). Only `bench-smoke` is in CI (`rust-ci.yml`, "Rust benchmark smoke test") and it only proves benchmarks *start*. The `manual` tag keeps `e2e-benchmarks` out of `bazel test //...`.

The closest thing to a real e2e layer is the **remote-executor matrix**, documented in `.codex/skills/remote-tests/SKILL.md`: the same core/app-server integration suites re-run with the exec-server split across an OS boundary — (1) Docker Linux exec-server, driven by `scripts/test-remote-env.sh` and env vars `CODEX_TEST_ENVIRONMENT` / `CODEX_TEST_REMOTE_ENV` / `CODEX_TEST_REMOTE_ENV_CONTAINER_NAME` / `CODEX_TEST_REMOTE_EXEC_SERVER_URL` (wired up in `rust-ci-full-nextest-platform.yml` when `inputs.remote_env` is true, Linux x86_64 only); and (2) **Wine**, a Windows exec-server run under Wine with a Linux host — `bazel test //codex-rs/core:core-all-wine-exec-test`, emitted by `defs.bzl` (`run_tests_with_wine_exec = True` in `core/BUILD.bazel`), constrained to `WINE_TEST_TARGET_COMPATIBLE_WITH = [gnu.2.28, x86_64, linux]` and marked **`flaky = True`** when sharded.

**Nondeterminism handling** is `.config/nextest.toml`, not n-runs/seeds/pass@k:

```toml
[profile.default]
# Retry once so one transient failure does not fail full-CI outright.
slow-timeout = { period = "30s", terminate-after = 2 }
retries = 1
```

plus per-test overrides (`rmcp_client` and `humanlike_typing_1000_chars_...` get `1m`/`terminate-after 4` with the comment *"Do not add new tests here"*) and concurrency test-groups to reduce contention flakes (`app_server_integration` max-threads 1, `core_apply_patch_cli_integration` 1, `windows_process_heavy` 2). Post-merge sharding is `cargo nextest --partition "hash:{shard}/4"` over archive-backed shards, including Windows-ARM64 archives cross-built on Windows x64 and replayed on native ARM64.

---

## 6. What they deliberately do NOT test (documented)

From `AGENTS.md`:

- *"Do not add tests for values that are statically defined."*
- *"Do not add negative tests for logic that was removed."*
- *"Avoid boilerplate tests that only assert experimental field markers for individual request fields in `common.rs`; rely on schema generation/tests and behavioral coverage instead."*
- *"Keep crate API surfaces as small as possible. Avoid proliferating test-only helpers."* / *"Avoid test-only functions in the main implementation."*
- *"Avoid mutating process environment in tests; prefer passing environment-derived flags or dependencies from above."*
- *"For agent changes prefer integration tests over unit tests... Features that change the agent logic MUST add an integration test."*
- *"Do not move or rewrite existing inline `#[cfg(test)] mod tests { ... }` modules solely to follow this convention."*
- Live API: `live_cli.rs` header — `#[ignore]` *"so CI stays deterministic and free."*

From `.github/workflows/README.md` (explicit PR-vs-postmerge split):

> *"`rust-ci.yml` keeps the Cargo-native PR checks intentionally small: cargo fmt --check, cargo shear, argument-comment-lint..."* / *"`rust-ci-full.yml` ... keeps the heavier checks off the PR path while still validating them after merge"* / *"Reserve `rust-ci-full.yml` for heavyweight Cargo-native coverage that Bazel does not replace yet."*

Other documented non-coverage:
- ARM Linux Bazel tests disabled (flakiness, `bazel.yml` comment).
- `codex-rs/core/tests/suite/mod.rs` comment on the arg0-dispatch `#[ctor]`: *"NOTE: this doesn't work on ARM"*.
- `codex-rs/tui/tests/suite/resize_reflow.rs` — 4 tests `#[ignore = "requires tmux and a locally built codex binary; run with --ignored for manual resize smoke"]`.
- `codex-rs/core/tests/suite/shell_snapshot.rs:712` — bare `#[ignore]` with a commented-out `#[cfg_attr(not(target_os = "windows"), ignore)]` above it (no stated reason).
- Two known-incorrect compaction behaviors intentionally left failing-and-ignored (§2c).
- `core/BUILD.bazel` admits a test-data hack: *"some of our integration tests are relying on the presence of this file as a repo root marker... TODO(aibrahim): Update the tests so that `just bazel-remote-test` succeeds without this workaround."*

Of the 26 `#[ignore]`s total, most are **not** disabled tests — 7 are `#[ignore = "child process for ..."]` / `"spawned by ..."` in `rmcp-client`, i.e. re-entrant test binaries used as fixtures.

---

## 7. Coverage shape (what's actually covered)

Concentrated in **`codex-rs/core/tests/suite/`** (~160 modules) — the agent loop, tools, approvals, compaction, MCP, hooks, multi-agent/subagents, guardian review, resume/fork, rollout persistence, model switching, prompt caching, OTel — and **`codex-rs/app-server/tests/suite/v2/`** (~115 files) — the public JSON-RPC surface, per `AGENTS.md`: *"Tests should exercise app-server's public JSON-RPC API."* `codex-rs/tui/` is covered almost entirely by 691 insta screen snapshots rather than integration tests (only 9 files / 1,274 lines in `tui/tests`). Thin spots by file count: `windows-sandbox-rs/tests` is a single 1-test file (`helper_manifest.rs`) — Windows sandbox behavior lives in `core/tests/suite/windows_sandbox.rs` and inline `*_tests.rs` instead.

Test-helper crates worth noting as reusable patterns: `core_test_support` (`core/tests/common/`, 15 files) with `TestCodexBuilder` (~35 `with_*` builders, `build`, `build_with_auto_env`, `build_with_streaming_server`, `build_with_websocket_server`, `resume`, `restart`) and `TestCodexHarness` (`request_bodies()`, `function_call_output_value()`, `apply_patch_output()`, workspace FS helpers); and `app-server/tests/common/` with `TestAppServer`, `mock_model_server.rs`, `local_websocket_exec_server.rs`, `analytics_server.rs`, `rpc_delay.rs`.

---

## Unverified / could not confirm

- **`gh` CLI is not installed** on this machine; `api.github.com` rate-limited me out partway through, so file listings after that point come from the `--depth 1` clone (which is authoritative) rather than the API.
- I did not run any of these tests. Statements about which tests *pass* are not made — only about what the source asserts and how CI is wired.
- I did not read `codex-rs/core/tests/suite/approvals.rs` in full (4,247 lines); the `ScenarioSpec` count is `grep -c 'async fn '` = 27 top-level test/helper fns plus a table whose exact scenario count I did not tally. The `test_case` fan-out is 5 groups.
- `.github/workflows/rust-ci-full.yml` (574 lines) and `rust-ci-full-nextest-platform.yml` (439 lines) were grepped, not read line-by-line; the target/profile matrix summary is from those greps.
- Whether the Wine and Docker remote-executor lanes actually execute on every `main` push (vs. being skipped for missing runners/inputs) — I confirmed the wiring (`inputs.remote_env: true` for `x86_64-unknown-linux-gnu` in `rust-ci-full.yml:481`; `WINE_TEST_TARGET_COMPATIBLE_WITH` gating) but not observed run history.
- I did not check `codex-rs/otel/tests`, `codex-rs/rmcp-client/tests`, `codex-rs/code-mode-host/tests`, or `codex-rs/cli/tests` in any depth.
- `scripts/test-remote-env.sh` is referenced by the skill doc; I confirmed the skill text but did not open the script.