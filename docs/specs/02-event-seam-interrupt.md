# 事件缝 / 流式 / 正确中断 / steering

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Engine/UI decoupling: an event sink, token streaming, correct interrupt, and mid-run steering`


## 一句话

Replace the 30 `print()` calls buried in `Agent.run()` with a single `await self._emit(event)` against a pluggable async `EventSink`, which turns the engine into a headless state machine and thereby unlocks token streaming (deltas are just events), a real interrupt (`task.cancel()` + synthesize-don't-delete history repair), mid-run steering (typed lines queued and injected at the next step boundary), a silent eval mode, and deletion of the drifted duplicate loop in `acp/__init__.py:127-165`.

## 为什么这是难点

Every coding agent that people actually use is a long-running, interactive, partially-observable process. The user watches tokens arrive, changes their mind halfway, hits Esc, types "no, use pytest not unittest" while a `bash` call is running. None of that is possible if the loop's only output channel is `print()` to a TTY — and `Agent.run()` currently has 30 of them (`mini_agent/agent.py`, lines 94, 180, 183, 190, 231-233, 286, 290, 311, 321, 334-336, 352, 355, 381-382, 386-387, 393, 400, 410, 413, 424, 463, 465, 480, 485, 491).

The coupling is not cosmetic; it is load-bearing, and you can measure the damage in this repo. Because the loop can only talk to a terminal, ACP had to fork it: `acp/__init__.py:127-165` is a second, drifted copy that has silently lost logging (no `agent.logger` calls) and lost context compaction (never calls `_summarize_messages`), so ACP sessions blow the context window while the CLI does not. Because there is no request/response channel to a UI, there can be no permission prompt before a destructive tool call. Because the LLM client only exposes `generate() -> LLMResponse` (`llm/anthropic_client.py:257`), time-to-first-token equals full-response latency. And because interrupt is a polled boolean rather than real cancellation, Esc is ignored for up to 120 s during a `bash` call.

Decoupling is the cheap structural move that makes all four of those tractable at once. It is the "I understand where the seam goes" module.

## 仓库现状

**1. The engine is the renderer.** `mini_agent/agent.py:15` imports `Colors, calculate_display_width` — a *terminal* concern imported by the core loop. `run()` (`agent.py:294-492`) draws a 58-column box (`agent.py:329-336`), truncates tool arguments to 200 chars (`agent.py:416-421`) and tool results to 300 chars (`agent.py:461-462`), and emits ANSI escapes. The truncation is the tell: it is a rendering decision baked into the engine, so *no* consumer — ACP, an eval harness, a log — can ever see the full tool output.

**2. ACP forked the loop.** `mini_agent/acp/__init__.py:127-165` (`_run_turn`) reimplements the step loop against `acp` session notifications. It has drifted: no `logger.log_request` / `log_response` / `log_tool_result`, and no `await agent._summarize_messages()` — so ACP sessions never compact. It also passes `tool_schemas` (dicts) at `acp/__init__.py:132-134` where the CLI passes `Tool` objects.

**3. No streaming.** `AnthropicClient._make_api_request` (`llm/anthropic_client.py:48-81`) calls `await self.client.messages.create(**params)` with `max_tokens: 16384` (`anthropic_client.py:69`) and blocks. `anthropic` 0.72.1 is installed (`.venv/.../anthropic-0.72.1.dist-info`), which has `client.messages.stream()`. `LLMClientBase` (`llm/base.py:40-55`) declares only `generate()`.

**4. The interrupt is broken in three distinct ways.**
- *It never cancels anything.* `cli.py:775-781` creates the task and then polls `while not agent_task.done(): ... await asyncio.sleep(0.1)`, setting `cancel_event` but never calling `agent_task.cancel()`. The `except asyncio.CancelledError` at `cli.py:786` is dead code. `Agent._check_cancelled()` (`agent.py:63-71`) is only read at three step boundaries (`agent.py:318, 397, 477`), so Esc during a 120 s foreground `bash` (`tools/bash_tool.py:399`) or a slow LLM call does nothing until that call returns.
- *The history "repair" destroys completed work.* `_cleanup_incomplete_messages()` (`agent.py:73-94`) finds the **last assistant message** and does `self.messages = self.messages[:last_assistant_idx]`. At the call site `agent.py:477-478` — reached *after* a tool result has already been appended at `agent.py:474` — that assistant message is a **completed** step. So: step 5 issues two tool calls, both succeed, both results are in history, user hits Esc → the assistant message and both tool results are deleted. The file the agent wrote still exists on disk; the history now claims it doesn't. At `agent.py:318` (top of the next step) it is worse: nothing is orphaned there at all, and it deletes a fully-complete turn for no reason.
- *It never checks the actual invariant.* The thing that must hold is "every `tool_use` id in an assistant message has a matching `tool_result` before the next assistant message" — the constraint `anthropic_client.py:147-176` serializes into the wire format. `_cleanup_incomplete_messages` never inspects `tool_calls` ids at all.

**5. Keystrokes during a run are eaten.** The Esc thread (`cli.py:725-767`) puts the tty in cbreak (`cli.py:754`) and reads one byte at a time; `cli.py:756-763` discards every byte that is not `\x1b`. Text typed during a run is consumed from stdin and thrown away — it does not steer, and it does not even reappear at the next prompt.

**6. No orphan-process cleanup.** `bash_tool.py:398-409` only kills the child on `asyncio.TimeoutError`. `bash_tool.py:431`'s `except Exception` does not catch `CancelledError` (it is a `BaseException`), so cancellation correctly propagates — and correctly leaves a live `sleep 300` with open pipes.

**7. All 9 external `run()` call sites take no arguments** (`cli.py:587`, `cli.py:775`, `tests/test_agent.py:66,147`, `tests/test_integration.py:107,194`, `examples/02_simple_agent.py:90,182`, `examples/04_full_agent.py:145,245`) and use the return value as a `str`. This is decisive for the design choice below.

## 最小实现

## 0. The design choice, decided first

**(a) `run()` becomes `AsyncIterator[AgentEvent]`.** Reject. Three reasons, in order of severity:
1. *It makes the hardest mechanism the hardest to write.* Interrupt requires history repair in a `finally`/`except CancelledError`. In an async generator, when the consumer stops pulling (breaks, or its task is cancelled), finalization happens via `athrow(GeneratorExit)` at a GC-determined moment, and `await`-ing inside that teardown raises `RuntimeError: async generator ignored GeneratorExit` unless every consumer wraps in `contextlib.aclosing()`. Making history repair depend on consumer discipline is exactly backwards.
2. *A yield-only stream cannot do request/response.* Permission prompts need "renderer, ask the user; engine, block until you answer." A generator can only push. You would need a second channel anyway — at which point the second channel is the real abstraction.
3. *It breaks all 9 call sites* (`cli.py:587`, `cli.py:775`, 4 tests, 4 examples), each of which does `result = await agent.run()`.

**(b) `on_event` callback + `ConsoleRenderer`. ← RECOMMENDED.** `run()` keeps its `-> str` contract, so all 9 call sites compile unchanged. The engine keeps control flow, so `try/except CancelledError/finally` is ordinary code. The sink may be `async`, so it can `await` a permission answer. `on_event=None` is silent mode for free.

**(c) Hybrid.** Adopt as a 12-line freebie, not as the primitive: `QueueSink` is an `EventSink` that `put_nowait`s into an `asyncio.Queue`, plus an `async def events()` that drains it. Anyone who wants pull-style iteration gets it; nobody who wants correct teardown pays for it. The callback is the primitive; the iterator is derived. Say exactly that in the interview.

What (b) unlocks, concretely: permission prompt = `PermissionRequest` carrying an `asyncio.Future` the sink resolves; streaming = `TextDelta`; eval harness = `on_event=None` or `JsonlSink`; ACP = an `AcpSink`, deleting `acp/__init__.py:127-165` outright.

---

## 1. New file: `mini_agent/events.py` (~130 LOC)

```python
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional, Union

@dataclass(frozen=True)
class RunStarted:      run_id: str; log_path: str; max_steps: int
@dataclass(frozen=True)
class StepStarted:     step: int; max_steps: int                    # 1-based
@dataclass(frozen=True)
class StepFinished:    step: int; elapsed_s: float; total_elapsed_s: float

@dataclass(frozen=True)
class TextDelta:
    text: str
    channel: Literal["answer", "thinking"] = "answer"

@dataclass(frozen=True)
class MessageComplete:
    content: str
    thinking: Optional[str]
    tool_call_names: tuple[str, ...]
    usage_total_tokens: Optional[int]
    streamed: bool          # True => TextDeltas already covered content/thinking

@dataclass(frozen=True)
class ToolCallStarted:  call_id: str; name: str; arguments: dict[str, Any]
@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str; name: str; success: bool
    content: str            # FULL content. Truncation is the renderer's job.
    error: Optional[str]; elapsed_s: float

@dataclass(frozen=True)
class CompactionStarted:  estimated_tokens: int; api_tokens: int; limit: int
@dataclass(frozen=True)
class CompactionFinished: tokens_before: int; tokens_after: int; rounds_summarized: int

@dataclass(frozen=True)
class SteeringInjected:   text: str; at_step: int
@dataclass(frozen=True)
class Interrupted:
    reason: Literal["user", "external"]
    repaired_messages: int      # synthesized tool_results; never a delete count
    partial_text: str

@dataclass(frozen=True)
class AgentError:  message: str; fatal: bool; retry_attempt: Optional[int] = None
@dataclass(frozen=True)
class RunFinished:
    result: str
    reason: Literal["end_turn", "max_steps", "cancelled", "error"]
    steps: int; total_elapsed_s: float

@dataclass
class PermissionRequest:        # NOT frozen: carries a Future
    call_id: str; tool_name: str; arguments: dict[str, Any]
    future: "asyncio.Future[bool]"

AgentEvent = Union[RunStarted, StepStarted, StepFinished, TextDelta, MessageComplete,
                   ToolCallStarted, ToolCallFinished, CompactionStarted, CompactionFinished,
                   SteeringInjected, Interrupted, AgentError, RunFinished, PermissionRequest]

EventSink = Callable[[AgentEvent], Union[None, Awaitable[None]]]
```

Note `PermissionRequest` is specified but **not wired** in this pass (see scope cut); it exists to prove the callback shape can carry request/response, which an iterator cannot.

## 2. `Agent` changes (`mini_agent/agent.py`)

`__init__` (`agent.py:21-29`): add `on_event: EventSink | None = None`. In the body add:
```python
self.on_event = on_event
self._steering: asyncio.Queue[str] = asyncio.Queue()
self._interrupt_requested = False
self._run_task: asyncio.Task | None = None
self._partial_text = ""
```
Delete `self.cancel_event` (`agent.py:36`). Delete the import at `agent.py:15` entirely — after this change the engine imports nothing from `.utils`. That single deleted import is the one-line proof of decoupling; show it in the diff.

```python
async def _emit(self, ev: AgentEvent) -> None:
    if self.on_event is None:
        return
    try:
        r = self.on_event(ev)
        if inspect.isawaitable(r):
            await r
    except asyncio.CancelledError:
        raise                       # never swallow cancellation
    except Exception as e:          # a broken renderer must not kill the run
        logging.getLogger(__name__).warning("event sink failed on %s: %s", type(ev).__name__, e)
```

**Delete `_check_cancelled()` (`agent.py:63-71`) and `_cleanup_incomplete_messages()` (`agent.py:73-94`)** and their three call sites (`agent.py:318-322`, `397-401`, `476-481`). Confirmed no other references (`grep -rn _cleanup_incomplete_messages mini_agent tests examples` → only those four lines).

Add:
```python
def interrupt(self) -> None:
    """Thread-unsafe; call from the event loop (use loop.call_soon_threadsafe)."""
    if self._interrupt_requested:
        return                       # second Esc must not re-cancel mid-repair
    self._interrupt_requested = True
    if self._run_task is not None and not self._run_task.done():
        self._run_task.cancel()

def steer_nowait(self, text: str) -> None:
    self._steering.put_nowait(text)

def _drain_steering(self) -> str:
    parts = []
    while True:
        try: parts.append(self._steering.get_nowait())
        except asyncio.QueueEmpty: break
    return "\n".join(parts)

def _repair_history(self) -> int:
    """Restore: every tool_call id in the last assistant message has a tool result.
    Synthesizes; never deletes. Pure sync — must not await. Returns count added."""
    idx = next((i for i in range(len(self.messages) - 1, -1, -1)
                if self.messages[i].role == "assistant"), None)
    if idx is None or not self.messages[idx].tool_calls:
        return 0
    satisfied = {m.tool_call_id for m in self.messages[idx + 1:]
                 if m.role == "tool" and m.tool_call_id}
    added = 0
    for tc in self.messages[idx].tool_calls:
        if tc.id not in satisfied:
            self.messages.append(Message(
                role="tool", content="[interrupted by user before this tool completed]",
                tool_call_id=tc.id, name=tc.function.name))
            added += 1
    return added
```

`run()` becomes a thin wrapper (replacing `agent.py:294-312`'s preamble):
```python
async def run(self, on_event: EventSink | None = None) -> str:
    if on_event is not None:
        self.on_event = on_event
    self._interrupt_requested = False
    self._partial_text = ""
    self._run_task = asyncio.current_task()
    try:
        return await self._run_loop()
    except asyncio.CancelledError:
        n = self._repair_history()          # SYNC, before any await
        try:
            await self._emit(Interrupted(
                reason="user" if self._interrupt_requested else "external",
                repaired_messages=n, partial_text=self._partial_text))
            await self._emit(RunFinished(result="Task cancelled by user.",
                                         reason="cancelled", steps=self._step,
                                         total_elapsed_s=perf_counter() - self._t0))
        except asyncio.CancelledError:
            pass                            # a redelivered cancel must not skip repair
        if not self._interrupt_requested:
            raise                           # not our cancel: propagate
        return "Task cancelled by user."
    finally:
        self._run_task = None
```

`_run_loop()` is the body of today's `agent.py:313-492` with these edits:
- `agent.py:311` → `await self._emit(RunStarted(run_id=..., log_path=str(self.logger.get_log_file_path()), max_steps=self.max_steps))`
- top of loop, **before** `await self._summarize_messages()` at `agent.py:326`, insert the steering drain (see §5).
- `agent.py:329-336` (box) → `await self._emit(StepStarted(step=step+1, max_steps=self.max_steps))`
- `agent.py:345` → `response = await self._generate(tool_list)` (§4)
- `agent.py:352 / 355` → `await self._emit(AgentError(message=error_msg, fatal=True))` then `RunFinished(reason="error")`
- `agent.py:380-387` → `await self._emit(MessageComplete(content=response.content, thinking=response.thinking, tool_call_names=tuple(tc.function.name for tc in (response.tool_calls or [])), usage_total_tokens=(response.usage.total_tokens if response.usage else None), streamed=self._streamed_this_step))`
- `agent.py:390-394` (the "done" branch) → §5's late-steering check, then `StepFinished` + `RunFinished(reason="end_turn")`
- `agent.py:410-424` → `await self._emit(ToolCallStarted(call_id=tool_call_id, name=function_name, arguments=arguments))`. **Delete the 200-char truncation at `agent.py:416-421`** — it moves to the renderer.
- `agent.py:459-465` → `await self._emit(ToolCallFinished(call_id=..., name=..., success=result.success, content=result.content, error=result.error, elapsed_s=...))`. **Delete the 300-char truncation at `agent.py:461-462`** — moves to the renderer.
- `agent.py:483-485` → `StepFinished`
- `agent.py:490-491` → `RunFinished(reason="max_steps")`
- `_summarize_messages` (`agent.py:180/183` → one `CompactionStarted`; `190` → `AgentError(fatal=False)`; `231-233` → one `CompactionFinished`; `286` → delete; `290` → `AgentError(fatal=False)`).

All 30 print sites accounted for; `agent.py` ends with zero `print(`.

## 3. New: `mini_agent/render/console.py` (~170 LOC, mostly moved)

```python
class ConsoleRenderer:
    def __init__(self, stream=sys.stdout, arg_limit=200, result_limit=300): ...
    async def __call__(self, ev: AgentEvent) -> None:
        handler = getattr(self, "_on_" + type(ev).__name__, None)
        if handler: await handler(ev) if inspect.iscoroutinefunction(handler) else handler(ev)
```
`_on_StepStarted` holds the `BOX_WIDTH = 58` / `calculate_display_width` block moved verbatim from `agent.py:329-336`. `_on_ToolCallStarted` holds the 200-char truncation from `agent.py:416-421`; `_on_ToolCallFinished` holds the 300-char one. `_on_TextDelta` writes raw + `flush()` and tracks whether a header has been printed for the current channel; `_on_MessageComplete` prints content **only if `not ev.streamed`**, else just a trailing newline. Goal: byte-identical stdout to today for the non-streaming path (see the parity test).

Also `mini_agent/render/jsonl.py` (~25 LOC): `dataclasses.asdict(ev)` + `type(ev).__name__` + `perf_counter()` per line — this is the demo artifact and the eval harness.

## 4. Streaming (`llm/base.py`, `llm/anthropic_client.py`, `llm/llm_wrapper.py`)

Add to `LLMClientBase` (after `llm/base.py:55`) a **non-abstract** default so `OpenAIClient` needs no change:
```python
async def stream_generate(self, messages, tools=None,
                          on_delta: Callable[[str, str], Awaitable[None]] | None = None) -> LLMResponse:
    return await self.generate(messages, tools)   # non-streaming fallback
```
Signature choice matters: it returns the **same `LLMResponse`**, so `agent.py:358-377` (usage accumulation, logging, `Message` construction) is untouched. Deltas leave by callback. The alternative — returning an async iterator of chunks — would force the agent loop to reassemble partial tool-call JSON, and the agent must never know that partial JSON exists.

`AnthropicClient` gains (`anthropic_client.py`, after line 81):
```python
async def _make_api_request_stream(self, system_message, api_messages, tools, on_delta):
    params = {"model": self.model, "max_tokens": 16384, "messages": api_messages}
    if system_message: params["system"] = system_message
    if tools: params["tools"] = self._convert_tools(tools)
    async with self.client.messages.stream(**params) as stream:
        async for event in stream:
            if event.type != "content_block_delta" or on_delta is None:
                continue
            d = event.delta
            if d.type == "text_delta":
                await on_delta("answer", d.text)
            elif d.type == "thinking_delta":
                await on_delta("thinking", d.thinking)
            # input_json_delta: ignored on purpose, the SDK accumulates it
        return await stream.get_final_message()
```
and `stream_generate` mirroring `generate` (`anthropic_client.py:257-293`) but calling `_make_api_request_stream` and reusing `_parse_response` unchanged. `LLMClient` (`llm/llm_wrapper.py:113-127`) gets a 3-line `stream_generate` passthrough.

Use `messages.stream()` (the helper), not `messages.create(stream=True)`: the helper accumulates `input_json_delta` fragments and `get_final_message()` returns `tool_use.input` already parsed to a dict, which is exactly what `_parse_response` (`anthropic_client.py:221-232`) expects.

**Endpoint dependency.** Relies on C4 (streaming SSE via `messages.stream()` works at all) and C5 (`tool_use` arguments arrive as `input_json_delta` fragments) — both still untested on this endpoint, see [`../PROVIDER_CAPABILITIES.md`](../PROVIDER_CAPABILITIES.md). If unsupported: `stream_generate` falls back to the non-streaming default on `LLMClientBase` above, which returns the whole `LLMResponse` and emits zero deltas; `MessageComplete.streamed` stays `False` so `ConsoleRenderer` prints the body exactly as today, no renderer change and no agent-loop change — and the time-to-first-token claim is reported as "not supported by this endpoint" rather than estimated.

Agent side:
```python
async def _generate(self, tool_list):
    self._streamed_this_step = False
    async def on_delta(channel, text):
        self._streamed_this_step = True
        if channel == "answer": self._partial_text += text
        await self._emit(TextDelta(text=text, channel=channel))
    return await self.llm.stream_generate(self.messages, tool_list, on_delta=on_delta)
```

**Retry × streaming.** The retry decorator (`anthropic_client.py:275-283`) re-invokes the request on failure. If the first attempt already emitted deltas, a retry silently duplicates half an answer on screen. Rule for this pass: count deltas in the closure; if `delta_count > 0` when the exception fires, do not retry — surface `AgentError(fatal=True)`. Implement by giving `stream_generate` its own `retryable` guard rather than reusing `async_retry` blindly.

## 5. Steering

**Producer** — replace `cli.py:756-763` (the discard loop). Capture `loop = asyncio.get_running_loop()` before `esc_thread.start()` (`cli.py:770-771`):
```python
buf = []
while not esc_listener_stop.is_set():
    rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not rlist: continue
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        loop.call_soon_threadsafe(agent.interrupt); break
    if ch in ("\r", "\n"):
        line = "".join(buf).strip(); buf.clear()
        if line: loop.call_soon_threadsafe(agent.steer_nowait, line)
    elif ch == "\x7f":
        if buf: buf.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
    elif ch >= " ":
        buf.append(ch); sys.stdout.write(ch); sys.stdout.flush()
```
`call_soon_threadsafe` is mandatory: `asyncio.Queue` is not thread-safe, and `put_nowait` from a foreign thread can leave a waiter unwoken. Also clear `ECHO` explicitly via `termios` rather than relying on `tty.setcbreak`, whose ECHO behaviour differs across Python versions — otherwise you get doubled characters from the manual echo above.

**Consumer** — insert at the top of `_run_loop`'s `while`, immediately before `await self._summarize_messages()` (today `agent.py:326`):
```python
steer = self._drain_steering()
if steer:
    self.messages.append(Message(role="user", content=f"[User steering, mid-task]\n{steer}"))
    await self._emit(SteeringInjected(text=steer, at_step=step + 1))
```
Before compaction, not after, so the compactor prices its tokens and its "keep every user message" rule (`agent.py:186`) preserves it.

**Late steering** — before the `return response.content` at `agent.py:394`, drain once more; if non-empty, append it as a user message, emit `SteeringInjected`, `step += 1`, and `continue` instead of returning. Otherwise a line typed during the final turn is stranded in the queue and would surface at the start of the *next*, unrelated `run()`.

**Wire-format merge** (`anthropic_client.py:158-176`): when the previous `api_messages` entry is a `user` message whose content is a list of `tool_result` blocks, append the steering text as a `{"type": "text", ...}` block to *that* message rather than emitting a second consecutive user turn. Order matters: `tool_result` blocks must come first. Depends on C9 (does this endpoint accept two consecutive `user` messages — untested); if it does, the merge is an *optimization* (one fewer turn, the redirect sits next to the results it is redirecting from), not a requirement, and the fallback is to emit the second user message unmerged.

## 6. Interrupt driver (`cli.py`)

Replace `cli.py:717-791`:
```python
loop = asyncio.get_running_loop()
esc_thread = threading.Thread(target=esc_key_listener, args=(loop,), daemon=True); esc_thread.start()
try:
    await agent.run()                 # run() sets self._run_task = current_task()
except asyncio.CancelledError:
    pass                              # only reachable for a non-agent cancel
finally:
    esc_listener_stop.set(); esc_thread.join(timeout=0.2)
```
Delete `cancel_event` at `cli.py:718-719, 737, 762, 780, 789` and the 100 ms polling loop at `cli.py:777-781`. `agent.run()` is awaited directly; `interrupt()` cancels the *current* task, which is this one.

Also `tools/bash_tool.py:398-409`, add one clause so Esc does not orphan a child:
```python
except asyncio.CancelledError:
    process.kill(); raise
```

## 7. Renderer wiring
- `cli.py:569-575`: add `on_event=ConsoleRenderer()`.
- `acp/__init__.py:102`: add `on_event=AcpSink(self._conn, session_id)`.
- **Delete `acp/__init__.py:127-165`** (`_run_turn`) and change `prompt()` (`acp/__init__.py:119`) to `stop = _STOP[await state.agent.run()]`-style mapping off the `RunFinished.reason` the sink captured: `end_turn→"end_turn"`, `max_steps→"max_turn_requests"`, `cancelled→"cancelled"`, `error→"refusal"`. `AcpSink` maps `TextDelta/MessageComplete→update_agent_message`, thinking→`update_agent_thought`, `ToolCallStarted→start_tool_call`, `ToolCallFinished→update_tool_call`. `cancel()` (`acp/__init__.py:122-125`) becomes `state.agent.interrupt()`.
- `tests/*` and `examples/*` keep working unchanged (all 9 `run()` sites verified argument-free) but go silent; add `on_event=ConsoleRenderer()` to their `Agent(` constructors (`examples/02_simple_agent.py:66,158`, `examples/04_full_agent.py:103,221`) if you want the old chatter.

## 边界情况

1. **Interrupt after a completed tool, before the next LLM call.** Obvious-but-wrong: what `agent.py:477-478` does today — call `_cleanup_incomplete_messages()`, which truncates to before the last assistant message, deleting a *completed* assistant turn and its already-present tool results. The file the tool wrote still exists on disk; the history now says it never happened, so the next turn redoes the work. Right: `_repair_history()` inspects the last assistant message's `tool_calls` ids, finds all of them satisfied, and returns 0 — history untouched. The invariant is 'no orphaned tool_use', not 'nothing incomplete'. Synthesize, never delete.

2. **Interrupt with 1 of 3 tool calls done.** Obvious-but-wrong: drop the assistant message and its one tool result, so the API can't see an orphaned `tool_use`. Right: append two synthetic `tool` messages with `content="[interrupted by user before this tool completed]"` for the two unsatisfied ids. Two reasons the synthesis wins: (1) the model now *sees* it was interrupted, so on the next user turn it can resume rather than blindly re-running the first tool; (2) tool 1's real side effect (a written file) stays represented, so history and the filesystem don't diverge. Truncation makes the interrupt invisible to the model — that divergence is the actual failure mode. Depends on C6 (does one assistant message here actually carry multiple `tool_use` blocks — untested); if this endpoint never parallelizes, the rule stands unchanged (a tool group is atomic) and the property test simply **constructs** a parallel group instead of waiting for a real response to produce one.

3. **Interrupt mid-LLM-stream.** Obvious-but-wrong: append the partially accumulated assistant text so nothing is lost. That produces an assistant message with no `tool_calls` and, if zero deltas arrived, empty content — an empty text block violates the protocol constraint (pending C7/C8 verification; even if this endpoint is more permissive, the invariant is kept anyway — a permissive endpoint only makes the bug surface later). Right: append nothing. `self.messages.append(assistant_msg)` at `agent.py:377` happens *after* the await, so a mid-stream cancel leaves history clean by construction; the partial text goes into `Interrupted.partial_text` for the renderer only. The correctness of this depends on a line-ordering accident in the existing code — pin it with a test so a future refactor can't move the append above the await.

4. **Cancellation delivered twice while the repair runs.** Obvious-but-wrong: do the repair inside the `except asyncio.CancelledError` handler after an `await self._emit(...)`. If the driver polls and calls `task.cancel()` again (as `cli.py:777-781` would), that `await` re-raises `CancelledError` and the repair never runs — leaving exactly the orphaned `tool_use` you were trying to prevent. Right: `_repair_history()` is pure-sync and runs as the *first* statement of the handler; every subsequent `await` is wrapped in its own `try/except CancelledError: pass`. Related: `interrupt()` is idempotent via `self._interrupt_requested`, so a second Esc can't clobber an in-flight repair.

5. **Swallowing `CancelledError` unconditionally.** Obvious-but-wrong: `except asyncio.CancelledError: return "cancelled"`. If the cancel came from an outer `asyncio.timeout()` or a TaskGroup tearing down, swallowing it makes the task complete normally and the outer shutdown hangs or misbehaves. Right: repair and emit either way, but `raise` unless `self._interrupt_requested` is set — i.e. only swallow *our own* deliberate interrupt. This is the difference between a cancel-aware component and one that quietly breaks its parent's structured concurrency.

6. **Steering that arrives between tool 2 and tool 3 of the same assistant turn.** Obvious-but-wrong: drain the queue inside the tool-execution loop (around `agent.py:404-474`) so the user's redirect lands as soon as possible. That inserts a `user` message between an assistant's `tool_use` blocks and its remaining `tool_result`s — the exact orphaned-`tool_use` violation the interrupt path exists to prevent, now caused by a feature (protocol constraint, pending C6/C7 verification; a more tolerant endpoint would only delay the bug, so the rule holds regardless). Right: drain **only** at the top of the step (before `agent.py:326`), where every id is satisfied. Latency cost is at most one tool call; correctness is absolute.

7. **Steering typed during the model's final turn.** If the model returns no `tool_calls`, `run()` returns at `agent.py:394` and the queued line is stranded — then injected at the start of the next, unrelated `run()`, where it reads as a non-sequitur. Right: drain once more before that return; if non-empty, append it, emit `SteeringInjected`, and `continue` the loop instead of returning. `max_steps` still bounds it.

8. **Streaming plus retry.** Obvious-but-wrong: wrap the streaming call in the existing `async_retry` decorator (`anthropic_client.py:275-283`) exactly like `generate` does. On a mid-stream failure the retry replays from scratch and the user watches half an answer, then a *different* answer, concatenated with no boundary. Right: track `delta_count` in the closure; retry only while it is 0, otherwise surface `AgentError(fatal=True)`. Streams are not idempotent from the renderer's point of view.

9. **Double-rendering thinking and text.** Obvious-but-wrong: emit `TextDelta`s during the stream *and* let `_on_MessageComplete` print the accumulated `content`/`thinking` (a straight port of `agent.py:380-387`). Everything appears twice. Right: `MessageComplete.streamed` is set from `self._streamed_this_step`, and the renderer prints the body only when it is `False` — which also keeps the OpenAI client's non-streaming fallback (`llm/base.py` default `stream_generate`) rendering correctly with zero client changes.

10. **Truncation left in the engine.** `agent.py:416-421` (200 chars) and `agent.py:461-462` (300 chars) look like harmless formatting, but if `ToolCallFinished.content` carries the truncated string, the ACP client, the JSONL eval log, and any future permission UI can never see the real tool output — and you will not notice until you're debugging a truncated stack trace. Events carry full fidelity; every limit lives in the renderer's `__init__`.

11. **Thinking-block signature is dropped.** `anthropic_client.py:140` serializes `{"type": "thinking", "thinking": msg.thinking}` with no `signature`, because `Message.thinking` (`schema/schema.py:34`) is a bare `str` with nowhere to put one. Whether that costs anything here is C10 (can a thinking block be replayed verbatim, or is a `signature` required — untested), and streaming is what makes the answer visible, since you now either see `signature_delta` arrive on the wire or see that it never does. If C10 says a signature is required, replaying a thinking block without one fails on the next turn and the fix is one field: `thinking_signature: str | None` on `Message`, populated in `_parse_response` and echoed in `_convert_messages`. If C10 says thinking blocks round-trip fine without it, the field is not added and this pitfall is recorded as not applying to this endpoint; the documented fallback is to stop replaying thinking altogether and log the cost as a PITFALL.

## 怎么证明它有效

**All of it runs offline in under 10 seconds. No live API, no SWE-bench.**

Add `tests/fakes.py` (~60 LOC): a `ScriptedLLM` with the `LLMClient` surface, driven by a list of canned `LLMResponse`s. Its `stream_generate` chunks `content` into 5-char `on_delta("answer", ...)` calls, and accepts `cancel_after_deltas: int | None` / `cancel_during_tool: str | None` hooks that raise `asyncio.CancelledError` at a chosen point. Plus one invariant checker, which is the real deliverable:

```python
def assert_history_valid(messages) -> None:
    """Every tool_call id in an assistant message has a matching tool result
    before the next assistant message. This is the Anthropic wire constraint."""
    pending: dict[str, str] = {}
    for m in messages:
        if m.role == "assistant":
            assert not pending, f"orphaned tool_use: {sorted(pending)}"
            pending = {tc.id: tc.function.name for tc in (m.tool_calls or [])}
        elif m.role == "tool":
            pending.pop(m.tool_call_id, None)
    assert not pending, f"orphaned tool_use at end: {sorted(pending)}"
```

Then `tests/test_events.py`, four tests, run with `pytest tests/test_events.py -q`:

1. `test_renderer_parity` — run a fixed 3-step script through `Agent(on_event=ConsoleRenderer())` with `stream_generate` falling back to non-streaming; `capsys` the stdout and compare against `tests/golden/console_run.txt`, generated once by running the same script on the **pre-refactor** commit (`git stash` the change, run, `git stash pop`). Proves the decoupling is behaviour-preserving, not a rewrite.
2. `test_silent_mode` — same script, `on_event=None`, assert `capsys.readouterr().out == ""`. One line; permanently kills the class of bug where a `print` sneaks back into the engine.
3. `test_interrupt_leaves_no_orphan` — script: assistant emits two tool calls; the first tool succeeds, the second blocks; cancel during it. Assert three things: `assert_history_valid(agent.messages)` passes; `len(agent.messages)` **increased** by exactly 1 (the synthetic result) rather than decreasing; and the surviving tool-1 result content is still present. As a contrast assertion, snapshot `len(messages)` before, call the deleted-in-this-PR `_cleanup_incomplete_messages` logic inline on a copy, and assert it *drops* 3 messages — the diff between the two numbers is the whole argument.
4. `test_steering_injects_at_boundary` — call `agent.steer_nowait("use pytest not unittest")` from a fake tool's body (i.e. mid-turn, between tool 1 and tool 2). Assert: no `user` message exists anywhere between the assistant message and its second `tool` result; exactly one `user` message contains the steering text; its index is greater than the last tool-result index and less than the next assistant index. Plus `assert_history_valid`.

**Artifacts to show.** (a) `pytest tests/test_events.py -q` → `4 passed`. (b) `python -m mini_agent.cli --task "…" ` with `JsonlSink` attached → `events.jsonl`; `jq -r '.type' events.jsonl | uniq -c` prints the event tape, which *is* the architecture diagram. (c) One live number, one API call, ~30 s: time-to-first-token, computed from the jsonl as `first TextDelta.ts − RunStarted.ts` versus the non-streaming `MessageComplete.ts − RunStarted.ts`. Report both, e.g. "0.4 s vs 11.2 s". (d) Optional 20-second terminal recording: `mini-agent --task "print the numbers 1..30, one per second, using bash"`, type `actually just print 1..5` and hit Enter mid-run to show steering land at the next step, then Esc, then `pgrep -f 'sleep'` returning nothing to show the child was killed rather than orphaned.

## 深度追问

1. **"Why a callback instead of `async for event in agent.run()`? Iterators are more Pythonic."** Three specifics. (1) *Teardown.* Interrupt requires history repair on cancellation. In an async generator, when the consumer stops pulling, finalization arrives as `GeneratorExit` at a GC-chosen moment, and `await`-ing during teardown raises `RuntimeError: async generator ignored GeneratorExit` unless every consumer uses `contextlib.aclosing()`. That makes the single most correctness-critical mechanism depend on consumer discipline. With a callback the engine owns control flow and repair is an ordinary `except asyncio.CancelledError` block. (2) *Direction.* A generator only pushes. Permission prompts are request/response — engine blocks, UI answers. A callback can be `async` and can carry an `asyncio.Future` (that's why `PermissionRequest` is the one non-frozen dataclass). An iterator needs a second channel anyway, and once you have it, the callback was the primitive all along. (3) *Blast radius.* All 9 `run()` call sites (`cli.py:587`, `cli.py:775`, four tests, four examples) do `result = await agent.run()`. The callback keeps `-> str` and changes zero of them. I still ship the iterator — as a 12-line `QueueSink` derived from the callback, not the other way round.

2. **"Walk me through exactly what's wrong with the current cancellation."** Three separate bugs that happen to share a name. First, nothing is ever cancelled: `cli.py:775-781` polls `agent_task.done()` and sets a boolean, never calling `.cancel()`, so the `except asyncio.CancelledError` at `cli.py:786` is dead code and Esc is ignored for up to the 120 s `bash` timeout (`bash_tool.py:399`). Second, `_cleanup_incomplete_messages` (`agent.py:73-94`) truncates to before the *last assistant message*, and at the `agent.py:477` call site that message is a **completed** step whose tool results were appended at `agent.py:474` — so pressing Esc deletes finished work while its side effects remain on disk. At `agent.py:318` it deletes a complete turn where nothing was orphaned to begin with. Third, it never checks the invariant it exists to protect: it never looks at `tool_calls` ids. The fix is a different shape of operation — *append* synthetic tool results for unsatisfied ids, never delete — because the API constraint is 'no orphaned `tool_use`', which appending can always satisfy and truncating can only satisfy by destroying context.

3. **"Why synthesize a fake tool result instead of rewinding to a clean state?"** Rewinding is locally simpler and globally wrong for two reasons. The model loses the fact that it was interrupted, so the next turn it re-runs the same tool — the user hits Esc on a slow `npm install` and the agent's first move is to run it again. And the interrupted turn's *completed* side effects are real: a file was written, a branch was created. Truncating makes history claim otherwise, and every subsequent decision is made against a world model that contradicts the filesystem. Synthesizing `[interrupted by user before this tool completed]` keeps the causal record intact and is O(1)-safe: appending can never create a new orphan, whereas every truncation has to prove it didn't cut in the middle of a tool group. Rejected alternative: writing the partial assistant text into history — it produces an assistant message with no tool_calls and possibly empty content, which the wire-format constraint rejects (pending C8 verification — and an endpoint that tolerates it would only hide the mistake for longer), and it teaches the model to imitate truncated outputs.

4. **"Where exactly do you inject steering text, and what breaks if you inject it anywhere else?"** Only at the top of the step loop, immediately before `_summarize_messages()` (today `agent.py:326`). Injecting inside the tool-execution loop (`agent.py:404-474`) puts a `user` message between an assistant's `tool_use` blocks and its remaining `tool_result`s — the same orphaned-`tool_use` violation the interrupt path exists to prevent, now self-inflicted (protocol constraint, pending C7 verification; the invariant is held regardless of how strict the endpoint turns out to be). Two further ordering facts. It goes *before* compaction, not after, because `_summarize_messages` keeps every `user` message (`agent.py:186`) and prices its tokens. And on the wire, rather than emitting a second consecutive user turn, I merge it as a trailing `{"type":"text"}` block into the same user message that carries the `tool_result` blocks (`anthropic_client.py:158-176`) — with the `tool_result`s first, since a leading text block is rejected. That merge rides on C9 (untested): if this endpoint accepts consecutive `user` messages, merging is an optimization rather than a necessity, and the unmerged form is the fallback. Finally, the final-turn drain before `agent.py:394`: without it, a line typed during the model's last turn is stranded and resurfaces at the start of an unrelated later run.

5. **"How do you get a tool-call dict out of a token stream?"** With `anthropic` 0.72.1 I use `client.messages.stream()` (the helper), not `messages.create(stream=True)`, and take `await stream.get_final_message()` — the helper accumulates `input_json_delta` fragments and hands back `tool_use.input` already parsed, which is exactly what `_parse_response` (`anthropic_client.py:221-232`) consumes, so nothing downstream changes. If you use the raw stream you own the reassembly: buffer `partial_json` strings per `content_block_index`, `json.loads` at `content_block_stop`, and — the case people miss — a tool call with an empty input emits *zero* `input_json_delta` events, so the naive `json.loads("".join(parts))` raises `JSONDecodeError`; you must default to `{}`. Architecturally the point is that this knowledge lives in the client, never in the agent loop: `stream_generate` returns the same `LLMResponse` as `generate` and deltas leave by a side callback, so the loop never learns that partial JSON exists. All of it is conditional on C4/C5 holding here (untested); if they don't, the base-class non-streaming fallback returns the identical `LLMResponse` and the architecture argument survives untouched, just unexercised.

6. **"What's the interaction between streaming and your retry layer?"** They are incompatible unless you constrain them. The existing decorator (`anthropic_client.py:275-283`) re-invokes the request on failure, which is fine when the only observable is the return value. Once deltas have been emitted, the renderer has already shown bytes to a human, and a replay concatenates two different answers with no boundary. So `stream_generate` counts deltas in its closure and only retries while the count is 0; after first token, a failure becomes `AgentError(fatal=True)`. The general principle: retry is only safe for operations whose observable effects are confined to their return value, and streaming deliberately breaks that. The production answer is a `StreamRestarted` event plus renderer-side rollback of the current block — I deliberately didn't build that.

7. **"Your key-reader lives in a thread. What are the concurrency hazards?"** Two. First, `asyncio.Queue` is not thread-safe: `put_nowait` from the reader thread can race the loop's waiter bookkeeping and drop a wakeup, so every hand-off crosses via `loop.call_soon_threadsafe(agent.steer_nowait, line)`, with the loop captured before `esc_thread.start()`. Same for `agent.interrupt()`, which calls `Task.cancel()` — cancelling a task from a foreign thread is undefined. Second, terminal state: the thread puts the tty in cbreak (`cli.py:754`) and restores it in a `finally` (`cli.py:765`), but `tty.setcbreak`'s handling of the `ECHO` flag differs across Python versions, so I clear `ECHO` explicitly via `termios` and echo typed characters myself — otherwise you get either invisible or doubled input. And today's loop at `cli.py:756-763` doesn't just ignore non-Esc bytes, it *consumes* them from stdin, so they never reach the next prompt either.

8. **"What did decoupling actually buy you — show me, don't tell me."** Four concrete things. (1) `acp/__init__.py:127-165` gets deleted: a 39-line forked copy of the loop that had already drifted — no `logger.log_*` calls and no `_summarize_messages()`, so ACP sessions never compact and blow the context window while the CLI doesn't. One loop, two sinks, that class of drift is structurally impossible. (2) `on_event=None` is a silent eval mode, asserted by a one-line test. (3) `mini_agent/agent.py:15` — `from .utils import Colors, calculate_display_width` — is deleted; the engine imports nothing terminal-related, which is the one-line proof in the diff. (4) The 200-char and 300-char truncations at `agent.py:416-421` and `agent.py:461-462` move to the renderer, so events carry full-fidelity tool output and the ACP client and JSONL log finally see the real thing.

## 前置条件

1. `mini_agent/agent.py:73-94` — `_cleanup_incomplete_messages()` must be **deleted**, not patched, along with its three call sites (`agent.py:318-319`, `397-398`, `476-478`) and `_check_cancelled()` (`agent.py:63-71`). Verified there are no other references anywhere in `mini_agent/`, `tests/`, or `examples/`. Any interrupt work built on top of it inherits its data loss.

2. `mini_agent/cli.py:717-791` — the `cancel_event` plumbing (`cli.py:718-719, 737, 762, 780, 789`) and the 100 ms `while not agent_task.done()` polling loop (`cli.py:777-781`) must be removed before wiring `interrupt()`. Leaving the poller in place means a second `.cancel()` can be redelivered while `_repair_history` is running.

3. `mini_agent/tools/bash_tool.py:398-409` — add `except asyncio.CancelledError: process.kill(); raise`. Not optional: `wait_for` only kills the child on timeout, so today a correct interrupt would leave a live subprocess with open pipes, and 'my Esc works' is not a demonstrable claim while `pgrep sleep` still finds it.

4. CONDITIONAL, only if C10 ([`../PROVIDER_CAPABILITIES.md`](../PROVIDER_CAPABILITIES.md), untested) comes back saying this endpoint requires a `signature` on a replayed thinking block: `mini_agent/schema/schema.py:34` needs `thinking_signature: str | None`, populated in `anthropic_client.py:216-220` and echoed at `anthropic_client.py:140`. The current serialization drops the signature (`anthropic_client.py:140`), which is a fact about our code regardless of the answer. Until C10 is probed this stays unknown — do not add the field on speculation, and if C10 comes back negative the documented fallback is to stop replaying thinking blocks and record the cost.

## 明确不做

Not building: (1) `PermissionRequest` is defined and documented but **not wired** to any tool — no allow/deny gating, no rule persistence, no `--dangerously-skip-permissions`. It exists purely to prove the sink shape can carry request/response, which is the argument against the iterator design. (2) No renderer-side rollback on a mid-stream retry — I forbid retry after first token instead of implementing `StreamRestarted` + block rewind. (3) No Live/TUI renderer, no spinner, no re-flowing output; `ConsoleRenderer` is deliberately byte-identical to today's output so the refactor is provable. (4) `OpenAIClient` gets the non-streaming `stream_generate` fallback from `llm/base.py`, not a real SSE implementation — one provider streams, and the fallback proves the seam holds for one that doesn't. (5) No Windows path for steering; the `msvcrt` branch (`cli.py:729-742`) keeps Esc-only behaviour. (6) No backpressure or event buffering — a slow sink blocks the loop by design.

To an interviewer: "I built the seam and one full vertical slice through it — streaming, interrupt, and steering all ride the same event sink, and ACP's forked loop is deleted because it rides it too. Permissions are the fourth consumer and I stopped at the dataclass on purpose: adding a second permission UI would have taught me nothing new about the mechanism, whereas making interrupt correct — synthesize tool results instead of deleting completed turns, repair before any await, re-raise cancellations that aren't mine — is where all the actual difficulty was."

## 代码量

≈750 lines touched: ~450 new (`events.py` ~130, `render/console.py` ~170 of which ~60 is moved verbatim from `agent.py:329-336/416-421/461-462`, `render/jsonl.py` ~25, `tests/fakes.py` ~60, `tests/test_events.py` ~140), ~120 modified in `agent.py` (+120/−90, net −30, ending at zero `print(`), ~115 added across `llm/base.py` (+12), `llm/anthropic_client.py` (+55), `llm/llm_wrapper.py` (+5), `cli.py` (+70/−60), `bash_tool.py` (+4), and ~39 deleted outright (`acp/__init__.py:127-165`, replaced by a ~45-line `AcpSink`).

## 工期

4-5 focused days. Day 1: `events.py` + `ConsoleRenderer` + migrate all 30 print sites, with the golden-output parity test proving byte-identical stdout. Day 2: interrupt — delete `_cleanup_incomplete_messages`, write `_repair_history` + `assert_history_valid`, rewire `cli.py`, patch `bash_tool.py`, land tests 3 and 4. Day 3: streaming — `stream_generate` on base/anthropic/wrapper, the `streamed` double-render guard, the no-retry-after-first-token rule. Day 4: steering — line-buffered reader thread, `call_soon_threadsafe` hand-off, boundary injection, the tool_result merge in `_convert_messages`, final-turn drain. Day 5: delete `acp/__init__.py:127-165`, write `AcpSink` + `JsonlSink`, capture the time-to-first-token number and the terminal recording.
