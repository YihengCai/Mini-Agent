# 假 LLM + 运行记录器 + 微型 eval

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`eval/ — the measurement rig: an in-process RunRecorder wired into Agent, a ScriptedLLM fake for zero-cost deterministic loop tests, and a 12-task suite that runs in git worktrees with shell verify commands.`


## 一句话

给 `Agent` 装上一个约 25 行的事件钩子，用 12 个手写任务驱动它——每个任务隔离在自己的 git worktree 里（各一个子进程），由外部 shell verify 命令判定——最后吐出一份 JSONL：{outcome, steps, tokens, usd, wall, tool_error_rate, edit_precision, compaction_count}；再加一个 ScriptedLLM 假实现，让循环、压缩器和权限引擎在**零 API 调用**的前提下被断言。

## 为什么这是难点

没有它，这套作品集里其他每一条机制主张都不可证伪。"我实现了上下文管理"什么都不说明，除非你能拿出一个任务：在 8k token 预算下之前失败、之后通过，且两次的 compaction_count > 0。"我实现了执行沙箱"什么都不说明，除非有一个任务之前能往 workspace 外写、之后被拒绝。而这个仓库现在两个数字都产不出来：`tests/test_agent.py:81-87` 在文件根本没被创建时也 `return True`，`tests/test_agent.py:94` 出异常时 `return False` 而不是失败，所以不管 agent 干了什么 pytest 都是绿的；`tests/test_llm.py:52-57` 和 `tests/test_llm_clients.py` 用同样的方式吞异常；而且这些测试每一个都在烧真实 API 调用，所以没人跑它们。

这也是真实 agent 团队会以一种特定方式做错的地方。天真的测量装置每个任务报一个布尔值加一个 token 数，这没法区分"模型答错了"、"循环撞到 max_steps"、"API 429 了"和"harness 崩了"——而在这个代码库里这四种长得一模一样，因为 `agent.py:346-356` 捕获了 LLM 异常然后*返回一个字符串*，调用方无法把它和正常完成区分开。一个分不清模型失败和 harness 失败的装置，会让你花一周去调错的东西。把 outcome 分类法、每次调用的 usage 记账、隔离边界这三件事做对，才是这里真正的工程内容。

## 仓库现状

**测试是装饰品。** `tests/test_agent.py:55-100` —— 整个断言面就是 `print()`。第 81/84/87 行全部 `return True`（包括打印 "File was not created" 的那个分支）；第 94 行异常时 `return False`。pytest 看到的是一个返回了的协程；什么都失败不了。`tests/test_llm.py:52-57` 和 `tests/test_llm_clients.py:54-60` 用的是同一个 `except Exception: return False` 形状。`tests/test_integration.py:243-262` 把两个测试都包进了裸 `try/except: print(...)`。这些每一个都打到线上 MiniMax API（`mini_agent/config/config.yaml`，模型 `MiniMax-M2.7`），所以它们慢、贵、flaky——这三条性质保证了它们永远不会被运行。

**没有任何办法从一次运行里取出数字。** `agent.py:358-360` 的注释写着 "Accumulate API reported token usage"，干的却是 `self.api_total_tokens = response.usage.total_tokens` —— 一次覆盖写。跑完 20 步之后它保存的是最后一次调用的上下文大小，不是这次运行的成本。任何地方都没有对 `prompt_tokens`/`completion_tokens` 求和。`agent.py:394` 在正常结束时返回 `response.content`，`agent.py:490-492` 返回字符串 `"Task couldn't be completed after N steps."`；`agent.py:356` 返回 `f"LLM call failed: {e}"`。三种完全不同的结局，一个 `str` 返回类型，没有结构化结果。

**唯一的 trace 无法解析。** `AgentLogger._write_log`（`logger.py:159-174`）把带虚线横幅的文本段落连同 JSON 正文写进 `~/.mini-agent/log/agent_run_<YYYYmmdd_HHMMSS>.log`（`logger.py:32-34`）。秒级粒度的文件名在并行下会撞车；`AgentLogger.__init__`（`logger.py:19-28`）把目录写死，不带参数。靠正则从这里面捞每步耗时或工具错误率，比加一个钩子还难。

**成本在原理上就推不出来。** `anthropic_client.py:238-247` 把 `cache_read_input_tokens` 和 `cache_creation_input_tokens` 折叠进单个 `prompt_tokens`。cache read 与 cache write 的计费倍率不同（厂商公开文档给的量级参考是约 0.1x 与 1.25x —— 厂商文档的量级参考，非本端点实测），所以 cache 命中率的改进在当前的 `TokenUsage`（`schema/schema.py:40-45` 只有三个 int 字段）里从算术上就是不可见的。依赖 C2/C3（[能力矩阵](../PROVIDER_CAPABILITIES.md) —— 本端点是否回报缓存 usage，待测）；若不支持：拆出来的两个字段恒为 0，装置只报 token 数，并在 `docs/bench.md` 里写明本端点不支持 prompt caching。

**装置必须能看见的那个明显的编辑质量 bug。** `EditTool.execute`（`file_tools.py:256-283`）宣称 "must match exactly and appear uniquely in the file"（`file_tools.py:230-232`），但第 280 行是 `content.replace(old_str, new_str)` —— 替换*每一处*出现，而第 273 行只检查存在性，从不检查计数。一次让 agent 改坏了三个调用点的运行，报的是 `success=True`。任何"工具成功率"指标都会给它打 100%。

**决定隔离设计的并行风险。** `BackgroundShellManager._shells` / `._monitor_tasks`（`bash_tool.py:109-110`）是*类*属性，被进程内每个 `BashTool` 共享。`mcp_loader` 持有进程全局的 stdio 连接，由 `cli.py:435-448` 统一拆除。一个进程里的两个 agent 两者都共享。

**唯一的非交互入口**是 `cli.py:583-596`（`--task`），它打印 ANSI 横幅、在裸 try/except 里调 `agent.run()`、打印统计、什么都不返回。作为 harness API 不可用；runner 必须直接构造 `Agent`（复用 `cli.add_workspace_tools`，`cli.py:399-432`）。

## 最小实现

## 新包：`mini_agent/eval/`

```
mini_agent/eval/
  __init__.py
  recorder.py     # RunRecorder, NullRecorder, RunTrace, metric derivation   (~130 LOC)
  scripted.py     # ScriptedLLM + response builders                          (~110 LOC)
  pricing.py      # model -> price table, usd()                              (~40 LOC)
  spec.py         # TaskSpec pydantic model + YAML loader                    (~60 LOC)
  worker.py       # runs ONE task in its own process, writes result.json     (~140 LOC)
  runner.py       # worktree setup, subprocess fan-out, JSONL sink, CLI      (~200 LOC)
  report.py       # results.jsonl -> markdown tables                         (~110 LOC)
  tasks/*.yaml    # 12 task specs                                            (~180 LOC)
  fixtures/*.sh   # seed-repo setup scripts                                  (~120 LOC)
tests/test_loop_scripted.py      # deterministic loop tests, no API          (~140 LOC)
tests/test_compactor_scripted.py # deterministic compactor invariants        (~90 LOC)
```

---

### 1. `recorder.py` —— 钩子

```python
# mini_agent/eval/recorder.py
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

class NullRecorder:
    def event(self, kind: str, **f: Any) -> None: ...
    def timer(self, kind: str, **f): return _NullTimer()

@dataclass
class RunRecorder:
    events: list[dict] = field(default_factory=list)
    t0: float = field(default_factory=perf_counter)

    def event(self, kind: str, **f: Any) -> None:
        self.events.append({"kind": kind, "t": perf_counter() - self.t0, **f})

    def timer(self, kind: str, **f):          # 上下文管理器，记录 dur_s
        return _Timer(self, kind, f)
```

事件种类与载荷（这就是全部契约）：

| kind | 字段 |
|---|---|
| `llm_call` | `dur_s, prompt_tokens, completion_tokens, cache_read, cache_write, finish_reason, n_tool_calls, purpose`（`purpose ∈ {"step","summary"}`） |
| `llm_error` | `dur_s, exc_type, message, retries` |
| `tool_call` | `name, dur_s, success, error_class, args_digest` |
| `edit_attempt` | `path, occurrences, success, was_read_since_write, old_len, new_len` |
| `compaction` | `tokens_before, tokens_after, n_msgs_before, n_msgs_after, summary_calls` |
| `step_end` | `step, dur_s, n_tool_calls` |
| `finish` | `reason ∈ {"stop","max_steps","llm_error","cancelled"}, steps` |

`RunTrace.derive() -> dict` 把事件折叠成上报的那一行：

```python
def derive(events) -> dict:
    llm = [e for e in events if e["kind"] == "llm_call"]
    tools = [e for e in events if e["kind"] == "tool_call"]
    edits = [e for e in events if e["kind"] == "edit_attempt"]
    return {
      "steps": max((e["step"] for e in events if e["kind"]=="step_end"), default=0) + 1,
      "llm_calls": len(llm),
      "prompt_tokens":     sum(e["prompt_tokens"] for e in llm),
      "completion_tokens": sum(e["completion_tokens"] for e in llm),
      "cache_read_tokens": sum(e["cache_read"] for e in llm),
      "cache_write_tokens":sum(e["cache_write"] for e in llm),
      "tool_calls": len(tools),
      "tool_error_rate": (sum(not e["success"] for e in tools) / len(tools)) if tools else None,
      "edit_attempts": len(edits),
      "edit_first_try_rate": (sum(e["success"] for e in edits)/len(edits)) if edits else None,
      "ambiguous_edits": sum(1 for e in edits if e["occurrences"] > 1),
      "stale_edits":     sum(1 for e in edits if not e["was_read_since_write"]),
      "compaction_count": sum(1 for e in events if e["kind"]=="compaction"),
      "tokens_reclaimed": sum(e["tokens_before"]-e["tokens_after"]
                              for e in events if e["kind"]=="compaction"),
      "summary_llm_calls": sum(1 for e in llm if e["purpose"]=="summary"),
    }
```

`edit_precision` 刻意是**三个**数字，不是一个：`edit_first_try_rate`、`ambiguous_edits`、`stale_edits`。单个标量会盖住编辑修复恰恰要针对的那个失败。

---

### 2. 打进 `agent.py` 的钩子 —— 全部改动（约 25 行）

- **`agent.py:21-29`** —— 给 `__init__` 加参数 `recorder=None`；**`agent.py:57`** —— 在 `self._skip_next_token_check` 之后加：
  ```python
  self.recorder = recorder or NullRecorder()
  self.cum_prompt_tokens = 0
  self.cum_completion_tokens = 0
  ```
- **`agent.py:344-345`** —— 把调用包起来：
  ```python
  t = perf_counter()
  try:
      response = await self.llm.generate(messages=self.messages, tools=tool_list)
  except Exception as e:
      self.recorder.event("llm_error", dur_s=perf_counter()-t,
                          exc_type=type(e).__name__, message=str(e))
      self.recorder.event("finish", reason="llm_error", steps=step)
      ...                                   # 现有的 346-356 行不变
  self.recorder.event("llm_call", purpose="step", dur_s=perf_counter()-t,
                      prompt_tokens=..., completion_tokens=...,
                      cache_read=..., cache_write=...,
                      finish_reason=response.finish_reason,
                      n_tool_calls=len(response.tool_calls or []))
  ```
- **`agent.py:358-360`** —— 把覆盖写换成累加，**同时**保留最后一次调用的值（`agent.py:174` 的压缩器确实想要的是"最后一次请求的大小"而非总和——这个语义必须保住）：
  ```python
  if response.usage:
      self.api_total_tokens = response.usage.total_tokens          # 上下文代理值，不变
      self.cum_prompt_tokens += response.usage.prompt_tokens        # 新增
      self.cum_completion_tokens += response.usage.completion_tokens
  ```
- **`agent.py:434-436`** —— 给工具调用计时并记录：
  ```python
  t = perf_counter()
  result = await tool.execute(**arguments)
  self.recorder.event("tool_call", name=function_name, dur_s=perf_counter()-t,
                      success=result.success,
                      error_class=_classify(result.error) if not result.success else None)
  ```
- **`agent.py:224-233`** —— 在 `_summarize_messages` 内部，`self.messages = new_messages` 之后：
  ```python
  self.recorder.event("compaction", tokens_before=estimated_tokens,
                      tokens_after=self._estimate_tokens(),
                      n_msgs_before=n_before, n_msgs_after=len(new_messages),
                      summary_calls=summary_count)
  ```
  （`n_before = len(self.messages)` 在 `agent.py:185` 处捕获。）
- **`agent.py:390-394`** → 在 return 之前 `self.recorder.event("finish", reason="stop", steps=step)`；**`agent.py:489-492`** → `reason="max_steps"`；**`agent.py:320/399/479`** → `reason="cancelled"`。
- **`agent.py:483-487`** → `self.recorder.event("step_end", step=step, dur_s=step_elapsed, n_tool_calls=len(response.tool_calls))`。

`edit_attempt` **不**从 `agent.py` 发出。它来自一个 wrapper，这样编辑前的文件内容能在写入之前被捕获：

```python
# mini_agent/eval/recorder.py
class RecordingEditTool(EditTool):
    def __init__(self, inner: EditTool, rec, read_log: dict[str, float]):
        ...
    async def execute(self, path, old_str, new_str):
        p = self._resolve(path)
        before = p.read_text(encoding="utf-8") if p.exists() else ""
        occ = before.count(old_str)
        fresh = self.read_log.get(str(p), -1.0) >= (p.stat().st_mtime if p.exists() else 0)
        r = await self.inner.execute(path, old_str, new_str)
        self.rec.event("edit_attempt", path=str(p), occurrences=occ,
                       success=r.success, was_read_since_write=fresh,
                       old_len=len(old_str), new_len=len(new_str))
        return r
```
`read_log` 由配套的 `RecordingReadTool` 喂数据，它在成功时打上 `read_log[path] = time.time()`。两者都是围绕 `cli.add_workspace_tools` 在 `cli.py:422-424` 构造的对象的直接替换式 wrapper；worker 在那次调用之后把它们换掉。**不改 `file_tools.py`。**

---

### 3. `scripted.py` —— 假 LLM

```python
# mini_agent/eval/scripted.py
Matcher = Callable[[list[Message], list | None], bool]

class ScriptExhausted(RuntimeError): ...

class ScriptedLLM:
    """Duck-typed stand-in for llm.LLMClient. Only .generate and .retry_callback
    are touched by Agent (agent.py:345 / cli.py:540)."""
    def __init__(self, rules: list[tuple[Matcher, LLMResponse | Callable]],
                 default: LLMResponse | Callable | None = None,
                 usage: TokenUsage | None = None):
        self.rules, self.default = rules, default
        self.calls: list[tuple[list[Message], list | None]] = []
        self.fired: list[int] = []
        self.retry_callback = None

    async def generate(self, messages, tools=None) -> LLMResponse:
        self.calls.append((copy.deepcopy(messages), tools))
        for i, (m, r) in enumerate(self.rules):
            if m(messages, tools):
                self.fired.append(i)
                return r(messages) if callable(r) else r
        if self.default is not None:
            return self.default(messages) if callable(self.default) else self.default
        raise ScriptExhausted(f"no rule for call #{len(self.calls)}")

    def assert_consumed(self):
        missing = [i for i in range(len(self.rules)) if i not in self.fired]
        assert not missing, f"unfired rules: {missing}"
```

构造器 + 预置 matcher：

```python
def say(text: str, usage=None) -> LLMResponse                      # finish_reason="stop"
def call(name: str, args: dict, id: str = "tc1", *more) -> LLMResponse
def is_summary_request(msgs, tools) -> bool:
    return tools is None and len(msgs) == 2 and \
           msgs[0].content.startswith("You are an assistant skilled at summarizing")
def at_step(n: int) -> Matcher      # 统计 `msgs` 里的 assistant 消息数
def after_tool(name: str) -> Matcher
```

`is_summary_request` 依据的是 `agent.py:278-280` 那段字面 system prompt，以及 `_create_summary` **不传 tools** 这个事实（`agent.py:275-283`）。

---

### 4. `spec.py` —— 任务 schema

```yaml
# mini_agent/eval/tasks/dup_string_edit.yaml
id: dup_string_edit
tier: edit
repo: fixture                  # "fixture" | "self"
setup: fixtures/dup_string.sh  # runs with cwd=<worktree>
prompt: |
  src/notify.py sends three notifications. Change ONLY the warning-level
  message (the second one) to read "WARN: disk almost full". Leave the
  other two byte-identical.
max_steps: 12
token_limit: 80000             # Agent(token_limit=...) knob, agent.py:28
timeout_s: 240
verify: |
  diff -u "$EVAL_EXPECT/notify.py" src/notify.py
```

```python
class TaskSpec(BaseModel):
    id: str; tier: str; repo: Literal["fixture","self"] = "fixture"
    base_sha: str | None = None      # required when repo == "self"
    setup: str | None = None
    prompt: str
    max_steps: int = 20
    token_limit: int = 80000
    timeout_s: int = 300
    verify: str                      # bash, cwd=<worktree>, exit 0 == pass
```

---

### 5. `runner.py` —— 隔离与扇出

```
runs/<UTC-stamp>/
  suite.json                 # model id, git sha of mini_agent, task ids, seeds
  <task_id>.<seed>/
     tree/                   # the worktree — the agent's workspace_dir
     expect/                 # golden files, OUTSIDE tree
     events.jsonl
     result.json
     agent.log               # AgentLogger redirected here
  results.jsonl
```

每个任务的 setup：
- `repo: self` → `git worktree add --detach <run>/<id>.<seed>/tree <base_sha>`（钉在 `953b943`，即当前 HEAD）。
- `repo: fixture` → `mkdir tree && git -C tree init -q && bash <setup> && git -C tree add -A && git -C tree commit -qm base`。提交这一步很重要：`verify` 之后就能用 `git -C tree diff --quiet`，而 runner 把 `git -C tree status --porcelain` 记录为 diff 足迹。
- `expect/` 由同一个 setup 脚本通过 `$EVAL_EXPECT` 填充，然后被设为只读（`chmod -R a-w`）。

**`git worktree add` 调用在一个 `asyncio.Lock` 下串行化** —— 它们会改动 `.git/worktrees` 并发生竞争。只有 agent 的运行扇出，在 `asyncio.Semaphore(4)` 之下。

每次运行是 `asyncio.create_subprocess_exec(sys.executable, "-m", "mini_agent.eval.worker", spec_json_path)`，外套 `asyncio.wait_for(..., timeout_s)`，超时则 `kill()` 并 `outcome="timeout"`。

`worker.py`（单任务，独立进程）：
```python
cfg = Config.load()
llm = LLMClient(api_key=..., provider=..., api_base=..., model=..., retry_config=...)
tools, skill_loader = await initialize_base_tools(cfg)     # cli.py:303
add_workspace_tools(tools, cfg, tree)                      # cli.py:399
tools = wrap_for_recording(tools, rec)                     # swaps Read/Edit
agent = Agent(llm, system_prompt, tools, max_steps=spec.max_steps,
              workspace_dir=str(tree), token_limit=spec.token_limit,
              recorder=rec)
agent.logger = AgentLogger(log_dir=run_dir)                # see prerequisites
agent.add_user_message(spec.prompt)
final = await agent.run()
```
然后由父进程验证，**在** agent 进程退出**之后**：
`bash -lc <verify>`，`cwd=tree`，`env={EVAL_EXPECT: expect/, PATH: ...}`，上限 120 秒。退出码 0 → pass。

`outcome` 分类法（绝不是一个裸布尔）：
`pass | fail_verify | max_steps | timeout | llm_error | crash | verify_error`
由 `finish` 事件 + verify 退出码 + 子进程返回码推导。

**反作弊，只记录不强制：** runner 保存 `git -C tree diff --stat`，如果 diff 触及了 verify 命令会读的任何路径，就标记 `touched_verify_surface=True`。`expect/` 位于 tree 之外并被 chmod 成只读，所以 agent 改不了 golden 文件。

---

### 6. `pricing.py`

```python
PRICES = {  # USD per 1M tokens: (input, output, cache_read, cache_write)
  "MiniMax-M2.7": (..., ..., ..., ...),
}
def usd(model, prompt, completion, cache_read, cache_write) -> float | None
```
未知模型 → `None`，`report.py` 打印 `n/a`。绝不编造一个 `$0.00`。

价格一律从**本端点自己的价目表**填（`mini_agent/config/config.yaml` 指向的那个 endpoint），**不要抄厂商公开价目**；本端点查不到的数字就留空，让 `usd()` 返回 `None`。`cache_read` / `cache_write` 两列依赖 C1–C3（待测）；本端点若不回报缓存 usage，这两列恒为 0，`$` 只由 input/output 算出，报告页脚写明本端点无 prompt caching。

---

### 7. `report.py` 的输出 —— README 表格

**逐任务（一次 suite 运行，N 个种子的中位数）：**

```markdown
| task | tier | pass | steps | in tok | out tok | cached | $ | wall s | tool err | edit 1st | amb | stale | compact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hello_write        | smoke   | 3/3 |  2 |  4.1k |  180 |  0   | 0.004 |   6 | 0.00 |  –   | 0 | 0 | 0 |
| fix_failing_test   | edit    | 3/3 |  6 | 31k   |  900 | 22k  | 0.012 |  24 | 0.09 | 1.00 | 0 | 0 | 0 |
| dup_string_edit    | edit    | 0/3 |  4 | 18k   |  400 | 12k  | 0.008 |  15 | 0.00 | 1.00 | 1 | 0 | 0 |
| context_marathon   | context | 1/3 | 31 | 240k  | 6.2k | 180k | 0.11  | 190 | 0.06 | 0.83 | 2 | 1 | 3 |
| sandbox_escape     | sandbox | 0/3 |  5 | 22k   |  600 | 15k  | 0.009 |  20 | 0.00 |  –   | 0 | 0 | 0 |
| **suite**          |         |**14/36**| — | 1.1M | 24k  | 0.7M | 0.48  | 720 | 0.04 | 0.91 | 5 | 3 | 9 |
```

**主张表 —— 真正重要的那张：**

```markdown
| claim | metric | before | after |
|---|---|---|---|
| edit uniqueness (occurrence count + refuse) | `ambiguous_edits` (suite) | 5 | 0 |
| edit staleness (read-before-write) | `stale_edits` (suite) | 3 | 0 |
| context manager (tail + structured summary) | `context_marathon` pass @ 8k budget | 0/3 | 3/3 |
| ” | `tokens_reclaimed` / compaction | 4.1k | 46k |
| sandbox containment | `sandbox_escape` pass | 0/3 | 3/3 |
| prompt caching (cache_control) | `cached / in tok` | 0% | 63% |
| ” | suite $ | 1.31 | 0.48 |
```

`prompt caching` 那两行依赖 C1–C3（`cache_control` 是否被接受、是否真的产生缓存命中、`input_tokens` 是否排除命中部分 —— 在本端点上全部待测）。三项里任一项不支持，这两行就从主张表里撤下，改成一句"本端点不支持 prompt caching"的说明，`$` 列只报 token 数，压缩器的双阈值改由纯上下文预算推导而不是缓存经济学。上面表里的每一个数字都是待测的占位值，不是指标。

每一格都能追溯到某个 JSONL 字段。`before` = 等价于 `git stash` 的做法（在机制落地前的 SHA 上跑整套 suite），这正是为什么 worktree 是隔离原语：`--suite core --at <sha>` 能把整套东西重跑在一棵旧树上。

---

### 8. 精确的 12 个任务

| # | id | tier | repo | 探测什么 | verify（要义） |
|---|---|---|---|---|---|
| 1 | `hello_write` | smoke | fixture（空） | 装置本身；这个都过不了，别的都没意义 | `python hello.py \| grep -qx 'hello mini-agent'` |
| 2 | `fix_failing_test` | edit | fixture：`calc.py` + `test_calc.py`，一个失败用例（`div` 除零抛 `ZeroDivisionError`，测试要的是 `None`） | read→edit→run 循环 | `pytest -q` |
| 3 | `dup_string_edit` | edit | fixture：`notify.py` 里同一个字面量出现 3 次 | **`file_tools.py:280` 的多处替换** | `diff -u $EVAL_EXPECT/notify.py src/notify.py` |
| 4 | `stale_edit` | edit | fixture：`gen.py` 会重写 `config.py`；prompt 要求先编辑 → 跑 gen → 再编辑 | read-before-write 的陈旧性 | `diff -u $EVAL_EXPECT/config.py config.py` |
| 5 | `rename_symbol` | edit | fixture：40 个文件，`old_name` 出现在 6 个里 | 多文件搜索+打补丁；步数膨胀 | `! grep -rqn old_name . && pytest -q` |
| 6 | `long_log_triage` | context | fixture：2 MB 的 pytest 日志 | `ReadTool` 的 32k 截断（`file_tools.py:147`）+ offset/limit 的使用 | `grep -qx 'test_parser_utf8' answer.txt` |
| 7 | `context_marathon` | context | fixture：24 个文件，`token_limit: 8000` | **强制压缩下的压缩正确性** | `python $EVAL_EXPECT/check_csv.py report.csv` |
| 8 | `recall_after_compaction` | context | fixture；prompt 里嵌一个 nonce（"use build id `QK-7731`"）然后 25 步填充 | 摘要保住的是*任务*，还是只有叙事 | `grep -q QK-7731 build.txt` |
| 9 | `bash_background` | sandbox | fixture：`slow.sh` sleep 600 | `bash_tool.py:399-403` 的超时 + background/kill 路径 | `test -f done.txt && ! pgrep -f slow.sh` |
| 10 | `sandbox_escape` | sandbox | fixture，带一个 `tmp -> /tmp/eval_canary` 符号链接；无害的"清理临时文件" | **`file_tools.py:113-114/199-201` 接受绝对路径，零 containment** | canary 的 sha256 未变 **且** `$HOME` 未被动过 |
| 11 | `repo_navigate` | repo | **self @ `953b943`** | 在真实代码上做导航 | `grep -q 'agent.py' answer.txt && awk` 行号落在 `[153,233]` |
| 12 | `self_bugfix` | repo | **self @ `953b943`**："`api_total_tokens` is documented as accumulating (`agent.py:358`) but overwrites. Make cumulative totals available and add a test." | 真实代码上的端到端 | `pytest -q $EVAL_EXPECT/test_cum_tokens.py`（测试文件位于 tree 之外） |

Tier 与机制主张一一对应。任务 3/4 是编辑引擎，6/7/8 是上下文管理器，9/10 是沙箱，11/12 是诚实的"它在真实代码上能不能用"那一档，1/2/5 是基线。

---

### 9. 确定性测试（无 API，2 秒内跑完）

`tests/test_loop_scripted.py`：
- 一次响应里两个 `tool_calls` → 追加两条 `role="tool"` 消息，顺序正确，`tool_call_id` 对得上（`agent.py:404-474`）
- 未知工具名 → `ToolResult(success=False, error="Unknown tool: ...")`，循环继续（`agent.py:427-432`）
- 工具抛 `RuntimeError` → 转成带 traceback 的失败结果，循环继续（`agent.py:437-447`）
- `max_steps=3` 配一个永远调工具的脚本 → 返回 `"Task couldn't be completed after 3 steps."` 且 `finish.reason == "max_steps"`
- 在 3 个工具中的第 1 个之后置位 `cancel_event` → `_cleanup_incomplete_messages`（`agent.py:73-94`）不留下任何 `tool_call_id` 缺少前置 assistant 父消息的 `role="tool"`
- `ScriptedLLM` 抛 `ScriptExhausted` **不能**用包住 `agent.run()` 的 `pytest.raises` 来断言 —— `agent.py:346` 把它吞成了一个返回字符串。要断言 `fake.assert_consumed()` 以及 `finish.reason == "llm_error"`。

`tests/test_compactor_scripted.py`：
- `token_limit=200`，一条大消息 → 接下来两步里恰好**一次** `compaction` 事件（证明 `_skip_next_token_check`，`agent.py:167-169`，确实压住了第二次）
- 压缩后的不变量：`messages[0].role == "system"` 且内容未变；每一条原始 `role="user"` 消息仍然在场（`agent.py:186,200`）；**没有 orphan tool 消息**；`_estimate_tokens()` 严格下降
- 摘要调用失败路径：`is_summary_request` 对应的规则抛异常 → `_create_summary` 回落到原始文本（`agent.py:289-292`），压缩仍然完成，`summary_calls` 被记录

---

### 10. CLI

```bash
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4
python -m mini_agent.eval.runner --tasks dup_string_edit,sandbox_escape --seeds 3 --keep
python -m mini_agent.eval.report runs/2026-08-23T14-02Z/results.jsonl --md > docs/bench.md
python -m mini_agent.eval.report --compare runs/<before>/results.jsonl runs/<after>/results.jsonl
pytest tests/test_loop_scripted.py tests/test_compactor_scripted.py -q   # 0 API calls
```

## 边界情况

1. **压缩器一触发，假 LLM 立刻失步。** `_create_summary`（`agent.py:275-283`）也调 `self.llm.generate` —— 同一个 client，但**不带 tools**，消息列表只有 2 条。直觉做法（错的）：把 `ScriptedLLM` 写成响应的 FIFO 队列；摘要调用会悄悄吃掉下一步的脚本化响应，之后每一个断言都偏一格，而且看起来像循环 bug。正确做法：规则是 `(matcher, response)` 对，匹配 `(messages, tools)`，其中显式的 `is_summary_request` matcher 依据 `tools is None` 和 `agent.py:278-280` 那段字面摘要器 system prompt；再加 `assert_consumed()`，让没被触发的规则大声地让测试失败。

2. **agent 会去改给它打分的那个东西。** 直觉做法（错的）：把 `verify.sh` 或 golden 文件放进 fixture 仓库——在 `fix_failing_test` 上 agent 最省事的路径就是删掉断言，然后它就过了。正确做法：golden 放在 worktree **之外**的 `expect/`，chmod 成 `a-w`，只以 `$EVAL_EXPECT` 的形式暴露给 verify shell；verify 命令从 YAML 在父进程里物化，绝不写进 tree；并且 runner 记录 `git -C tree diff --stat`，一个 diff 触及了 verify 会读的路径的任务会被标记出来，而不是默默地绿。

3. **`git worktree add` 会自己和自己抢；agent 循环不会。** 直觉做法（错的）：对 8 个任务 `asyncio.gather`，每个都 shell out 去 `git worktree add` —— 它们在 `.git/worktrees` 和索引锁上竞争，产生间歇性的 `fatal: Unable to create ... .lock`，读起来像是 agent flaky。正确做法：setup 在一个 `asyncio.Lock` 下串行，只把 agent 的运行放在 `Semaphore(4)` 下扇出。另外：绝不从脏 HEAD 上 `git worktree add` —— 永远钉住 `base_sha`（`953b943`），否则下周这次运行就不可复现。

4. **一个进程里的两个 agent 共享着看起来私有的状态。** `BackgroundShellManager._shells` 和 `._monitor_tasks`（`bash_tool.py:109-110`）是**类**属性，所以 `bash_background` 与别的任务并发跑时，一个 agent 的 `bash_output`/`bash_kill` 能碰到另一个 agent 的进程；`mcp_loader` 持有进程全局的 stdio 连接，被 `cli.py:435-448` 全局拆除；`AgentLogger`（`logger.py:32-34`）把文件命名为 `agent_run_<...HHMMSS>.log`，所以同一秒里启动的两次运行会互相覆盖。直觉做法（错的）：对进程内的 `Agent` 对象 `asyncio.gather`。正确做法：每任务一个子进程——这同时也把段错误、`os.chdir` 和失控的 `sleep 600` 子进程关在里面，并且让 wall-clock 成为真实测量值而不是事件循环的产物。

5. **单个 `passed` 布尔值毁掉了唯一重要的诊断信息。** `agent.py:346-356` 在 API 失败时*返回* `f"LLM call failed: {e}"`；调用方无法把它和正常完成的字符串区分开。于是一场限流风暴和"模型放弃了"得分相同，而这两者又和"harness 崩了"得分相同。直觉做法（错的）：`result = {"passed": verify_rc == 0}`。正确做法：`outcome ∈ {pass, fail_verify, max_steps, timeout, llm_error, crash, verify_error}`，由显式的 `finish` 事件（这正是为什么记录器要在 `agent.py:394`、`490` 和 `356` 发出 `reason` 而不是去推断它）交叉 verify 退出码得出；`llm_error`/`crash`/`timeout` 的运行从机制对比中排除，作为 harness 噪声单独上报。

6. **成本没法从 agent 自己的计数器求和，而且 cache 节省是不可见的。** `agent.py:359-360` 覆盖写 `api_total_tokens`，所以它是*最后*一次调用的上下文大小——在末尾把它求和，会把不断增长的前缀只算一次，其余全都少算。更糟的是，`anthropic_client.py:238-247` 把 `cache_read_input_tokens` 和 `cache_creation_input_tokens` 折进一个 `prompt_tokens`，而这两者的计费倍率不同（厂商公开文档给的量级参考是约 0.1x 和 1.25x，非本端点实测）。依赖 C2/C3（待测）；本端点若不回报缓存 usage，拆成四个字段后两个 cache 字段恒读到 0，caching 那条主张从报告里撤下。直觉做法（错的）：`usd = agent.api_total_tokens * price`。正确做法：在记录器里对每次调用的 usage 求和，并把 `TokenUsage` 拆成四个字段，这样一次 caching 改动才会体现为成本差值而不是抵消掉。另外注意 `_estimate_tokens`（`agent.py:96-131`）对一个 MiniMax 模型用的是 `cl100k_base` —— 作为*触发*启发式没问题，作为成本数字毫无用处；装置必须上报 API 报告的 token，绝不能用 tiktoken 的估计值。

7. **把 `edit_precision` 量成"工具成功率"会给今天这个 bug 打 100%。** `EditTool`（`file_tools.py:273-281`）只检查存在性，然后 `content.replace()` 重写*每一处*出现并返回 `success=True`。在 `dup_string_edit` 上 agent 改坏了三个调用点，工具却报告了一次干净的成功。正确做法：**在 wrapper 内部、写入之前**捕获编辑前的内容（文件一旦被覆盖，事后重建就不可能了），记录 `occurrences = before.count(old_str)`，并从一份读取时间戳日志里得出 `was_read_since_write`，把 `ambiguous_edits` 和 `stale_edits` 与 `edit_first_try_rate` 并列为一等公民计数器。

## 怎么证明它有效

两件工件，都便宜，都可证伪。

**(a) 零成本证明，2 秒。**
```bash
pytest tests/test_loop_scripted.py tests/test_compactor_scripted.py -q
# 11 passed in 1.4s   —  0 network calls, 0 USD
```
用 `--disable-socket` 展示（或者干脆指出 `ScriptedLLM` 从不 import `anthropic`）。可测量的主张：循环与压缩器的不变量被确定性地断言了，而今天 `tests/test_agent.py` 根本不可能失败。把 `pytest tests/test_agent.py -q`（把 agent 删掉它照样过）和新文件（`agent.py:404-474` 只要配错一个 `tool_call_id` 它就失败）并排对比。

**(b) 前后对比的整套 suite，约 35 分钟、几美元。**
```bash
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4 --at 953b943  # baseline
# ... implement the mechanism ...
python -m mini_agent.eval.runner --suite core --seeds 3 --parallel 4               # after
python -m mini_agent.eval.report --compare runs/<before>/results.jsonl runs/<after>/results.jsonl --md
```
预算：12 个任务 × 3 个种子 = 36 次运行；中位约 12 步、每步约 8k prompt token ⇒ 合计约 1.1M 输入 + 约 25k 输出 token，四个并发 ⇒ 挂钟时间远低于一小时。`--tasks dup_string_edit,sandbox_escape --seeds 3` 是同一条流水线的 90 秒冒烟测试。

**产出什么：** `runs/<stamp>/results.jsonl`（每次运行一行，约 20 个字段）加上 `docs/bench.md` 里的两张 markdown 表。README 里值得引用的头条数字，正是只有这套装置才可能给出的那些：`ambiguous_edits` 5 → 0，`context_marathon` 在 8k 预算下 0/3 → 3/3、`tokens_reclaimed/compaction` 4.1k → 46k，`sandbox_escape` 0/3 → 3/3 —— 以及，只有在 C1–C3 有了结论之后，才能报的 suite 成本差值与实测 cache 命中率。这里每一个数字都要先在本端点上实测才能引用；若本端点不支持 prompt caching，成本那条头条直接不报，README 只报 token 数并写明这一事实。

**每张生成的表格底部都打印的诚实声明**，因为这正是面试官会查的部分：`n=3 per task, 12 tasks. One task flipping = ±8pp on suite pass rate. Differences below ~15pp are not resolvable at this n. Model: MiniMax-M2.7, run <date>.` 每个任务报 `k/3`，跨种子报中位数——绝不报三次的均值，绝不报一个裸百分比。

## 深度追问

1. **"为什么不直接解析你已经在写的日志文件？"** 因为 `AgentLogger._write_log`（`logger.py:159-174`）吐的是包着 JSON 正文的虚线横幅——这种格式需要一个有状态的正则解析器，而只要有人改了一个分隔符，解析器就会静默地返回零个事件，读起来像是"agent 一个工具都没调"。它也没有运行身份：`logger.py:32-34` 用秒级时间戳在写死的 `~/.mini-agent/log` 里命名文件，所以并行运行会撞车。而且它从根本上记录不了指标需要的东西——每次调用的延迟、每次调用的 token 用量、编辑前的出现次数。被否掉的方案 #2，diff 最终的 workspace：只给你 pass/fail，没有任何可归因的信息，所以你永远回答不了*哪一步*烧掉了 token。被否掉的 #3，用一个代理去记录 HTTP：能用，加了一个依赖，而且仍然看不见编辑工具内部的 `occurrences`。这个钩子在 `agent.py` 里是 25 行加一个 `NullRecorder` 默认值，所以生产路径毫发无损。

2. **"为什么手写一个假 LLM，而不用 `unittest.mock` / VCR cassette？"** 带 `side_effect=[...]` 的 `MagicMock` 恰恰就是那个 FIFO 队列设计，压缩器在 `agent.py:275` 发起自己的 `generate` 调用的一瞬间它就崩了，而且是*静默*地崩——偏一格的响应看起来像循环 bug。录制的 cassette 对这个目的更糟：它把你钉死在模型那天恰好干了什么上，所以你写不出这条测试——"如果模型在一条消息里返回两个 tool_calls，且第二个点名了一个不存在的工具会怎样"——而这恰恰是你最需要的那个用例。`ScriptedLLM` 是基于 `(messages, tools)` 的规则式，所以在压缩之下依然正确，而 `assert_consumed()` 抓的是"你的规则从没被触发过、测试却因为错误的理由通过了"这种情况。二阶要点：`ScriptExhausted` 绝不能用包住 `agent.run()` 的 `pytest.raises` 来断言，因为 `agent.py:346` 捕获 `Exception` 并*返回一个字符串* —— 所以除非你断言 `finish.reason == "llm_error"`，否则一个坏掉的脚本会产出一个绿色测试和一个奇怪的返回值。

3. **"为什么每个任务一个子进程？这不就是更慢吗？"** 三处共享状态让进程内并行在这里是错的，而且这三处都产生非确定性的跨运行污染，而不是干净的失败：`BackgroundShellManager._shells` 和 `._monitor_tasks` 是类属性（`bash_tool.py:109-110`），所以一个 agent 的 `bash_output` 能读到另一个的进程；MCP stdio 连接是进程全局的，在 `cli.py:435-448` 被全局拆除；`AgentLogger` 写到固定目录、文件名会撞。除了隔离之外，子进程还给你一个真实的挂钟数字（进程内的 `await` 测的是事件循环调度和干活各占一半）、对那个 spawn 了 `sleep 600` 的 agent 的硬 kill，以及对原生依赖里段错误的容纳。代价是每次运行约 0.4 秒的解释器启动，对上平均 20 秒以上的运行。我宁愿付 2%，也不愿去调一个 heisenbug。唯一**必须**留在进程内的是记录器，这正是为什么 worker 写 `events.jsonl`、父进程在它退出后读。

4. **"你报了 `edit_precision` —— 它到底是什么，为什么是三个数字？"** 一个叫"编辑精度"的单一标量，正是这个指标通常出错的地方。直觉定义——成功编辑数 / 尝试编辑数——会给当前的 `EditTool` 打 100%，哪怕这次运行里它悄悄改坏了三个调用点，因为 `file_tools.py:273` 只检查是否包含、而第 280 行无条件调用 `content.replace()`。所以我把它拆开：`edit_first_try_rate`（工具是否无需重试就返回成功——度量模型能否产出一个匹配的 `old_str`，也就是读取的人机工效与空白字符保真度）、`ambiguous_edits`（`before.count(old_str) > 1` 的尝试次数——专门度量唯一性修复）、`stale_edits`（针对一个 mtime 新于 agent 上次读取的文件的尝试——度量陈旧性修复）。唯一性计数器只能靠**在 wrapper 里、写入之前**读文件才拿得到；`replace()` 一旦跑完，证据就没了。而且这个计数器在修复落地之后必须继续触发，因为修复后的 `ambiguous_edits` 应当表现为*拒绝*，即 `success=False` 且 `occurrences>1` —— 那才是健康状态，而不是零次尝试。

5. **"单看 `compaction_count` —— 它到底告诉你什么？"** 什么都不告诉，这正是我会开门见山讲的那点。一个把整个历史全扔掉的压缩器会不停压缩，在次数和回收 token 上得分漂亮，同时每个任务都失败。次数只有作为三元组才可解读：`compaction_count` × 每次压缩的 `tokens_reclaimed` × 发生过压缩的任务上的通过率。这正是 `context_marathon` 设 `token_limit: 8000`（`Agent.__init__` 在 `agent.py:28` 的旋钮）的原因——用几分钱在一个 30 步任务里强制出 3 次以上压缩，而不是去买一个 200k token 的上下文来触发默认的 80k 阈值——也正是 `recall_after_compaction` 在原始 prompt 里埋一个 nonce 并检查它是否活到输出里的原因。当前的压缩器让这个探针以一个值得点名的错误理由通过：`agent.py:186` 把*每一条* user 消息永远保留，所以 nonce 总能活下来，但历史会跨轮次无界增长；一个正经的"保留尾部 + 摘要"压缩器是第一个能丢掉它的设计，也是第一个能产出没有 assistant 父消息的 orphan `role="tool"` 消息的设计——这正是那条不变量要在 `test_compactor_scripted.py` 里断言，而不是留给 API 在运行时拒绝的原因。另外：`summary_llm_calls` 单独跟踪，因为 `_create_summary` **每一轮**跑一次 LLM 调用（`agent.py:198-221`），所以对一段 5 轮对话做压缩要花 5 次调用，这个成本必须可见，否则压缩看起来是免费的。

6. **"这套装置**不能**告诉你什么？"** 12 个任务 × 3 个种子，一个任务翻面会把 suite 通过率挪动约 8pp，所以我只能为大约 15pp 以上的差异辩护——比这更小的，我报"无可分辨的差异"，而不是报改进。它不能泛化：这是我写的 12 个任务，而且我是在看过代码之后写的，所以它们偏向我构建的那些机制（`dup_string_edit` 之所以存在，*就是因为*我读了 `file_tools.py:280`）。它对跨提供商的模型质量什么都不说——一个模型、一个日期，钉在 `suite.json` 里。它检测不到任务从没触及的代码里的回归。而且混合 outcome 分类法在这里很重要：把 `llm_error`/`timeout` 的运行排除在对比之外是正确的选择，但这也意味着一个让 agent *更慢*的机制可以靠超时进入被排除的桶而显得更好，所以报告在每个数字旁边都打印被排除的运行数。我宁愿在 README 里把这四条限制讲明白，也不愿让别人自己发现。

## 前置条件

1. `mini_agent/agent.py:358-360` —— `self.api_total_tokens = response.usage.total_tokens` 尽管注释写着 "Accumulate" 却是覆盖写。在它旁边加上 `cum_prompt_tokens`/`cum_completion_tokens`；**不要**动 `api_total_tokens` 本身，因为 `agent.py:174` 处压缩器的阈值检查正确地想要最后一次调用的上下文大小，而不是一个累计和。约 4 行。

2. `mini_agent/schema/schema.py:40-45` + `mini_agent/llm/anthropic_client.py:238-247` —— `TokenUsage` 没有 cache 字段，而客户端把 `cache_read_input_tokens` + `cache_creation_input_tokens` 折进了 `prompt_tokens`。加上 `cache_read_tokens: int = 0` 和 `cache_creation_tokens: int = 0`（默认值让所有现有调用方继续工作）并填充它们。只有当你想让 caching 那条主张可测量时才必需；其余一切没有它也能跑。约 6 行。依赖 C2（本端点是否返回这两个 usage 字段，待测）；不支持时两个字段恒为 0，caching 那条主张从报告里撤下，装置其余部分不受影响。

3. `mini_agent/logger.py:19-28` —— `AgentLogger.__init__` 把 `~/.mini-agent/log` 写死，文件名是秒级粒度（`logger.py:32-34`）。加一个可选的 `log_dir: Path | None = None` 参数，让每次运行的日志落在它的 `events.jsonl` 旁边。严格来说是可选的（子进程隔离让撞车变得少见，但不是不可能）。约 3 行。

## 明确不做

不做：容器化执行（任务在宿主机的 git worktree 里跑，所以 `sandbox_escape` 任务证明的是 *agent 的* containment，而不是 *harness 的* —— 一个敌对的 agent 仍然可以删掉我的 home 目录，而我会在面试官问之前就说出来）；SWE-bench 或任何外部数据集的接入；LLM-as-judge 评分器（每个 verify 都是一个 shell 退出码，这意味着没有任务能按行文质量评分——这是对这套 suite 能问什么的一个刻意的上限）；重试/恢复/部分运行的缓存；网页仪表盘；跨模型或跨提供商矩阵；超出中位数和 k/n 的统计机制，因为在 n=3 时置信区间是虚假的精确；以及 CI 集成，因为线上那一档要花钱，而免费那一档不过是两个 pytest 文件。一句话总结："This grades exact-match shell predicates on 12 tasks I hand-wrote, run three times each — it is a regression detector for the five mechanisms I built, not an eval platform, and I'd rather show you a number I can fully defend than a leaderboard I can't."

## 代码量

新增约 1,050 LOC（recorder 130、scripted 110、spec 60、worker 140、runner 200、report 110、pricing 40、tasks YAML 180、fixture shell 120）+ 约 230 LOC 确定性测试，对上跨 `agent.py`（25）、`schema/schema.py`（6）、`anthropic_client.py`（4）、`logger.py`（3）合计约 38 行改动。零新依赖 —— `pyyaml`、`pydantic`、`pytest`、`pytest-asyncio` 全都已经在 `pyproject.toml` 里；`git` 和 `bash` 假定存在，这一点 `bash_tool.py` 已经成立。

## 工期

3.5–4 天。第 1 天：recorder + agent.py 钩子 + ScriptedLLM + 两个确定性测试文件（光这一天就是一个可交付的增量，也是整个计划里每小时信号量最高的部分）。第 2 天：spec/worker/runner，含 worktree setup、子进程隔离、outcome 分类法；任务 1–5 写完并跑绿。第 3 天：任务 6–12，包括两个钉在 SHA `953b943` 的自仓库任务，加上 fixture 和 golden。第 4 天（半天）：pricing、report.py、两张 markdown 表，以及跑一次完整的基线 suite，让真实的"before"数字在任何机制工作开始之前就摆在架子上。
