# 事件缝 / 流式 / 正确中断 / steering

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Engine/UI decoupling: an event sink, token streaming, correct interrupt, and mid-run steering`


## 一句话

把埋在 `Agent.run()` 里的 30 个 `print()` 调用换成对一个可插拔的异步 `EventSink` 的单一 `await self._emit(event)`，从而把引擎变成一台无头状态机，并由此解锁：token 流式输出（delta 就是事件）、真正的中断（`task.cancel()` + 合成而非删除的历史修复）、运行中 steering（用户输入的行排队，在下一个步骤边界注入）、静默 eval 模式，以及删掉 `acp/__init__.py:127-165` 里那份已经漂移的重复循环。

## 为什么这是难点

每一个真的有人用的 coding agent，都是一个长时间运行、可交互、只能部分观测的进程。用户盯着 token 一个个到达，中途改主意，按 Esc，在一个 `bash` 调用正在跑的时候敲下"不对，用 pytest 别用 unittest"。如果循环唯一的输出通道是往 TTY `print()`，这些一个都做不到——而 `Agent.run()` 现在正好有 30 个（`mini_agent/agent.py`，行 94、180、183、190、231-233、286、290、311、321、334-336、352、355、381-382、386-387、393、400、410、413、424、463、465、480、485、491）。

这种耦合不是表面装饰，它是承重的，而且损害在这个仓库里可以量出来。因为循环只能跟终端说话，ACP 只好把它 fork 一份：`acp/__init__.py:127-165` 是第二份已经漂移的拷贝，它悄悄丢了日志（没有 `agent.logger` 调用），也丢了上下文压缩（从不调 `_summarize_messages`），于是 ACP 会话会撑爆上下文窗口，而 CLI 不会。因为没有通往 UI 的请求/响应通道，破坏性工具调用之前也就不可能有权限提示。因为 LLM 客户端只暴露 `generate() -> LLMResponse`（`llm/anthropic_client.py:257`），首 token 时间等于整个响应的延迟。又因为中断是一个轮询的布尔量而不是真正的取消，在一次 `bash` 调用期间按 Esc 最长会被忽略 120 秒。

解耦是那个廉价的结构性动作，它一次性让上面四件事全部变得可做。这是"我知道缝该切在哪"的那个模块。

## 仓库现状

**1. 引擎就是渲染器。** `mini_agent/agent.py:15` 导入了 `Colors, calculate_display_width`——一个*终端*层面的关注点被核心循环导入。`run()`（`agent.py:294-492`）画一个 58 列宽的框（`agent.py:329-336`），把工具参数截断到 200 字符（`agent.py:416-421`）、工具结果截断到 300 字符（`agent.py:461-462`），并输出 ANSI 转义序列。截断就是那个破绽：它是一个被烤进引擎里的渲染决策，于是*任何*消费者——ACP、eval harness、日志——永远都看不到完整的工具输出。

**2. ACP fork 了这个循环。** `mini_agent/acp/__init__.py:127-165`（`_run_turn`）针对 `acp` 会话通知重新实现了一遍步骤循环。它已经漂移了：没有 `logger.log_request` / `log_response` / `log_tool_result`，也没有 `await agent._summarize_messages()`——所以 ACP 会话从不压缩。它还在 `acp/__init__.py:132-134` 传的是 `tool_schemas`（dict），而 CLI 传的是 `Tool` 对象。

**3. 没有流式。** `AnthropicClient._make_api_request`（`llm/anthropic_client.py:48-81`）以 `max_tokens: 16384`（`anthropic_client.py:69`）调用 `await self.client.messages.create(**params)` 并阻塞。已安装 `anthropic` 0.72.1（`.venv/.../anthropic-0.72.1.dist-info`），它是有 `client.messages.stream()` 的。`LLMClientBase`（`llm/base.py:40-55`）只声明了 `generate()`。

**4. 中断以三种彼此独立的方式坏掉。**
- *它从不取消任何东西。* `cli.py:775-781` 创建 task 之后就开始轮询 `while not agent_task.done(): ... await asyncio.sleep(0.1)`，只设置 `cancel_event`，从不调用 `agent_task.cancel()`。`cli.py:786` 处的 `except asyncio.CancelledError` 是死代码。`Agent._check_cancelled()`（`agent.py:63-71`）只在三个步骤边界被读取（`agent.py:318, 397, 477`），所以在一次 120 秒的前台 `bash`（`tools/bash_tool.py:399`）或一次慢速 LLM 调用期间按 Esc，在那个调用返回之前什么都不会发生。
- *历史"修复"会毁掉已完成的工作。* `_cleanup_incomplete_messages()`（`agent.py:73-94`）找到**最后一条 assistant 消息**然后执行 `self.messages = self.messages[:last_assistant_idx]`。在调用点 `agent.py:477-478`——这里是在 `agent.py:474` 已经追加了一个 tool result *之后*才到达的——那条 assistant 消息是一个**已完成**的步骤。于是：第 5 步发出两个工具调用，两个都成功，两个结果都已在历史里，用户按 Esc → 那条 assistant 消息和两个 tool result 都被删掉。agent 写下的那个文件还在磁盘上；历史现在声称它不存在。在 `agent.py:318`（下一步的开头）更糟：那里根本没有任何东西是孤立的，它却无缘无故删掉了一个完整的轮次。
- *它从不检查真正的不变量。* 必须成立的是"assistant 消息里的每个 `tool_use` id，在下一条 assistant 消息之前都有一个匹配的 `tool_result`"——也就是 `anthropic_client.py:147-176` 序列化进 wire format 的那条约束。`_cleanup_incomplete_messages` 压根没看过 `tool_calls` 的 id。

**5. 运行期间的按键被吃掉。** Esc 线程（`cli.py:725-767`）把 tty 置为 cbreak（`cli.py:754`）并逐字节读；`cli.py:756-763` 丢弃每一个不是 `\x1b` 的字节。运行期间输入的文本从 stdin 被消费掉然后扔了——它不会 steering，甚至不会在下一个提示符处重新出现。

**6. 没有孤儿进程清理。** `bash_tool.py:398-409` 只在 `asyncio.TimeoutError` 时杀掉子进程。`bash_tool.py:431` 的 `except Exception` 捕获不到 `CancelledError`（它是 `BaseException`），所以取消能正确传播——同时也正确地留下一个还活着、管道还开着的 `sleep 300`。

**7. 全部 9 个外部 `run()` 调用点都不传参数**（`cli.py:587`、`cli.py:775`、`tests/test_agent.py:66,147`、`tests/test_integration.py:107,194`、`examples/02_simple_agent.py:90,182`、`examples/04_full_agent.py:145,245`），并把返回值当 `str` 用。这一条对下面的设计选择是决定性的。

## 最小实现

## 0. 先定下设计选择

**(a) `run()` 变成 `AsyncIterator[AgentEvent]`。** 否决。三个理由，按严重程度排：
1. *它让最难的机制变得最难写。* 中断要求在 `finally`/`except CancelledError` 里做历史修复。在异步生成器里，当消费者停止拉取（break，或者它的 task 被取消）时，收尾是在一个由 GC 决定的时刻通过 `athrow(GeneratorExit)` 发生的，而在那个 teardown 里 `await` 会抛出 `RuntimeError: async generator ignored GeneratorExit`，除非每个消费者都用 `contextlib.aclosing()` 包起来。让历史修复依赖消费者的自觉，正好是反的。
2. *只能 yield 的流做不了请求/响应。* 权限提示需要的是"渲染器，去问用户；引擎，阻塞到你回答为止"。生成器只能推。你无论如何都得再要一条通道——而到那时，第二条通道才是真正的抽象。
3. *它会打断全部 9 个调用点*（`cli.py:587`、`cli.py:775`、4 个测试、4 个示例），每一处都写着 `result = await agent.run()`。

**(b) `on_event` 回调 + `ConsoleRenderer`。← 推荐。** `run()` 保住它的 `-> str` 契约，所以 9 个调用点一个都不用改。引擎保住控制流，所以 `try/except CancelledError/finally` 就是普通代码。sink 可以是 `async` 的，于是它能 `await` 一个权限回答。`on_event=None` 白送一个静默模式。

**(c) 混合。** 当成 12 行的赠品接受，而不是当成原语：`QueueSink` 是一个把事件 `put_nowait` 进 `asyncio.Queue` 的 `EventSink`，外加一个把它排干的 `async def events()`。想要 pull 式迭代的人拿得到；想要正确 teardown 的人不用为它买单。回调是原语，迭代器是派生物。面试时就这么原话说出来。

(b) 具体解锁了什么：权限提示 = 一个携带 `asyncio.Future`、由 sink 来解决的 `PermissionRequest`；流式 = `TextDelta`；eval harness = `on_event=None` 或 `JsonlSink`；ACP = 一个 `AcpSink`，直接删掉 `acp/__init__.py:127-165`。

---

## 1. 新文件：`mini_agent/events.py`（约 130 LOC）

```python
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional, Union

@dataclass(frozen=True)
class RunStarted:      run_id: str; log_path: str; max_steps: int
@dataclass(frozen=True)
class StepStarted:     step: int; max_steps: int                    # 从 1 开始
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
    streamed: bool          # True => TextDelta 已经覆盖了 content/thinking

@dataclass(frozen=True)
class ToolCallStarted:  call_id: str; name: str; arguments: dict[str, Any]
@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str; name: str; success: bool
    content: str            # 完整内容。截断是渲染器的活。
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
    repaired_messages: int      # 合成出来的 tool_result 数；永远不是删除计数
    partial_text: str

@dataclass(frozen=True)
class AgentError:  message: str; fatal: bool; retry_attempt: Optional[int] = None
@dataclass(frozen=True)
class RunFinished:
    result: str
    reason: Literal["end_turn", "max_steps", "cancelled", "error"]
    steps: int; total_elapsed_s: float

@dataclass
class PermissionRequest:        # 不是 frozen：它携带一个 Future
    call_id: str; tool_name: str; arguments: dict[str, Any]
    future: "asyncio.Future[bool]"

AgentEvent = Union[RunStarted, StepStarted, StepFinished, TextDelta, MessageComplete,
                   ToolCallStarted, ToolCallFinished, CompactionStarted, CompactionFinished,
                   SteeringInjected, Interrupted, AgentError, RunFinished, PermissionRequest]

EventSink = Callable[[AgentEvent], Union[None, Awaitable[None]]]
```

注意 `PermissionRequest` 只是被规定下来，本轮**并不接线**（见范围裁剪）；它存在的意义是证明回调这个形状能承载请求/响应，而迭代器不能。

## 2. `Agent` 的改动（`mini_agent/agent.py`）

`__init__`（`agent.py:21-29`）：加上 `on_event: EventSink | None = None`。函数体里加：
```python
self.on_event = on_event
self._steering: asyncio.Queue[str] = asyncio.Queue()
self._interrupt_requested = False
self._run_task: asyncio.Task | None = None
self._partial_text = ""
```
删掉 `self.cancel_event`（`agent.py:36`）。把 `agent.py:15` 的 import 整行删掉——改完之后引擎不再从 `.utils` 导入任何东西。那一行被删掉的 import 就是解耦的一行式证据；在 diff 里把它亮出来。

```python
async def _emit(self, ev: AgentEvent) -> None:
    if self.on_event is None:
        return
    try:
        r = self.on_event(ev)
        if inspect.isawaitable(r):
            await r
    except asyncio.CancelledError:
        raise                       # 永远不要吞掉取消
    except Exception as e:          # 坏掉的渲染器不该弄死这次运行
        logging.getLogger(__name__).warning("event sink failed on %s: %s", type(ev).__name__, e)
```

**删掉 `_check_cancelled()`（`agent.py:63-71`）和 `_cleanup_incomplete_messages()`（`agent.py:73-94`）**，以及它们的三个调用点（`agent.py:318-322`、`397-401`、`476-481`）。已确认没有其他引用（`grep -rn _cleanup_incomplete_messages mini_agent tests examples` → 只有那四行）。

新增：
```python
def interrupt(self) -> None:
    """Thread-unsafe; call from the event loop (use loop.call_soon_threadsafe)."""
    if self._interrupt_requested:
        return                       # 第二次 Esc 不能在修复过程中再次取消
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

`run()` 变成一层薄包装（替换 `agent.py:294-312` 的前导部分）：
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
        n = self._repair_history()          # 同步，在任何 await 之前
        try:
            await self._emit(Interrupted(
                reason="user" if self._interrupt_requested else "external",
                repaired_messages=n, partial_text=self._partial_text))
            await self._emit(RunFinished(result="Task cancelled by user.",
                                         reason="cancelled", steps=self._step,
                                         total_elapsed_s=perf_counter() - self._t0))
        except asyncio.CancelledError:
            pass                            # 重新投递的取消不能跳过修复
        if not self._interrupt_requested:
            raise                           # 不是我们发的取消：向上传播
        return "Task cancelled by user."
    finally:
        self._run_task = None
```

`_run_loop()` 就是今天 `agent.py:313-492` 的主体，做这些改动：
- `agent.py:311` → `await self._emit(RunStarted(run_id=..., log_path=str(self.logger.get_log_file_path()), max_steps=self.max_steps))`
- 循环顶部，在 `agent.py:326` 的 `await self._summarize_messages()` **之前**，插入 steering 的排空（见 §5）。
- `agent.py:329-336`（画框）→ `await self._emit(StepStarted(step=step+1, max_steps=self.max_steps))`
- `agent.py:345` → `response = await self._generate(tool_list)`（§4）
- `agent.py:352 / 355` → `await self._emit(AgentError(message=error_msg, fatal=True))` 然后 `RunFinished(reason="error")`
- `agent.py:380-387` → `await self._emit(MessageComplete(content=response.content, thinking=response.thinking, tool_call_names=tuple(tc.function.name for tc in (response.tool_calls or [])), usage_total_tokens=(response.usage.total_tokens if response.usage else None), streamed=self._streamed_this_step))`
- `agent.py:390-394`（"done" 分支）→ §5 的末轮 steering 检查，然后 `StepFinished` + `RunFinished(reason="end_turn")`
- `agent.py:410-424` → `await self._emit(ToolCallStarted(call_id=tool_call_id, name=function_name, arguments=arguments))`。**删掉 `agent.py:416-421` 的 200 字符截断**——它搬去渲染器。
- `agent.py:459-465` → `await self._emit(ToolCallFinished(call_id=..., name=..., success=result.success, content=result.content, error=result.error, elapsed_s=...))`。**删掉 `agent.py:461-462` 的 300 字符截断**——搬去渲染器。
- `agent.py:483-485` → `StepFinished`
- `agent.py:490-491` → `RunFinished(reason="max_steps")`
- `_summarize_messages`（`agent.py:180/183` → 一个 `CompactionStarted`；`190` → `AgentError(fatal=False)`；`231-233` → 一个 `CompactionFinished`；`286` → 删除；`290` → `AgentError(fatal=False)`）。

30 个 print 点全部有着落；`agent.py` 最终 `print(` 数为零。

## 3. 新增：`mini_agent/render/console.py`（约 170 LOC，大部分是搬过来的）

```python
class ConsoleRenderer:
    def __init__(self, stream=sys.stdout, arg_limit=200, result_limit=300): ...
    async def __call__(self, ev: AgentEvent) -> None:
        handler = getattr(self, "_on_" + type(ev).__name__, None)
        if handler: await handler(ev) if inspect.iscoroutinefunction(handler) else handler(ev)
```
`_on_StepStarted` 里放从 `agent.py:329-336` 逐字搬来的 `BOX_WIDTH = 58` / `calculate_display_width` 那段。`_on_ToolCallStarted` 里放来自 `agent.py:416-421` 的 200 字符截断；`_on_ToolCallFinished` 里放那个 300 字符的。`_on_TextDelta` 直接写原始文本 + `flush()`，并跟踪当前 channel 的标题有没有打印过；`_on_MessageComplete` **只在 `not ev.streamed` 时**打印内容，否则只输出一个换行。目标：非流式路径下 stdout 与今天逐字节一致（见 parity 测试）。

另加 `mini_agent/render/jsonl.py`（约 25 LOC）：每行 `dataclasses.asdict(ev)` + `type(ev).__name__` + `perf_counter()`——这既是演示工件，也是 eval harness。

## 4. 流式（`llm/base.py`、`llm/anthropic_client.py`、`llm/llm_wrapper.py`）

在 `LLMClientBase`（`llm/base.py:55` 之后）加一个**非抽象**的默认实现，这样 `OpenAIClient` 一行都不用改：
```python
async def stream_generate(self, messages, tools=None,
                          on_delta: Callable[[str, str], Awaitable[None]] | None = None) -> LLMResponse:
    return await self.generate(messages, tools)   # 非流式回退
```
签名的选择很重要：它返回**同一个 `LLMResponse`**，所以 `agent.py:358-377`（usage 累加、日志、`Message` 构造）完全不动。delta 走回调离开。另一种方案——返回一个 chunk 的异步迭代器——会逼着 agent 循环去重组不完整的 tool-call JSON，而 agent 永远不该知道存在不完整的 JSON。

`AnthropicClient` 新增（`anthropic_client.py`，第 81 行之后）：
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
            # input_json_delta: 故意忽略，SDK 会自己累积
        return await stream.get_final_message()
```
以及一个照抄 `generate`（`anthropic_client.py:257-293`）的 `stream_generate`，只是改调 `_make_api_request_stream`，并原封不动复用 `_parse_response`。`LLMClient`（`llm/llm_wrapper.py:113-127`）加一个 3 行的 `stream_generate` 透传。

用 `messages.stream()`（那个 helper），不要用 `messages.create(stream=True)`：helper 会累积 `input_json_delta` 片段，而 `get_final_message()` 返回的 `tool_use.input` 已经解析成 dict，这正是 `_parse_response`（`anthropic_client.py:221-232`）期待的东西。

**端点依赖。** 依赖 C4（`messages.stream()` 这条流式 SSE 通路本端点到底能不能用）与 C5（`tool_use` 参数是否以 `input_json_delta` 分片下发）——两项都还是「待测」，见 [`../PROVIDER_CAPABILITIES.md`](../PROVIDER_CAPABILITIES.md)。若不支持：`stream_generate` 落回上面 `LLMClientBase` 那个非流式默认实现，整段返回 `LLMResponse`、一个 delta 都不发；`MessageComplete.streamed` 保持 `False`，于是 `ConsoleRenderer` 打正文的方式和今天一模一样——渲染层不用改，agent 循环也不用改——而首 token 时间那个指标写成「本端点不支持」，不估算。

agent 这一侧：
```python
async def _generate(self, tool_list):
    self._streamed_this_step = False
    async def on_delta(channel, text):
        self._streamed_this_step = True
        if channel == "answer": self._partial_text += text
        await self._emit(TextDelta(text=text, channel=channel))
    return await self.llm.stream_generate(self.messages, tool_list, on_delta=on_delta)
```

**重试 × 流式。** 重试装饰器（`anthropic_client.py:275-283`）在失败时重新发起请求。如果第一次尝试已经吐出过 delta，重试就会在屏幕上悄悄把半个回答重复一遍。本轮的规则：在闭包里计数 delta；异常触发时如果 `delta_count > 0`，就不重试——抛出 `AgentError(fatal=True)`。实现上给 `stream_generate` 自己的 `retryable` 守卫，而不是盲目复用 `async_retry`。

## 5. Steering

**生产者**——替换 `cli.py:756-763`（那个丢弃循环）。在 `esc_thread.start()`（`cli.py:770-771`）之前捕获 `loop = asyncio.get_running_loop()`：
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
`call_soon_threadsafe` 是强制的：`asyncio.Queue` 不是线程安全的，从外部线程 `put_nowait` 可能留下一个没被唤醒的 waiter。另外要用 `termios` 显式清掉 `ECHO`，而不是指望 `tty.setcbreak`——它对 ECHO 的行为在各 Python 版本之间不一样；否则上面那段手动回显会让你看到重复的字符。

**消费者**——插在 `_run_loop` 的 `while` 顶部，紧挨着 `await self._summarize_messages()` 之前（今天的 `agent.py:326`）：
```python
steer = self._drain_steering()
if steer:
    self.messages.append(Message(role="user", content=f"[User steering, mid-task]\n{steer}"))
    await self._emit(SteeringInjected(text=steer, at_step=step + 1))
```
放在压缩之前而不是之后，这样压缩器才会把它的 token 计入价格，它那条"保留每一条 user 消息"的规则（`agent.py:186`）也才能把它保住。

**末轮 steering**——在 `agent.py:394` 的 `return response.content` 之前再排空一次；如果非空，就把它作为 user 消息追加、发出 `SteeringInjected`、`step += 1`、`continue` 而不是返回。否则在最后一轮期间输入的一行会滞留在队列里，然后在*下一次*毫不相关的 `run()` 开头冒出来。

**Wire format 合并**（`anthropic_client.py:158-176`）：当上一条 `api_messages` 条目是一个内容为 `tool_result` 块列表的 `user` 消息时，把 steering 文本作为一个 `{"type": "text", ...}` 块追加到*那条*消息上，而不是发出连续第二个 user 轮次。顺序有讲究：`tool_result` 块必须排在前面。依赖 C9（本端点是否接受连续两条 `user` 消息，待测）；如果接受，这个合并就只是**优化**（少一个轮次，重定向紧挨着它要重定向的那批结果），不是必需，降级路径就是不合并、直接发第二条 user 消息。

## 6. 中断驱动方（`cli.py`）

替换 `cli.py:717-791`：
```python
loop = asyncio.get_running_loop()
esc_thread = threading.Thread(target=esc_key_listener, args=(loop,), daemon=True); esc_thread.start()
try:
    await agent.run()                 # run() 会设置 self._run_task = current_task()
except asyncio.CancelledError:
    pass                              # 只有非 agent 发起的取消才会到这里
finally:
    esc_listener_stop.set(); esc_thread.join(timeout=0.2)
```
删掉 `cli.py:718-719, 737, 762, 780, 789` 的 `cancel_event`，以及 `cli.py:777-781` 的 100 毫秒轮询循环。`agent.run()` 被直接 await；`interrupt()` 取消的是*当前*这个 task，也就是它。

另外 `tools/bash_tool.py:398-409` 加一个分支，让 Esc 不会遗留孤儿子进程：
```python
except asyncio.CancelledError:
    process.kill(); raise
```

## 7. 渲染器接线
- `cli.py:569-575`：加 `on_event=ConsoleRenderer()`。
- `acp/__init__.py:102`：加 `on_event=AcpSink(self._conn, session_id)`。
- **删掉 `acp/__init__.py:127-165`**（`_run_turn`），并把 `prompt()`（`acp/__init__.py:119`）改成 `stop = _STOP[await state.agent.run()]` 这类写法，按 sink 捕获到的 `RunFinished.reason` 做映射：`end_turn→"end_turn"`、`max_steps→"max_turn_requests"`、`cancelled→"cancelled"`、`error→"refusal"`。`AcpSink` 把 `TextDelta/MessageComplete→update_agent_message`，thinking→`update_agent_thought`，`ToolCallStarted→start_tool_call`，`ToolCallFinished→update_tool_call`。`cancel()`（`acp/__init__.py:122-125`）变成 `state.agent.interrupt()`。
- `tests/*` 和 `examples/*` 无需改动照常工作（9 个 `run()` 调用点已验证均不带参数），但会变静默；如果你想要原来那些输出，就给它们的 `Agent(` 构造函数加上 `on_event=ConsoleRenderer()`（`examples/02_simple_agent.py:66,158`、`examples/04_full_agent.py:103,221`）。

## 边界情况

1. **在一个工具已完成、下一次 LLM 调用之前中断。** 直觉做法（错的）：就是今天 `agent.py:477-478` 干的事——调 `_cleanup_incomplete_messages()`，它截断到最后一条 assistant 消息之前，删掉一个*已完成*的 assistant 轮次和它已经在场的 tool result。工具写下的文件还在磁盘上；历史却说它从没发生过，于是下一轮又把活重做一遍。正确做法：`_repair_history()` 检查最后一条 assistant 消息的 `tool_calls` id，发现它们全部已被满足，返回 0——历史不动。不变量是"没有孤立的 tool_use"，不是"没有未完成的东西"。合成，绝不删除。

2. **3 个工具调用完成了 1 个时中断。** 直觉做法（错的）：丢掉那条 assistant 消息和它那一个 tool result，好让 API 看不到孤立的 `tool_use`。正确做法：为两个未被满足的 id 追加两条合成的 `tool` 消息，`content="[interrupted by user before this tool completed]"`。合成胜出有两个理由：(1) 模型现在*看得见*自己被中断了，所以下一个用户轮次它能续上，而不是傻乎乎重跑第一个工具；(2) 工具 1 真实的副作用（写下的文件）在历史里仍有体现，历史和文件系统不会分叉。截断会让中断对模型不可见——那个分叉才是真正的失效模式。依赖 C6（本端点一条 assistant 消息里是否真的会出现多个 `tool_use`，待测）；若本端点从不并行发工具调用，这条规则原样成立（工具组是原子的），只是覆盖它的属性测试要自己**构造**一个并行组，而不是等真实响应产生。

3. **在 LLM 流的中途中断。** 直觉做法（错的）：把已累积的部分 assistant 文本追加进去，免得丢东西。这会产生一条没有 `tool_calls` 的 assistant 消息，而且如果一个 delta 都没到，内容是空的——空 text 块违反协议约束（待 C7/C8 验证）；即便本端点更宽容，这个不变量照守——宽容的端点只是让 bug 更晚暴露。正确做法：什么都不追加。`agent.py:377` 处的 `self.messages.append(assistant_msg)` 发生在那个 await *之后*，所以流中途的取消在构造上就让历史保持干净；部分文本进入 `Interrupted.partial_text`，只给渲染器用。这个正确性依赖于现有代码里一个行序上的巧合——用一个测试把它钉住，好让将来的重构没法把 append 挪到 await 之上。

4. **修复正在进行时取消被投递了第二次。** 直觉做法（错的）：把修复放在 `except asyncio.CancelledError` 处理器里、放在一次 `await self._emit(...)` 之后。如果驱动方在轮询并再次调用 `task.cancel()`（`cli.py:777-781` 就会这么干），那个 `await` 会重新抛出 `CancelledError`，修复永远不会执行——留下的正是你想避免的那个孤立 `tool_use`。正确做法：`_repair_history()` 是纯同步的，作为处理器的*第一条*语句运行；之后每一个 `await` 都各自包在 `try/except CancelledError: pass` 里。相关地：`interrupt()` 通过 `self._interrupt_requested` 做到幂等，所以第二次 Esc 没法冲掉一次正在进行的修复。

5. **无条件吞掉 `CancelledError`。** 直觉做法（错的）：`except asyncio.CancelledError: return "cancelled"`。如果取消来自外层的 `asyncio.timeout()` 或者正在拆解的 TaskGroup，吞掉它会让这个 task 正常完成，外层的关停就会挂住或行为异常。正确做法：无论如何都做修复并发事件，但除非 `self._interrupt_requested` 被置上，否则 `raise`——也就是只吞掉*我们自己*那次有意的中断。这就是一个 cancel-aware 组件和一个悄悄破坏父级结构化并发的组件之间的区别。

6. **Steering 在同一个 assistant 轮次的工具 2 与工具 3 之间到达。** 直觉做法（错的）：在工具执行循环内部（`agent.py:404-474` 附近）排空队列，让用户的重定向尽快落地。那会把一条 `user` 消息插到一个 assistant 的 `tool_use` 块和它剩下的 `tool_result` 之间——正是中断路径存在的意义所要防止的那个孤立 `tool_use` 违规，现在由一个功能亲手造出来了（协议约束，待 C6/C7 验证；端点更宽容也只是让 bug 更晚暴露，所以规则照守）。正确做法：**只**在步骤顶部排空（`agent.py:326` 之前），那里每个 id 都已被满足。延迟代价最多一次工具调用；正确性是绝对的。

7. **在模型最后一轮期间输入的 steering。** 如果模型没有返回 `tool_calls`，`run()` 在 `agent.py:394` 返回，排队的那行就滞留了——然后在下一次毫不相关的 `run()` 开头被注入，读起来是句莫名其妙的话。正确做法：在那个 return 之前再排空一次；如果非空，就追加它、发出 `SteeringInjected`，然后 `continue` 循环而不是返回。`max_steps` 仍然给它兜底。

8. **流式加重试。** 直觉做法（错的）：像 `generate` 那样，把流式调用原样包进现有的 `async_retry` 装饰器（`anthropic_client.py:275-283`）。流中途失败时重试会从头重放，用户先看到半个回答，然后是*另一个*回答，两者无缝拼在一起。正确做法：在闭包里跟踪 `delta_count`；只在它为 0 时重试，否则抛出 `AgentError(fatal=True)`。从渲染器的角度看，流不是幂等的。

9. **thinking 和文本被渲染两次。** 直觉做法（错的）：流期间发 `TextDelta`，*同时*让 `_on_MessageComplete` 打印累积的 `content`/`thinking`（`agent.py:380-387` 的直接移植）。所有东西都出现两遍。正确做法：`MessageComplete.streamed` 从 `self._streamed_this_step` 取值，渲染器只在它为 `False` 时打印正文——这同时也让 OpenAI 客户端的非流式回退（`llm/base.py` 里默认的 `stream_generate`）在客户端零改动的情况下依然渲染正确。

10. **截断被留在引擎里。** `agent.py:416-421`（200 字符）和 `agent.py:461-462`（300 字符）看起来是无害的格式化，但如果 `ToolCallFinished.content` 携带的是截断后的字符串，ACP 客户端、JSONL eval 日志、以及将来任何权限 UI 就永远看不到真实的工具输出——而你要等到在调试一段被截断的 stack trace 时才会发现。事件承载完整保真度；每一个上限都活在渲染器的 `__init__` 里。

11. **thinking 块的签名被丢掉。** `anthropic_client.py:140` 序列化出的是 `{"type": "thinking", "thinking": msg.thinking}`，没有 `signature`，因为 `Message.thinking`（`schema/schema.py:34`）是个裸 `str`，没地方放。这在本端点上要不要紧，是 C10 的事（thinking 块能否原样回传、是否需要 `signature`，待测）；而流式恰恰让答案可见——你要么看到 `signature_delta` 在线上到达，要么看到它压根不来。若 C10 结论是需要签名，那重放一个不带签名的 thinking 块会让下一轮失败，修法就是加一个字段：`Message` 上的 `thinking_signature: str | None`，在 `_parse_response` 里填充、在 `_convert_messages` 里回显。若 C10 结论是不带签名也能原样回传，就不加这个字段，把这条坑记为"本端点不适用"；再退一步的降级路径是干脆不回传 thinking，并记一条 PITFALL 说明代价。

## 怎么证明它有效

**全部离线运行，10 秒以内跑完。不需要在线 API，不需要 SWE-bench。**

加 `tests/fakes.py`（约 60 LOC）：一个具备 `LLMClient` 表面的 `ScriptedLLM`，由一串预置的 `LLMResponse` 驱动。它的 `stream_generate` 把 `content` 切成 5 字符一段的 `on_delta("answer", ...)` 调用，并接受 `cancel_after_deltas: int | None` / `cancel_during_tool: str | None` 这两个钩子，在选定的点抛出 `asyncio.CancelledError`。再加一个不变量检查器，那才是真正的交付物：

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

然后是 `tests/test_events.py`，四个测试，用 `pytest tests/test_events.py -q` 跑：

1. `test_renderer_parity` —— 让一个固定的 3 步脚本跑过 `Agent(on_event=ConsoleRenderer())`，`stream_generate` 走非流式回退；用 `capsys` 抓 stdout，与 `tests/golden/console_run.txt` 比对，后者是在**重构前**那个 commit 上跑同一脚本生成一次的（`git stash` 掉改动，跑，`git stash pop`）。证明这次解耦是行为保持的，不是重写。
2. `test_silent_mode` —— 同一脚本，`on_event=None`，断言 `capsys.readouterr().out == ""`。一行；永久杀死"某个 `print` 又溜回引擎"这一类 bug。
3. `test_interrupt_leaves_no_orphan` —— 脚本：assistant 发出两个工具调用；第一个工具成功，第二个阻塞；在它期间取消。断言三件事：`assert_history_valid(agent.messages)` 通过；`len(agent.messages)` **增加**了恰好 1（那条合成结果）而不是减少；以及工具 1 幸存下来的结果内容仍然在场。作为对照断言，先快照之前的 `len(messages)`，在一份拷贝上内联调用本 PR 里被删掉的 `_cleanup_incomplete_messages` 逻辑，断言它*掉了* 3 条消息——两个数字之间的差就是整个论证。
4. `test_steering_injects_at_boundary` —— 在一个假工具的函数体里（也就是轮次中途、工具 1 和工具 2 之间）调用 `agent.steer_nowait("use pytest not unittest")`。断言：那条 assistant 消息与它第二个 `tool` 结果之间任何位置都不存在 `user` 消息；恰好一条 `user` 消息包含 steering 文本；它的下标大于最后一个 tool-result 下标、小于下一条 assistant 的下标。外加 `assert_history_valid`。

**要展示的工件。** (a) `pytest tests/test_events.py -q` → `4 passed`。(b) 挂上 `JsonlSink` 跑 `python -m mini_agent.cli --task "…" ` → `events.jsonl`；`jq -r '.type' events.jsonl | uniq -c` 打印出事件带，它*就是*那张架构图。(c) 一个实测数字，一次 API 调用，约 30 秒：首 token 时间，从 jsonl 里算 `first TextDelta.ts − RunStarted.ts`，对比非流式的 `MessageComplete.ts − RunStarted.ts`。两个都报出来，例如"0.4 s vs 11.2 s"。(d) 可选的 20 秒终端录屏：`mini-agent --task "print the numbers 1..30, one per second, using bash"`，运行中途敲 `actually just print 1..5` 回车，展示 steering 在下一步落地，然后按 Esc，再 `pgrep -f 'sleep'` 什么都找不到，说明子进程是被杀掉而不是变成孤儿。

## 深度追问

1. **"为什么用回调而不是 `async for event in agent.run()`？迭代器更 Pythonic。"** 三条具体理由。(1) *Teardown。* 中断要求在取消时做历史修复。在异步生成器里，消费者停止拉取时，收尾以 `GeneratorExit` 的形式在一个 GC 选定的时刻到来，而在 teardown 期间 `await` 会抛 `RuntimeError: async generator ignored GeneratorExit`，除非每个消费者都用 `contextlib.aclosing()`。那会让整个系统里最要命的那个正确性机制依赖消费者的自觉。用回调，引擎握着控制流，修复就是一个普通的 `except asyncio.CancelledError` 块。(2) *方向。* 生成器只能推。权限提示是请求/响应——引擎阻塞，UI 回答。回调可以是 `async` 的，可以携带一个 `asyncio.Future`（这就是为什么 `PermissionRequest` 是唯一一个非 frozen 的 dataclass）。迭代器无论如何都还得再要一条通道，而一旦你有了它，回调从一开始就是那个原语。(3) *爆炸半径。* 全部 9 个 `run()` 调用点（`cli.py:587`、`cli.py:775`、四个测试、四个示例）都写着 `result = await agent.run()`。回调保住 `-> str`，一个都不用改。迭代器我照样出——作为一个从回调派生出来的 12 行 `QueueSink`，而不是反过来。

2. **"把现在的取消到底错在哪讲一遍。"** 三个碰巧共用一个名字的独立 bug。第一，什么都没被取消：`cli.py:775-781` 轮询 `agent_task.done()` 并置一个布尔量，从不调 `.cancel()`，于是 `cli.py:786` 的 `except asyncio.CancelledError` 是死代码，Esc 最长会被忽略到 `bash` 的 120 秒超时（`bash_tool.py:399`）。第二，`_cleanup_incomplete_messages`（`agent.py:73-94`）截断到*最后一条 assistant 消息*之前，而在 `agent.py:477` 那个调用点，那条消息是一个**已完成**的步骤，它的 tool result 已经在 `agent.py:474` 追加过了——所以按 Esc 会删掉已完成的工作，而它的副作用还留在磁盘上。在 `agent.py:318` 它删的是一个压根没有任何东西孤立的完整轮次。第三，它从不检查它本该保护的那个不变量：它从不看 `tool_calls` 的 id。修法是一个形状完全不同的操作——为未被满足的 id *追加*合成 tool result，绝不删除——因为 API 的约束是"没有孤立的 `tool_use`"，追加总能满足它，而截断只能靠毁掉上下文来满足它。

3. **"为什么要合成一个假的 tool result，而不是回退到一个干净状态？"** 回退在局部更简单，在全局是错的，两个理由。模型会丢掉"自己被中断过"这个事实，于是下一轮它把同一个工具重跑一遍——用户对一个很慢的 `npm install` 按了 Esc，agent 的第一个动作就是再跑一遍。而且被中断的那一轮里*已完成*的副作用是真的：文件写了，分支建了。截断让历史宣称并非如此，之后每一个决策都是基于一个和文件系统互相矛盾的世界模型做出的。合成 `[interrupted by user before this tool completed]` 保住了因果记录，而且是 O(1) 安全的：追加永远不可能造出新的孤儿，而每一次截断都得自证它没有从一个工具组的中间切下去。被否决的替代方案：把部分 assistant 文本写进历史——它产生一条没有 tool_calls、内容可能为空的 assistant 消息，wire format 的约束会拒（待 C8 验证——而一个容忍它的端点只会把这个错误藏得更久）；而且它在教模型模仿被截断的输出。

4. **"steering 文本你到底注入在哪，注在别处会坏掉什么？"** 只在步骤循环的顶部，紧挨着 `_summarize_messages()` 之前（今天的 `agent.py:326`）。注入到工具执行循环内部（`agent.py:404-474`）会把一条 `user` 消息放在一个 assistant 的 `tool_use` 块和它剩下的 `tool_result` 之间——正是中断路径存在意义所要防止的那个孤立 `tool_use` 违规，现在是自己捅的（协议约束，待 C7 验证；不管端点最后有多严，这个不变量照守）。还有两条顺序上的事实。它走在压缩*之前*而不是之后，因为 `_summarize_messages` 保留每一条 `user` 消息（`agent.py:186`）并把它的 token 计入价格。而在 wire 上，我不是发出连续第二个 user 轮次，而是把它作为一个尾随的 `{"type":"text"}` 块合并进那条携带 `tool_result` 块的同一条 user 消息（`anthropic_client.py:158-176`）——`tool_result` 排在前面，因为开头是 text 块会被拒。这个合并压在 C9 上（待测）：若本端点接受连续 user 消息，合并就是优化而非必需，不合并即是降级路径。最后是 `agent.py:394` 之前的末轮排空：没有它，模型最后一轮期间输入的一行会滞留，然后在一次毫不相关的后续运行开头重新浮出来。

5. **"你怎么从一个 token 流里拿到一个 tool-call dict？"** 用 `anthropic` 0.72.1，我用 `client.messages.stream()`（那个 helper），不用 `messages.create(stream=True)`，然后取 `await stream.get_final_message()`——helper 会累积 `input_json_delta` 片段，交回来的 `tool_use.input` 已经解析好，正是 `_parse_response`（`anthropic_client.py:221-232`）消费的东西，所以下游一点不变。如果你用裸流，重组就归你自己管：按 `content_block_index` 缓冲 `partial_json` 字符串，在 `content_block_stop` 时 `json.loads`，而且——大家常漏的那个情况——一个入参为空的工具调用会发出*零*个 `input_json_delta` 事件，于是天真的 `json.loads("".join(parts))` 会抛 `JSONDecodeError`；你必须默认成 `{}`。架构上的要点是：这些知识活在客户端里，永远不进 agent 循环——`stream_generate` 返回的 `LLMResponse` 和 `generate` 一样，delta 走一个旁路回调离开，所以循环永远不会知道存在不完整的 JSON。以上全部以 C4/C5 在本端点成立为前提（待测）；若不成立，基类那个非流式回退返回的是同一个 `LLMResponse`，上面这套架构论证原样成立，只是没被跑起来而已。

6. **"流式和你的重试层之间是什么关系？"** 除非你给它们加约束，否则它们不兼容。现有的装饰器（`anthropic_client.py:275-283`）在失败时重新发起请求，这在唯一的可观测量是返回值时没问题。一旦 delta 已经发出去，渲染器就已经把字节给人看了，重放会把两个不同的回答无缝拼在一起。所以 `stream_generate` 在自己的闭包里数 delta，只在计数为 0 时重试；首 token 之后，失败变成 `AgentError(fatal=True)`。一般性原则：重试只对那些可观测效果被限制在返回值里的操作是安全的，而流式故意打破了这一点。生产级的答案是一个 `StreamRestarted` 事件加渲染器侧对当前块的回滚——我故意没有实现那个。

7. **"你的按键读取器住在一个线程里。有哪些并发风险？"** 两个。第一，`asyncio.Queue` 不是线程安全的：从读取线程 `put_nowait` 可能和事件循环的 waiter 记账竞争，丢掉一次唤醒，所以每一次交接都要经过 `loop.call_soon_threadsafe(agent.steer_nowait, line)`，并且 loop 要在 `esc_thread.start()` 之前捕获。`agent.interrupt()` 同理，它会调 `Task.cancel()`——从外部线程取消一个 task 是未定义行为。第二，终端状态：该线程把 tty 置为 cbreak（`cli.py:754`）并在 `finally` 里恢复（`cli.py:765`），但 `tty.setcbreak` 对 `ECHO` 标志的处理在各 Python 版本间不同，所以我用 `termios` 显式清掉 `ECHO`，自己回显输入的字符——否则你得到的要么是看不见的输入，要么是双份的。另外今天 `cli.py:756-763` 的那个循环不只是忽略非 Esc 字节，它还把它们从 stdin *消费*掉，于是它们也永远到不了下一个提示符。

8. **"解耦到底给你换来了什么——别说，给我看。"** 四件具体的事。(1) `acp/__init__.py:127-165` 被删掉：一份 39 行的、已经漂移的循环 fork——没有 `logger.log_*` 调用，没有 `_summarize_messages()`，所以 ACP 会话从不压缩、撑爆上下文窗口，而 CLI 不会。一个循环、两个 sink，那一类漂移在结构上就不可能发生。(2) `on_event=None` 就是静默 eval 模式，由一行测试断言。(3) `mini_agent/agent.py:15`——`from .utils import Colors, calculate_display_width`——被删掉；引擎不再导入任何和终端相关的东西，这是 diff 里的一行式证明。(4) `agent.py:416-421` 和 `agent.py:461-462` 处的 200 字符和 300 字符截断搬到渲染器，于是事件携带完整保真的工具输出，ACP 客户端和 JSONL 日志终于看到真东西。

## 前置条件

1. `mini_agent/agent.py:73-94` —— `_cleanup_incomplete_messages()` 必须被**删掉**而不是打补丁，连同它的三个调用点（`agent.py:318-319`、`397-398`、`476-478`）和 `_check_cancelled()`（`agent.py:63-71`）。已验证 `mini_agent/`、`tests/`、`examples/` 里任何地方都没有其他引用。任何建立在它之上的中断工作都会继承它的数据丢失。

2. `mini_agent/cli.py:717-791` —— 在接线 `interrupt()` 之前必须移除 `cancel_event` 那套管道（`cli.py:718-719, 737, 762, 780, 789`）和 100 毫秒的 `while not agent_task.done()` 轮询循环（`cli.py:777-781`）。把轮询器留着，意味着 `_repair_history` 运行期间可能被重新投递第二个 `.cancel()`。

3. `mini_agent/tools/bash_tool.py:398-409` —— 加上 `except asyncio.CancelledError: process.kill(); raise`。这不是可选项：`wait_for` 只在超时时杀子进程，所以今天一次正确的中断会留下一个还活着、管道还开着的子进程，而只要 `pgrep sleep` 还能找到它，"我的 Esc 能用"就不是一个可演示的主张。

4. 有条件，仅当 C10（[`../PROVIDER_CAPABILITIES.md`](../PROVIDER_CAPABILITIES.md)，待测）测出本端点要求重放的 thinking 块带 `signature`：`mini_agent/schema/schema.py:34` 需要 `thinking_signature: str | None`，在 `anthropic_client.py:216-220` 填充，在 `anthropic_client.py:140` 回显。当前的序列化把签名丢了（`anthropic_client.py:140`）——这是关于我们自己代码的事实，跟测出什么结论无关。在 C10 探测出来之前这一项保持未知：不要凭猜测加这个字段；如果 C10 是否定的，写在文档里的降级路径就是不回传 thinking 块并记下代价。

## 明确不做

不做：(1) `PermissionRequest` 被定义并写进文档，但**不接线**到任何工具——没有 allow/deny 拦截，没有规则持久化，没有 `--dangerously-skip-permissions`。它存在纯粹是为了证明 sink 这个形状能承载请求/响应，而这正是反对迭代器设计的论据。(2) 流中途重试时没有渲染器侧回滚——我选择在首 token 之后禁止重试，而不是实现 `StreamRestarted` + 块回退。(3) 没有 Live/TUI 渲染器，没有 spinner，没有输出重排；`ConsoleRenderer` 刻意与今天的输出逐字节一致，这样重构才是可证明的。(4) `OpenAIClient` 拿的是 `llm/base.py` 里那个非流式的 `stream_generate` 回退，不是真正的 SSE 实现——一个 provider 流式，回退证明这条缝对不流式的那个也成立。(5) steering 没有 Windows 路径；`msvcrt` 分支（`cli.py:729-742`）保持只支持 Esc 的行为。(6) 没有背压、没有事件缓冲——慢的 sink 按设计就会阻塞循环。

对面试官说："我做的是那道缝，加一条完整贯穿它的纵切——流式、中断、steering 全都跑在同一个 event sink 上，ACP 那份 fork 的循环被删掉也是因为它也跑在上面。权限是第四个消费者，我故意停在 dataclass：再加一个权限 UI 不会让我对机制多懂一点，而把中断做对——合成 tool result 而不是删掉已完成的轮次、在任何 await 之前完成修复、把不属于我的取消重新抛出去——才是全部真正的难点所在。"

## 代码量

约 750 行改动：约 450 行新增（`events.py` 约 130，`render/console.py` 约 170，其中约 60 是从 `agent.py:329-336/416-421/461-462` 逐字搬过来的，`render/jsonl.py` 约 25，`tests/fakes.py` 约 60，`tests/test_events.py` 约 140），`agent.py` 里约 120 行修改（+120/−90，净 −30，最终 `print(` 数为零），跨 `llm/base.py`（+12）、`llm/anthropic_client.py`（+55）、`llm/llm_wrapper.py`（+5）、`cli.py`（+70/−60）、`bash_tool.py`（+4）约新增 115 行，以及约 39 行直接删除（`acp/__init__.py:127-165`，由一个约 45 行的 `AcpSink` 取代）。

## 工期

4-5 个专注日。第 1 天：`events.py` + `ConsoleRenderer` + 迁移全部 30 个 print 点，用 golden 输出 parity 测试证明 stdout 逐字节一致。第 2 天：中断——删掉 `_cleanup_incomplete_messages`，写 `_repair_history` + `assert_history_valid`，重接 `cli.py`，给 `bash_tool.py` 打补丁，落地测试 3 和 4。第 3 天：流式——在 base/anthropic/wrapper 上做 `stream_generate`，`streamed` 双重渲染守卫，首 token 之后不重试的规则。第 4 天：steering——行缓冲读取线程、`call_soon_threadsafe` 交接、边界注入、`_convert_messages` 里的 tool_result 合并、末轮排空。第 5 天：删掉 `acp/__init__.py:127-165`，写 `AcpSink` + `JsonlSink`，取到首 token 时间那个数字和终端录屏。
