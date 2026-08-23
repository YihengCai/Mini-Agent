# 子 agent 与上下文隔离

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Sub-agent delegation with context isolation (`task`/explore tool)`


## 一句话

一个 `task` 工具，跑起一个完整的子 `Agent`：它有自己的消息列表、一个只读工具子集、三条正交预算（步数 / 输入 token / 墙钟时间），以及一份强制的结构化 `submit_report` 返回契约——于是不管子 agent 读了多少内容，一次任意大的探索对父上下文的开销都被限死在约 1 KTok。

## 为什么这是难点

每个 coding agent 都会撞上同一堵墙：回答"X 在哪里处理"最省事的办法就是读大量字节，而读进来的每一个字节都会永久落在同一个上下文窗口里——那个窗口还得装计划和编辑。这个仓库把这件事变得很具体。`ReadTool` 在 32 000 token 处截断（`mini_agent/tools/file_tools.py:147`），而 `BashTool` 的输出根本不截断——`BashOutputResult.format_content`（`mini_agent/tools/bash_tool.py:32-48`）直接拼接原始 stdout，没有上限。所以两次大读取，或者一次在仓库上跑的 `grep -rn`，就能把父上下文推过 `token_limit=80000`（`mini_agent/agent.py:28`），引爆 `_summarize_messages`（`mini_agent/agent.py:153-233`）——它会不可逆地把整段执行历史换成 LLM 写的散文。探索是单项最大的上下文消费者，而且它几乎完全是*可丢弃的*：为回答一个问题读的 40 KB 里，真正有用的可能就 300 字节。

委派是对这个问题的预防式回答，压缩则是被动式回答。它也是唯一一个属于*硬边界*而非启发式的上下文机制：有了结构化返回契约，父上下文每次委派的增长量是由构造保证的上限，而不是寄希望于摘要器表现良好。把它做出来，会逼你直面四件让它从装饰变成真机制的事——子 agent 如何终止，如何约束三种正交资源，返回载荷里必须有什么才能让人不回头翻源文件也敢信，以及哪些共享可变状态（file ledger、shell registry、审批 session）子 agent 可以碰、哪些不能。

## 仓库现状

现在没有任何形式的委派。`Agent`（`mini_agent/agent.py:18-57`）是一个扁平的单循环：一个 `self.messages` 列表，用 system prompt 起头（`agent.py:47`）；一个 `self.tools` 字典（`agent.py:31`）；一个 `self.max_steps`（`agent.py:32`）；一个 `AgentLogger`（`agent.py:52`）。`run()`（`agent.py:294-492`）在 `for tool_call in response.tool_calls:`（`agent.py:404`）里严格顺序执行工具调用，内联 await 每个 `tool.execute(**arguments)`（`agent.py:436`）。除了 `acp/__init__.py:102`——它为每个 ACP session 建一个，不是嵌套——任何地方都没有构造第二个 `Agent`。

唯一的上下文防线是 `_summarize_messages`（`agent.py:153-233`）：它保留 system prompt 和每条 user 消息，把每个 user→user 区间换成一段 LLM 生成的散文块，以 `role="user"` 注入、前缀 `[Assistant Execution Summary]`（`agent.py:213-217`）。它有损、无结构，而且只在损害*发生之后*才触发（在 `agent.py:331` 每步检查）。

这份规格必须绕开的具体缺口：
- **没有 FileLedger，完全没有权限/审批系统。** 在 `mini_agent/` 上 `grep -rn "ledger"` 什么也搜不到。`EditTool.description` 宣称 "You must read the file first before editing"（`file_tools.py:230`），但 `EditTool.execute`（`file_tools.py:254-285`）什么都不强制——它只是 `read_text` / `replace` / `write_text`。所以"子 agent 是否共享 ledger"是一个关于尚不存在的接缝的设计问题；这份规格把位置预留出来，而不是假装它已经在。
- **token 记账是赋值，不是累加。** `agent.py:359-360` 写的是 `self.api_total_tokens = response.usage.total_tokens`，上面的注释却写着 "Accumulate"。它报告的是*最后一次*调用的总量，因此没法用来测树的开销。基准测试需要真正的累加器。
- **日志文件名会撞。** `AgentLogger.start_new_run`（`logger.py:30-41`）拼出 `agent_run_{YYYYmmdd_HHMMSS}.log` 并以 `"w"` 模式打开。同一墙钟秒内起的子 agent 会静默截断父 agent 的日志。
- **`BackgroundShellManager._shells` 是类属性**（`bash_tool.py:108-110`），即进程全局，父子共享。
- **`run()` 把整份 transcript 打到 stdout**（步骤框 `agent.py:334-336`、工具参数 `agent.py:405-421`、结果 `agent.py:462-468`），没有 verbosity 开关。
- `run()` 里任何地方都没有 per-step hook，所以预算强制需要新加一个。

## 最小实现

## 新包：`mini_agent/subagent/`

### 1. `report.py` —— 返回契约（约 90 LOC）

```python
from typing import Literal
from pydantic import BaseModel, Field

class Evidence(BaseModel):
    path: str                      # workspace-relative
    lines: str | None = None       # "120-160" or "147"
    quote: str | None = Field(None, max_length=200)  # verbatim

class Answer(BaseModel):
    question: str                  # must echo the parent's question verbatim
    answer: str = Field(max_length=600)
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(min_length=1, max_length=4)

class SubagentReport(BaseModel):
    status: Literal["complete", "partial", "failed"]
    answers: list[Answer]
    files_examined: list[str] = Field(max_length=30)
    unresolved: list[str] = Field(default_factory=list, max_length=5)
    next_actions: list[str] = Field(default_factory=list, max_length=5)

def verify_evidence(rep: SubagentReport, workspace: Path) -> SubagentReport
def render_report(rep: SubagentReport, meta: RunMeta, cap_tokens: int = 900) -> str
```

`verify_evidence` **在服务端运行，在报告到达父 agent 之前**：对每个 `Evidence`，在 workspace 下解析 `path`；缺失 → 改写成 `path + " [PATH NOT FOUND]"`；如果设了 `quote`，就读取被引用的行区间，检查引文是否逐字出现（空白归一化后）→ 否则打上 `[QUOTE UNVERIFIED]`。任何一条 `Answer` 若无任何存活的已验证证据，就被降级为 `confidence="low"`。正是这一步把契约从一种格式约定变成一种被检查的契约。

`render_report` 输出确定性的紧凑 markdown，并用一条固定的降级阶梯执行 token 上限（用 `tiktoken.get_encoding("cl100k_base")` 计量，与 `agent.py:101` 同一个编码器）：丢 `next_actions` → 丢 `unresolved` → 每个 answer 只保留 1 条证据 → 把每条 `answer` 截到 300 字符 → 追加 `[report truncated to fit parent context]`。确定性的，绝不调用 LLM。

### 2. `budget.py`（约 60 LOC）

```python
@dataclass
class Budget:
    max_steps: int = 12
    max_input_tokens: int = 60_000
    timeout_s: float = 180.0
    warn_at: float = 0.75
```
三条预算，因为有三种正交资源：一次 `read_file` 可以在单步内加进 32 000 token（`file_tools.py:147`），而一次 `find /` 可以挂好几分钟，既不花步数也不花 token。

### 3. `readonly_bash.py`（约 110 LOC）

`class ReadOnlyBashTool(BashTool)` —— 覆写 `name → "bash_readonly"`、一段说明只读的描述，以及 `execute()`：把 `timeout` 钳到 ≤30 s，强制 `run_in_background=False`，先跑 `_reject(command)`，再 `super().execute(...)`；另外通过 `truncate_text_by_tokens`（`file_tools.py:11`）把返回的 `content` 截到 8 000 token，因为基类工具根本不截断。

`_reject(cmd) -> str | None`（返回原因，或 None）：
1. 原始字符串中含 `>`、`>>`、`` ` ``、`$(`、`tee` 就拒绝。
2. 按 `;`、`&&`、`||`、`|` 切分，检查**每一个**片段。
3. 每个片段的第一个 token 必须属于 `{ls, cat, head, tail, sed, grep, rg, find, wc, file, tree, awk, sort, uniq, cut, stat, du, basename, dirname, git, diff, nl, xargs? no}` —— `xargs`、`python*`、`perl`、`node`、`sh`、`bash`、`env` 一律拒绝。
4. 逐命令的 flag 规则：`sed` 不得带 `-i`/`--in-place`；`find` 不得带 `-delete`、`-exec`、`-execdir`、`-fprint`、`-fls`；`git` 子命令必须在 `{status, log, diff, show, ls-files, blame, rev-parse, cat-file, grep}` 之中。

在 docstring 里明说：**这是防糊涂模型的护栏，不是防敌意模型的 sandbox。** 真正的隔离是容器/seccomp 边界；那部分是被砍掉的范围。

### 4. `task_tool.py`（约 340 LOC）

```python
_MUTATING = {"write_file", "edit_file", "bash", "bash_output", "bash_kill", "record_note", "task"}
_CHILD_ALLOW = {"read_file", "get_skill", "recall_notes"}

def build_child_tools(parent_tools: dict[str, Tool], workspace: Path,
                      cfg: SubagentConfig) -> list[Tool]:
    out = [t for n, t in parent_tools.items() if n in _CHILD_ALLOW]   # shared by reference
    out.append(ReadOnlyBashTool(workspace_dir=str(workspace)))
    out += [parent_tools[n] for n in cfg.mcp_allow if n in parent_tools]  # explicit allowlist
    assert not ({t.name for t in out} & _MUTATING)
    return out
```
MCP 工具**默认拒绝**：MCP 工具 schema 不带副作用标注，所以从协议格式上无法判断它是不是只读的。要用就在配置里按名字显式开启。

```python
class _ReportBox:
    report: SubagentReport | None = None

class SubmitReportTool(Tool):          # name = "submit_report"
    def __init__(self, box: _ReportBox, questions: list[str], workspace: Path)
    async def execute(self, **kwargs) -> ToolResult
```
`execute` 用 `SubagentReport(**kwargs)` 校验；遇到 `ValidationError` 返回 `success=False` 并带上 pydantic 的报错信息，让子 agent 在预算内有一次修复机会。它还会检查 `[a.question for a in rep.answers] == self.questions`（精确、有序），不匹配就拒绝。成功时执行 `verify_evidence`，存入 `box`，并返回 `"Report accepted. Stop now — do not produce further output."`

```python
class TaskTool(Tool):
    name = "task"
    def __init__(self, llm_client, workspace_dir: Path, cfg: SubagentConfig, depth: int = 0)
    def bind_parent(self, agent: "Agent") -> None
    def reset_turn(self) -> None            # resets self._spawns_this_turn = 0
    async def execute(self, description: str, prompt: str,
                      questions: list[str], subagent_type: str = "explore") -> ToolResult
```

**工具 schema**（`parameters`）：
- `description` —— 字符串，3–8 个词，仅用于终端那一行。
- `prompt` —— 字符串，必填。描述文本：*"The complete, self-contained instruction. The subagent has NO access to this conversation, the files you have read, or anything you know. Anything it needs must be written here."*
- `questions` —— 1–5 个字符串的数组，必填。*"The exact questions you need answered. The subagent's report answers these, in order, and nothing else."*
- `subagent_type` —— 枚举 `["explore"]`，默认 `"explore"`。

`execute()` 主体，按顺序：
1. 守卫：`depth > 0` → 报错（子 agent 永远拿不到 `task`，这是双保险）；`self._spawns_this_turn >= cfg.max_spawns_per_turn (8)` → 报错。
2. `budget = Budget(**cfg.budget)`；`run_id = uuid4().hex[:6]`。
3. `box = _ReportBox()`；`tools = build_child_tools(self.parent.tools, ws, cfg) + [SubmitReportTool(box, questions, ws)]`。
4. 构建子 agent 的 system prompt（见下），然后：
   ```python
   child = Agent(llm_client=self.llm, system_prompt=sys_prompt, tools=tools,
                 max_steps=budget.max_steps, workspace_dir=str(ws),
                 token_limit=int(budget.max_input_tokens * 0.9))
   child.logger.start_new_run(tag=f"_sub_{run_id}")
   child.step_hook = make_budget_hook(box, budget, deadline)
   child.add_user_message(prompt + "\n\nQuestions:\n" + numbered(questions))
   ```
5. 接好取消线路（见下），然后在 `contextlib.redirect_stdout(io.StringIO())` 里 `await child.run(cancel_event=child_cancel)`。把捕获的文本倒进子 agent 的日志文件。
6. 整形结果并返回。
7. `finally:` 取消 linker 与 watchdog 任务，并终止子 agent 运行期间新出现的所有 `BackgroundShellManager` id（通过 `BackgroundShellManager.get_available_ids()`（`bash_tool.py:120-123`）在运行前后各取一次 id 集合做差）—— 绝不"全杀"，这个 registry 是与父 agent 共享的。

**`make_budget_hook`**（真正的强制点，在每个子 agent 步骤的 LLM 调用前调用一次）：
```python
def hook(agent, step) -> str | None:
    if box.report is not None: return "STOP"
    used_t = agent.tokens_in
    if step >= budget.max_steps - 1 or used_t >= budget.max_input_tokens or now() > deadline:
        agent.tools = {"submit_report": agent.tools["submit_report"]}   # poor-man's tool_choice
        agent.max_steps = step + 2
        return "BUDGET EXHAUSTED. All other tools have been removed. Call submit_report NOW with status=\"partial\" and whatever you have."
    if step >= budget.max_steps * warn_at or used_t >= budget.max_input_tokens * warn_at:
        return f"Budget warning: {budget.max_steps - step} steps and ~{budget.max_input_tokens - used_t} tokens remain. Wrap up and call submit_report."
    return None
```
工具集是**就地**缩小的，因为两个客户端都不设 `tool_choice` —— `AnthropicClient._make_api_request` 只设 `model/max_tokens/messages/system/tools`（`llm/anthropic_client.py:68-79`）。移除其余所有工具是手头最接近的强制手段，而且不需要改客户端。

**`execute()` 里的结果整形：**
| 情形 | 返回给父 agent 的内容 |
|---|---|
| `box.report` 已设置 | `ToolResult(success=True, content=render_report(...))`，≤900 token |
| 预算耗尽，报告 `status="partial"` | 同上，`success=True` —— 带证据的部分答案是有用的；标记为失败会让父 agent 重做那件昂贵的事 |
| 抢救轮什么都没产出 | 机械合成 `SubagentReport(status="failed", answers=[Answer(q, "no answer produced", "low", [...])], files_examined=<paths harvested from the child's `read_file` tool-call arguments>)` —— 仍然 `success=True`，这样父 agent 至少知道*看过哪些东西* |
| LLM 报错 / `RetryExhaustedError` | `ToolResult(success=False, error=f"subagent {run_id} failed after {n} steps: {str(e)[:200]} (log: {child.logger.get_log_file_path()})")` —— **一行，绝不带 traceback**；否则 `agent.py:437-452` 的通用处理器会把完整的 `traceback.format_exc()` 注入父上下文 |
| 用户取消 | `success=False, error="subagent cancelled by user"` —— 父 agent 自己在 `agent.py:441-446` 的 `_check_cancelled()` 会在一个工具结果之内停下父 agent |
| 超时 | 若抢救轮成功落地，则 `success=True` 且 `status="partial"`，否则 `success=False, error="subagent {run_id} timed out after {t}s"` |

**取消 / 超时线路** —— 两个事件，单向传播：
```python
child_cancel = asyncio.Event()
linker  = asyncio.create_task(_link(self.parent.cancel_event, child_cancel))  # parent -> child ONLY
watchdog = asyncio.create_task(_fire_after(budget.timeout_s, child_cancel, reason))
result = await asyncio.wait_for(child.run(cancel_event=child_cancel),
                                timeout=budget.timeout_s + 30)   # hard backstop
```
先协作式（watchdog 设置子 agent 的事件，`run()` 在它已有的安全检查点上检查 —— `agent.py:301`、`agent.py:400`、`agent.py:441` —— 于是 `_cleanup_incomplete_messages`（`agent.py:73-93`）能让子 agent 的消息列表保持一致），硬性 `wait_for` 只作为工具卡死时的兜底。

**子 agent system prompt** —— 新文件 `mini_agent/config/subagent_explore_prompt.md`，通过 `Config.find_config_file` 加载。必须包含以下内容（措辞可变，意思照搬）：
- "You are a read-only exploration subagent. You cannot write, edit, or run anything that mutates state."
- "You have no memory of the conversation that spawned you and no way to ask a question. If the request is ambiguous, answer the most likely reading and record the ambiguity in `unresolved`."
- "**Nothing you say outside a `submit_report` tool call reaches the caller.** Prose, apologies, and offers of further help are discarded."
- "Every entry in `answers` must carry at least one `evidence` item with a real path and line range you actually read. Quotes are verified against the file; fabricated quotes are flagged in the report you hand back."
- "Do not paste file contents. Quotes are capped at 200 characters."
- 预算行，插值填入：`"You have {max_steps} steps and ~{max_input_tokens} input tokens."`

字符串 `"Current Workspace"` **不得**出现在这个文件里，这样 `Agent.__init__`（`agent.py:39-43`）才会自动追加 workspace 块。

## 对现有文件的改动（都很小）

1. **`mini_agent/logger.py:30-33`** —— 改成 `def start_new_run(self, tag: str = "")`，文件名 `f"agent_run_{timestamp}{tag}.log"`。修掉 `logger.py:38` 同秒内 `"w"` 模式截断的问题。
2. **`mini_agent/agent.py:54-57`** —— 增加 `self.llm_calls = 0`、`self.tokens_in = 0`、`self.tokens_out = 0`、`self.step_hook: Callable[["Agent", int], str | None] | None = None`、`self.children: list["Agent"] = []`。
3. **`mini_agent/agent.py:358-360`** —— 把赋值换成累加，同时保留 `api_total_tokens` 给现有的 `print_stats`（`cli.py:245`）用：
   ```python
   if response.usage:
       self.api_total_tokens = response.usage.total_tokens
       self.tokens_in += response.usage.prompt_tokens
       self.tokens_out += response.usage.completion_tokens
   self.llm_calls += 1
   ```
4. **`mini_agent/agent.py:330`** —— 在 `await self._summarize_messages()` 之前紧挨着插入 hook：
   ```python
   if self.step_hook is not None:
       directive = self.step_hook(self, step)
       if directive == "STOP":
           break
       if directive:
           self.messages.append(Message(role="user", content=directive))
   ```
   （`break` 会落到 `agent.py:488-491` 的 max-steps 返回；`TaskTool` 读的是 `box.report`，不是 `run()` 返回的字符串，所以无害。）
5. **`mini_agent/agent.py:59-61`** —— 在 `add_user_message` 里，append 之后加：`for t in self.tools.values(): getattr(t, "reset_turn", lambda: None)()`。
6. **`mini_agent/cli.py:544`** —— 在 `add_workspace_tools(tools, config, workspace_dir)` 之后紧接一个新块：
   ```python
   task_tool = None
   if config.tools.enable_subagent:
       task_tool = TaskTool(llm_client, workspace_dir, config.tools.subagent)
       tools.append(task_tool)
   ```
   （`llm_client` 在 `cli.py:526` 已经存在。`add_workspace_tools` 在 `cli.py:399` 的签名刻意不动，好让 `acp/__init__.py:101` 继续工作。）
7. **`mini_agent/cli.py:575`** —— 紧接在 `agent = Agent(...)` 块之后：`if task_tool: task_tool.bind_parent(agent)`。
8. **`mini_agent/config.py:48-63`** —— 新增 `class SubagentConfig(BaseModel)`，含 `max_steps: int = 12`、`max_input_tokens: int = 60000`、`timeout_s: float = 180`、`max_spawns_per_turn: int = 8`、`mcp_allow: list[str] = []`、`prompt_path: str = "subagent_explore_prompt.md"`；给 `ToolsConfig` 加 `enable_subagent: bool = True` 和 `subagent: SubagentConfig`，另外在 `config.py:140` 附近加一行解析。
9. **`mini_agent/config/system_prompt.md`** —— 加一段：对于那些*中间*输出你并不需要的开放式搜索（"X 在哪里"、"Y 怎么工作"、"哪些文件做 Z"）用 `task`；当你需要精确字节来做编辑时自己动手。

## 边界情况

1. **子 agent 以散文结尾，而不是调用 `submit_report`。** 直觉做法（错的）：把子 agent 最后那段 assistant 文本当成结果——那恰好就是你想避开的自由格式返回，而且实践中最后一轮往往是"Let me know if you'd like me to dig deeper!"，对着一个根本不存在的用户说话。正确做法：强制一次抢救轮，把 `agent.tools` 就地替换成 `{"submit_report": ...}`，让模型只剩一个合法动作；如果连这个也失败，就机械合成（`status="failed"`，`files_examined` 从子 agent 自己 `read_file` 调用的 `path` 参数里收集）。抢救轮的代价是一次 LLM 调用，这也是 `max_steps` 设成 `step + 2` 而不是 `step + 1` 的原因。

2. **子 agent 编造引用。** 直觉做法（错的）：相信报告，因为它通过了 pydantic schema 校验——但 schema 只能证明它*长得像*证据。正确做法：`verify_evidence` 解析每个 `path`，读取被引用的行区间，检查 `quote` 是否逐字出现；无法验证的证据打上 `[QUOTE UNVERIFIED]`，其所属答案被强制降级为 `confidence="low"`。这的代价是每条引用一次带缓冲的文件读取，而这正是返回*契约*和返回*约定*之间的区别。它还顺带给了父 agent 一把便宜的下钻钥匙：一个已验证的 `path:lines` 是父 agent 在需要字节时可以直接去读的东西。

3. **只读 bash 靠第一个 token 强制。** 直觉做法（错的）：`cmd.split()[0] in ALLOWLIST`。这会放过 `sed -i 's/x/y/' f`、`find . -name '*.py' -delete`、`find . -exec rm {} \;`、`git checkout .`、`cat a > b`、`python3 -c "open('f','w')"`，以及 `ls; rm -rf build`。正确做法：直接拒绝重定向与命令替换字符，按 `;`/`&&`/`||`/`|` 切分并校验每一段，施加逐命令的 flag 规则（`sed` 不带 `-i`，`find` 不带 `-delete`/`-exec`/`-fprint`，`git` 子命令 allowlist），并彻底禁掉解释器。而且要明说：这是防糊涂模型的护栏，不是防敌意模型的 sandbox。

4. **超时实现成对父 agent cancel event 的 `asyncio.wait_for`。** 错了两层。(a) 共享事件意味着子 agent 超时会连带取消父 agent 的这一轮，因为 `Agent.run` 在 `agent.py:301`/`400`/`441` 检查的是同一个对象。(b) `wait_for` 会在工具执行中途取消协程，于是注册在 `BackgroundShellManager._shells` 里的后台 shell —— 那是 `bash_tool.py:108-110` 的一个*类*属性，进程范围共享 —— 就成了 orphan。正确做法：一个独立的子 `Event`，一个只做父→子传播的 linker 任务，一个在截止时刻设置子事件的 watchdog（让取消落在已有的安全检查点上，保住 `_cleanup_incomplete_messages`，`agent.py:73-93`），一个硬性的 `wait_for(deadline + 30)` 兜底，以及清理时只终止子 agent 启动之后新增的 shell id —— 绝不是全部，否则子 agent 的清理会杀掉父 agent 的开发服务器。

5. **子 agent 继承了父 agent 的 `token_limit=80000`，于是它自己的压缩器触发了。** `_summarize_messages` 在 `agent.py:331` 每步都会被调用，子 agent 里也一样。一旦它在子 agent 内部触发，子 agent 的原始证据就被换成有损散文（`agent.py:213-217`），它随后提交的引用便是对摘要的摘要推导出来的——从外部完全看不出来，因为报告仍然形状良好。正确做法：用 `token_limit = int(0.9 * budget.max_input_tokens)` 来构造子 agent，这样预算的抢救轮总是严格早于压缩器可能触发的时刻。通用规则：两套上下文机制的触发区间不能重叠，否则你说不清一次失败归谁。

6. **用 `redirect_stdout` 让子 agent 闭嘴。** 它今天能用，而且只在今天能用。`contextlib.redirect_stdout` 是进程范围地替换 `sys.stdout`，不是 per-task；它之所以安全，纯粹因为父 agent 的工具循环是顺序的（`agent.py:404` 逐个 await `tool.execute`，`agent.py:436`），所以同一时刻恰好只有一个子 agent 存在。任何人一旦把那个循环包进 `asyncio.gather`，这个重定向就会开始吞掉兄弟 agent 的输出，per-child 的 cancel 链接也会多出第二个写入方。把这条不变量写进注释，并在测试里断言它，而不是等以后才发现。

7. **递归与扇出。** 直觉做法（错的）：把父 agent 的工具列表去掉写工具后原样交给子 agent，忘了 `task` 就在那个列表里——一句"探索得彻底一点"就会分叉出一整棵树，把整个账户的额度烧光。正确做法：`task` 在 `_MUTATING` 里，所以 `build_child_tools` 会把它过滤掉；再加 `execute()` 内部的 `depth > 0` 守卫；再加通过挂进 `add_user_message`（`agent.py:59-61`）的 `reset_turn()` 协议重置的 `max_spawns_per_turn` —— 三道彼此独立的闸门，因为前两道是静态的，第三道是唯一能约束单个失控轮次的那道。

## 怎么证明它有效

两个工件。单元测试证明*契约*成立；基准测试证明契约*划算*。

**(a) `tests/test_subagent_isolation.py` —— 确定性，不走 API，约 5 秒。** 一个实现 `generate(messages, tools) -> LLMResponse` 的 `FakeLLM` 回放一份脚本化的轮次列表。把子 agent 脚本成：`read_file` 一个 50 KB 的 fixture，然后调用 `submit_report`。断言：
1. 整个委派过程中，`len(parent.messages)` 恰好增长 2（assistant 的 tool-call 消息 + tool result）。
2. 那个 50 KB fixture 的标记字符串**不**出现在 `json.dumps([m.model_dump() for m in parent.messages])` 里——这是把上下文隔离表述成一条不变量，而不是一种感觉。
3. tool-result 内容的 `tiktoken` 计数 ≤ 900。
4. `set(child.tools) & _MUTATING == set()` 且 `"task" not in child.tools`。
5. 预算：一个*永不*调用 `submit_report` 的 `FakeLLM` 恰好产生 `max_steps + 1` 次 `generate` 调用（含抢救轮），最后一次调用收到的 `tools` 列表长度为 1 且其唯一成员是 `submit_report`，返回的报告 `status in {"partial","failed"}`。
6. 证据验证：一份脚本化的、引用 `nonexistent.py:1-5` 的报告，回来时被打上 `[PATH NOT FOUND]` 且 `confidence == "low"`。

**(b) `scripts/bench_subagent.py` —— 那个数字，约 35 分钟墙钟时间。** 以本仓库为语料的六个任务（除 LLM 外不走网络），每个在 `scripts/bench_tasks.yaml` 里配一份机器可校验的评分标准：必需的文件引用加一组关键词。示例：*"How does message-history summarisation decide what to keep, and what message structure results?"*（必须引用 `agent.py:153-233`）；*"Which tools mutate the filesystem, and where is each registered?"*（`file_tools.py`、`cli.py:399-431`）；*"Is the session token counter cumulative?"*（`agent.py:359-360` —— 标准答案：不是，那是赋值）；*"What happens to in-flight messages on Esc?"*（`agent.py:73-93`、`cli.py:718-790`）；*"Which config keys control MCP timeouts and where are they applied?"*；*"List every place a log file is opened for writing."* 评分是对父 agent 最终答案做确定性字符串匹配——不用 LLM judge。

两组，只差一个开关，同一模型、同一批任务：**A** = `enable_subagent: false`；**B** = `enable_subagent: true` 外加 system prompt 里那一段委派说明。3 seed × 6 任务 × 2 组 = 36 次运行。

上报的指标，全部来自 `agent.py:54-57` 与 `agent.py:358-360` 新加的累加器，并遍历 `agent.children`：
1. **每个任务的父上下文增长量** —— 每步之后采样 `parent._estimate_tokens()`（`agent.py:96`）；报告"最终 − 初始"以及峰值。这是头条指标；预测是在搜索密集型任务上有 3–6 倍的下降。
2. **压缩事件次数** —— 有多少次运行触发了 `_summarize_messages`。这是能拿出的最强论断：A 组在若干任务上会触发（一次穿过未截断 `BashTool` 输出的 `grep -rn` 加一次 32 KTok 的 `read_file`，很快就能越过 `token_limit=80000`），B 组应该一次都不触发。委派是预防式策略；证明被动式策略根本不必登场，*本身*就是结果。
3. **整棵树的总计费 token**（父 + 所有子）。即使它多半持平或略差也要报——存在一笔固定的约 1.5 KTok spawn 税（子 agent 的 system prompt + 工具 schema），单独测量并写明。
4. **证据压缩比** = 整棵树检视过的文件内容字节数 ÷ 加进父上下文的 token 数。预期 B 组 20–60×，A 组约 1×。这是真正能把机制作用隔离出来的指标。
5. **重复劳动率** = 既出现在某个子 agent 的 `files_examined` 里、*又*出现在后续父 agent `read_file` 调用中的文件。对主导失败模式的直接测量。
6. **通过率**，以计数形式报告（例如 16/18 vs 17/18），并附上明确的诚实说明：在 n=18 的样本下，这个结论是"没有检测到回退"，不是"没有回退"。

产出：一份 `bench_results.md` 表格，外加逐次运行的原始 JSON。

## 深度追问

1. **子 agent 为什么通过工具调用返回，而不是通过它的最终 assistant 消息？** 四个理由，按重要性递减。(1) 大小由构造保证有上限——自由格式返回意味着父 agent 每次委派的增长量取决于子 agent 想写多少，糟糕的一轮就是 4 KTok 的散文倾泻，代价恰好等于你自己去读那些文件；你只是把 token 挪了个地方，没省下来。(2) schema 由 provider 的 tool-input JSON Schema 强制，校验发生在你看到它之前，违规是一个可修复的 tool error 而不是解析失败。(3) 终止变得显式，且与 `finish_reason` 解耦——扁平循环唯一的停止信号是"模型没有发出工具调用"（`agent.py:397`），这跟模型放弃了根本区分不开。(4) provenance 有地方安放，于是父 agent 能下钻到 `path:lines`，而不必重读整个文件。被否决的替代方案：*在散文里要 JSON*（模型会加代码围栏并裹上一堆评述；你要建一套修复循环，又回到解析上了）；*对子 agent 的 transcript 再做一次摘要 LLM 调用*（多一次调用、多一个幻觉面，而且返回的仍是没有 provenance 的散文）。

2. **子 agent 共享 FileLedger 和权限 session 吗？** 今天根本没有 ledger —— `EditTool.description` 在 `file_tools.py:230` 宣称写前必读，但 `file_tools.py:254-285` 的 `execute` 什么都不强制。设计上的答案是：ledger 必须**按引用共享**，因为如果子 agent 的读取不算在父 agent 头上，父 agent 就得把每个想编辑的文件重读一遍，省下的全都退回去了。真正会绊倒人的微妙之处：ledger 必须以 `(path, mtime, sha256)` 为键，而不是 `(path, who_read_it)` —— 陈旧性是文件的属性，不是读取者的属性。而且有一个 ledger *修不了*的真实危险：子 agent 读了那些字节，父 agent 只看到一份 600 字符的摘要，于是"这个文件已被读过"这句话对进程为真、对正在做编辑的那个 agent 为假。这正是 `Evidence.quote` 存在并被验证的原因——一个准备编辑某个区域的父 agent，要么手里有那段区域的已验证逐字引文，要么就该自己去读文件。权限/审批状态：按引用共享，但子 agent 的回调必须**自动拒绝**，绝不自动批准，也绝不弹提示。弹提示会在父 agent 的终端里乱序冒出来，而且看不出是哪个子 agent 在问；自动批准则会把 `build_child_tools` 里的任何一个 bug 变成一次静默写入。一个撞上审批闸门的只读子 agent 说明过滤有 bug，就该大声失败。

3. **子 agent 为什么是串行的，以及为什么这并不是它看上去的那种妥协？** 父循环按顺序 await 每个工具调用（`agent.py:404`、`agent.py:436`）；并行需要在那里用 `asyncio.gather`，而那会同时打破三件事：进程全局的 `redirect_stdout` 捕获会开始吞掉兄弟 agent 的输出；per-child 的 cancel-event 链接会多出多个并发写入方；`BackgroundShellManager._shells` —— `bash_tool.py:108-110` 的类属性 —— 变成兄弟之间共享的可变状态，于是用于清理的"新 shell id 差集"再也分不清谁的 shell 是谁的。真正要紧的一点：并行子 agent 买到的是**延迟**，不是上下文。上下文收益两种方式完全相同，因为父 agent 的增长是按每次委派封顶的，与委派何时发生无关。既然交付物是上下文机制，那么砍掉并行就是正确选择——而知道*为什么*它是正交的才是答案，不是"我跳过了"这个事实。

4. **是什么阻止模型把什么都委派出去，或者什么都不委派？** 光靠 prompt 是拦不住的，有意思的部分在失败分类。(a) *委派了你需要字节的工作* —— "给我 `_summarize_messages` 的确切函数体"回来的是一段 200 字符引文，父 agent 最后还是去读了文件，你付了两遍钱；这可以用重复劳动率来度量。(b) *规格不足的 prompt* —— 模型写了一行 `prompt`，依赖只有父 agent 才有的上下文，而消息列表真的空空如也的子 agent，自信地回答了另一个问题。这是现实中最主要的失败模式，也正是为什么 `prompt` 参数的描述必须用大写字母写明子 agent 什么都看不到，以及为什么 `questions` 是一个独立的必填数组，而不是揉进散文里。(c) *委派琐事* —— 花 1.5 KTok 的 spawn 税只为知道一个文件名。诚实的立场是：这三种都对 prompt 敏感，机制本身一个都修不了——这恰恰是基准测试在报告那些好看的数字之外，还要报告整棵树的总 token 和重复劳动率的原因。

5. **为什么是三条预算，而不是一个步数？** 因为有三种正交资源，每种都有另外两种约束不住的失败。步数约束不住 token：`ReadTool` 在 32 000 token 处截断（`file_tools.py:147`），所以两步就能吃掉 64 KTok，而 `BashTool` 根本不截断（`BashOutputResult.format_content`，`bash_tool.py:32-48`），所以一次 `grep -rn` 就能超过其中任何一条。token 约束不住时间：一个挂住的 `find` 既不花步数也不花 token，却把整个墙钟时间烧完。时间约束不住步数：一个快模型能在截止时刻内做三十次便宜调用。二阶要点是预算耗尽必须是*优雅的*——到了上限就硬中止会把子 agent 学到的一切丢掉，所以设计在 75% 处告警，然后花最后一步、把工具集缩到 `{submit_report}`，把部分工作转换成部分答案。带已验证证据的 `status="partial"` 报告仍然返回 `success=True`，因为把它标成失败会让父 agent 重做它刚刚付过钱的那件昂贵的事。

6. **你怎么知道隔离真的划算，而不只是把成本挪了个位置？** 你没法从 token 总量知道，宣称能知道就是"这人只读过一篇博客"的破绽。在委派之下总量通常还会略微*变差*，因为每次 spawn 都要交 system-prompt 与 schema 的税。真正站得住的三个论断是：父上下文峰值（一个硬边界，不是启发式）、压缩事件次数（A 组毁掉历史，B 组根本不必触发），以及证据压缩比——每进入父上下文一个 token 所检视的字节数。这三个都是关于 token *在哪里*的论断，而那正是委派改变的东西；没有一个是关于总花费的论断，而总花费它基本不改变。再配上以原始计数报告的通过率，n=18，并写成"没有检测到回退"，不是"没有回退"。要这么小心的原因在于：一个子 agent 不过是四十行 `Agent(...)`——谁都能搭，所以没有数字这套设计一文不值，而如果那个数字是错的数字，它同样一文不值。

## 前置条件

1. `mini_agent/agent.py:359-360` —— `self.api_total_tokens = response.usage.total_tokens` 是赋值，注释却写着 'Accumulate'。必须先变成真正的累加（`self.tokens_in/tokens_out/llm_calls`），否则任何树开销的测量都无从谈起。约 5 行。

2. `mini_agent/logger.py:30-38` —— `start_new_run` 拼出 `agent_run_{HHMMSS}.log` 并以 `"w"` 模式打开，所以同一秒内起的子 agent 会静默截断父 agent 的日志。需要一个 `tag: str = ""` 参数。约 2 行。

3. `mini_agent/agent.py:294-492` —— `run()` 没有 per-step hook，预算强制无处安放。需要在 `agent.py:330` 插入那约 7 行的 `step_hook`（替代方案是继承 `Agent` 并覆写 `run()`，那要复制 200 行，更糟）。

## 明确不做

不做：真正的执行 sandbox（只读 bash 守卫是一套 first-token/flag/segment allowlist，不是容器也不是 seccomp 边界）；并行的兄弟子 agent；多于一种 `subagent_type`；子 agent 套子 agent；把子 agent 的进度流式推进父 agent UI（超出一行状态之外的部分）；MCP 只读性自动分类（改用默认拒绝 + 配置 allowlist）；真正的 `FileLedger`（只预留接缝）；以及基准测试的 LLM judge（改用确定性的"关键词 + 引用"评分标准）。对面试官这样说："只读工具集是防糊涂模型的护栏，不是防敌意模型的 sandbox —— 那条边界属于进程层，把它塞进工具封装里会是安全表演，而且还放错了层。并行子 agent 是我刻意跳过的，因为并行买到的是延迟，而我想演示的机制是一条上下文边界，无论子 agent 同时跑还是顺序跑，它都完全一样。"

## 代码量

约 700 LOC 新增生产代码（`report.py` 约 90、`budget.py` 约 60、`readonly_bash.py` 约 110、`task_tool.py` 约 340、config 约 40、prompt 文件约 60 行散文）+ 跨 `agent.py`、`logger.py`、`cli.py`、`config.py` 的约 30 LOC 修改 + 约 180 LOC 测试 + 约 230 LOC 基准测试脚手架。合计约 1,140，其中约 730 是生产代码。

## 工期

一个人 3.5–4.5 天：1 天做子 agent 运行器、预算和取消线路；1 天做返回契约、证据验证和渲染/降级阶梯；0.5 天做只读 bash 守卫及其对抗性测试；0.5 天做确定性 FakeLLM 隔离测试；1–1.5 天做基准测试脚手架、跑完 36 次运行，并诚实地把数字写出来。
