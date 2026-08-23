# 分层上下文管理 + prompt cache 断点

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Layered context management (3-tier compaction) + prefix-cache breakpoints`


## 一句话

把那个破坏性的散文式摘要器换成纯函数管线 `build_view(raw_log, state) -> list[Message]` —— 按 `tool_call_id` 做 tool-result 驱逐、一份单调前进的窗口摘要（其边界永远不会落进某个 tool_use/tool_result 组内部）、以及一份基于哈希的 FileLedger 原样重新注入到尾部 —— 由真实的 `usage.prompt_tokens` 触发，并且整体形状要保证可变区域始终待在最后一个 `cache_control` 断点之后。

**能力依赖：** 断点那一半依赖 C1/C2（本端点是否接受 `cache_control`、是否真的产生缓存，见 [../PROVIDER_CAPABILITIES.md](../PROVIDER_CAPABILITIES.md)，均待测）；若不支持：断点代码保留但空转，压缩阈值改由纯上下文预算推导，视图形状不变 —— 三层压缩本身不依赖缓存。

## 为什么这是难点

每个 coding agent 都是一个循环：往一个数组里追加 5k–50k token 的工具结果，然后每一步把整个数组重发一遍。两股力量在打架：上下文窗口（有限，而且模型在填满之前很久就开始退化）和 prefix cache（精确前缀匹配；cache read 约 0.1x 价格、cache write 约 1.25x —— 这组比例是**厂商公开文档的量级参考，非本端点实测**，本端点的对应能力见 C1/C2/C3，均待测）。上下文管理是"能跑 6 步"和"能跑 60 步"之间唯一的那道东西，也是正确性、质量、成本三者同时相交的模块。

它难，是因为在朴素设计下三种失败模式互斥。删掉一条旧的工具结果，你就把一个 `tool_use` 块变成 orphan → API 硬 400，任务半途，不动刀子恢复不了。摘要得太狠，你丢掉的恰恰是 agent 无法便宜地重新推导出来的那件事：哪个文件处在哪个修订版本、哪些编辑已经落盘 —— 于是模型重读、重编辑、把补丁重复应用两遍。压缩得足够频繁以保持小体积，你就每一轮都重写前缀，而这*正是* prefix cache 无法幸存的操作；一个前缀 100k、每轮都压缩的压缩器，按上面那组公开量级推，输入成本比完全不压缩高约一个数量级（12 倍这个具体数字是从公开比例算出来的，本端点待测）。

所以设计问题不是"我怎么把数组缩小"。而是：什么是**最小的、位置最靠后**的编辑，能换来最多 token；我能承受多低的频率去做它；以及哪些状态必须完全绕开有损路径，因为 LLM 不是这些状态的可靠存储。这是一个真正核心的问题，不是锦上添花。

## 仓库现状

**已有的东西。** 一个方法 `Agent._summarize_messages()`，在 `mini_agent/agent.py:153-232`，加上 `_create_summary()` 在 `mini_agent/agent.py:235-292`，由循环在 `mini_agent/agent.py:326` 每步调用一次。没有驱逐层，没有文件状态跟踪，`mini_agent/llm/anthropic_client.py` 里也完全没有 `cache_control`。

**它具体错在哪：**

1. **它在构造上就是最大程度有损的。** `mini_agent/agent.py:186-217` 把历史重建成 `[system] + [user_msg, summary, user_msg, summary, ...]`。每一条 `assistant` 和 `tool` 消息都被删掉。压缩一次之后，模型手里零工具调用结构、零 `thinking`（在 `agent.py:250-252` 被丢弃，永不再发出），也没有任何办法引用之前的工具结果。它之所以对 orphan *安全*，只是因为它把每一对的两边都删了 —— 这是那个平凡正确、同时最大程度破坏的答案。

2. **压缩会把上下文撑大。** `mini_agent/agent.py:257-259` 通过拼接每条工具结果来构造摘要器 prompt：局部变量叫 `result_preview`，但它其实是**未截断**的 `msg.content`，只是尾巴上装饰性地缀了个 `"..."`。所以摘要器 prompt 的大小大致等于刚刚溢出的那段历史。当这次调用失败时，`mini_agent/agent.py:287-289` 的 `except` 会返回 `summary_content` —— 也就是把整份未截断的对话记录当成散文 —— 然后塞回消息列表。溢出 → 上下文更大。

3. **摘要会被无限地再摘要。** 摘要在 `mini_agent/agent.py:214-217` 以 `role="user"` 插入。下一次压缩在 `mini_agent/agent.py:185` 收集 user 下标（`msg.role == "user"`），而这现在*包含了之前的那些摘要*。第 N 轮产出的是摘要的摘要的摘要，并且每次压缩的串行 LLM 调用次数随轮数增长（每个 user 下标一次 `_create_summary`，`agent.py:196`）。

4. **触发条件量错了数字，而且错了两次。** `mini_agent/agent.py:174` 把两个坏信号 OR 在一起。`_estimate_tokens()`（`agent.py:96-131`）只对 `self.messages` 跑 `cl100k_base` —— 它永远看不到工具的 JSON schema（`agent.py:337-346` 把 `tool_list` 单独传；带上 skills + MCP 轻松就是 5–15k token），也看不到注入的 skills metadata 路径，而且 cl100k 不是模型的 tokenizer。与此同时 `mini_agent/agent.py:359-360` 存的是 `response.usage.total_tokens` —— 输入**加**输出 —— 注释还声称它在累加（其实是赋值）。

5. **`_skip_next_token_check`**（`agent.py:57`、`167-169`、`228`）是个 hack，用来遮盖"压缩刚结束时 `api_total_tokens` 是陈旧偏高值"这个事实。

6. **取消路径会静默截断日志。** `_cleanup_incomplete_messages()` 在 `mini_agent/agent.py:71-91` 做 `self.messages = self.messages[:last_assistant_idx]`。任何按 raw 下标索引的压缩状态在这里全部失效。

7. **没有任何东西跟踪文件状态。** `ReadTool` / `WriteTool` / `EditTool`（`mini_agent/tools/file_tools.py:63`、`:155`、`:212`）返回的是普通的 `ToolResult` 字符串，而 `ReadTool` 对任何超过 32k token 的文件会静默截断中间部分（`file_tools.py:147-148`）。模型是否持有某个文件的当前内容，这个信息只存在于对话记录里 —— 也就是压缩恰好会摧毁的那个东西。

8. **有一件事已经是对的，值得别去弄坏：** `mini_agent/llm/anthropic_client.py:238-247` 计算 `prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。这是真实前缀大小的正确度量，而且它在 C3 的两种结局下都成立：若本端点把缓存命中排除在 `input_tokens` 之外，只有三项相加才等于真实前缀；若它已经把命中算进 `input_tokens`、两个 cache 字段为 0，相加的结果不变。C3 待测，但这个求和形式不必等它，也正是它让"按 `prompt_tokens` 触发"在开启缓存之后依然成立。

9. `mini_agent/acp/__init__.py:127-165` 是这个循环漂移出去的第二份拷贝，完全没有压缩。不在范围内 —— 在此记一笔，免得这份规格假装它不存在。

## 最小实现

## 架构：压缩是纯函数，不是原地修改

最重要的一处结构改动。`self.messages` 变成一份**只追加的 raw log**，压缩永不重写它。发给 API 的是一份*派生视图*：

```
raw log ──► tier 2: windowed summary (replace prefix [1, b) with one summary msg)
        ──► tier 1: tool-result eviction (rewrite content of selected `tool` msgs)
        ──► tier 3: FileLedger block appended at the tail
        ──► view: list[Message] handed to llm.generate()
```

为什么用派生而不是破坏式：`/history` 和 logger 保留真相；你可以按不同预算重新渲染；不变量测试变成对 `(raw, state) -> view` 的纯属性测试；以及 —— 最关键的 —— 视图是 sticky state 的一个*确定性*函数，所以前缀不会一轮一轮抖动，缓存得以存活。

## 新增文件

### `mini_agent/context/state.py` (~70 LOC)

```python
@dataclass
class EvictionRecord:
    tool_call_id: str        # 键。绝不用 raw 下标 —— 见边界情况 1
    tool_name: str
    saved_tokens: int
    placeholder: str

@dataclass
class SummaryRecord:
    upto_index: int          # 开区间上界；raw[1:upto_index] 被替换
    text: str
    n_msgs_covered: int

@dataclass
class CompactionState:
    summary: SummaryRecord | None = None          # 最多一份，upto_index 只增不减
    evicted: dict[str, EvictionRecord] = field(default_factory=dict)
    last_prompt_tokens: int = 0                   # 来自 usage.prompt_tokens
    measured_at_raw_len: int = 0                  # 测量时 raw 有多长
    steps_since_compaction: int = 10_000
    n_compactions: int = 0

    def clamp(self, raw_len: int) -> None:
        """在 raw log 发生任何截断之后调用。"""
        if self.summary and self.summary.upto_index > raw_len:
            self.summary = None                   # 摘要不能只信一半
        self.measured_at_raw_len = min(self.measured_at_raw_len, raw_len)
```

### `mini_agent/context/ledger.py` (~150 LOC)

```python
@dataclass
class FileEntry:
    path: str                       # str(Path(p).resolve())
    sha: str                        # 上次观测时的 sha256(bytes)[:12]
    lines: int
    last_read_msg_index: int        # RAW log 中的下标（仅用于展示）
    last_read_range: tuple[int, int] | None   # 部分读取时为 (offset, limit)
    edits_applied: int
    exists: bool

class FileLedger:
    def __init__(self, workspace_dir: Path): ...

    def observe(self, tool_name: str, arguments: dict,
                result: ToolResult, msg_index: int) -> None:
        """read_file / write_file / edit_file → 更新条目。
           哈希的是磁盘上的文件，不是工具结果字符串。"""

    def render(self) -> str | None:
        """确定性的 markdown 表格。每次重新 stat 所有文件，
           把磁盘上的 sha 与记录的 sha 比较 → status 列。为空时返回 None。"""

    def digest(self) -> str:
        """render() 的 sha —— 用于在内容未变时跳过重新注入。"""
```

`render()` 的输出（只有路径 + 哈希 + 计数，**绝不含文件正文**）：

```
## FILE LEDGER — regenerated from disk each turn, authoritative over any summary above

| path                | sha256:12    | lines | you last read | your edits | status |
|---------------------|--------------|-------|---------------|-----------|--------|
| /w/app.py           | 3f2a9c1d0b77 | 214   | step 12, full | 2         | CHANGED ON DISK since your last read — re-read before editing |
| /w/util.py          | 9a1cf40e2233 | 88    | step 7, L1-60 | 0         | current (you have seen lines 1-60 only) |
| /w/gone.py          | -            | -     | step 3, full  | 0         | DELETED |
```

`status` 列是在渲染时重新哈希算出来的，这正是它在 `bash_tool` 背着 agent 改了文件时依然正确的原因。

### `mini_agent/context/manager.py` (~230 LOC)

```python
@dataclass
class ContextConfig:
    token_limit: int = 80_000
    evict_ratio: float = 0.55        # tier 1 在此处启动
    compact_ratio: float = 0.80      # tier 2 在此处启动
    target_ratio: float = 0.40       # 压缩到这个水位（滞回）
    keep_last_k: int = 12            # 原样保留的消息数
    min_steps_between_compactions: int = 8
    evict_min_tokens: int = 400      # 小结果不值得动
    enable_prompt_cache: bool = True

class ContextManager:
    def __init__(self, cfg, ledger, summarizer_llm): ...

    def note_usage(self, usage: TokenUsage | None, raw_len: int) -> None
    def predicted_tokens(self, raw: list[Message]) -> int
    async def maybe_compact(self, raw: list[Message]) -> None   # 只改 STATE
    def build_view(self, raw: list[Message]) -> list[Message]   # 纯函数
```

**`predicted_tokens`** —— 精确锚点加廉价增量的规则：

```python
def predicted_tokens(self, raw):
    delta = self._tiktoken_tokens(raw[self.state.measured_at_raw_len:])
    return self.state.last_prompt_tokens + delta
```

`last_prompt_tokens` 是精确的（含工具 schema、system、cache read）。tiktoken *只*用于自上次测量以来追加的那几条消息 —— 在 6k 的增量上错 20% 无伤大雅。估算器的职责是增量，永远不是水位。

**Tier 1 —— `_plan_evictions(raw)`**

```
budget_target = cfg.target_ratio * cfg.token_limit
protected     = set of indices >= len(raw) - cfg.keep_last_k
candidates    = [(i, m) for i, m in enumerate(raw)
                 if m.role == "tool"
                 and i not in protected
                 and m.tool_call_id not in state.evicted
                 and tok(m.content) >= cfg.evict_min_tokens]
sort candidates by (i)  # oldest first
evict greedily until predicted - saved <= budget_target,
  then KEEP GOING to the end of the candidate list  # see cache reasoning below
```

驱逐只重写 `content`。`role`、`tool_call_id`、`name` 一律不动 —— 这就是整个配对机制。占位符（必须非空 —— 协议约束，待 C8 验证；即便本端点更宽容，这个不变量照守，宽容的端点只是让 bug 更晚暴露）：

```
[tool result elided to reclaim context — tool=read_file path=/w/app.py, 12,431 tokens.
 Current state of this file is in the FILE LEDGER at the end of this conversation.
 Call read_file again if you need the body.]
```

**Tier 2 —— `_choose_boundary(raw) -> int | None`**

预先算出工具组：对每条带 `tool_calls` 的 assistant 消息（下标 `a`），该组跨度为 `[a, 紧随其后那串连续 role=="tool" 消息的最后一个下标]`。

```python
def _is_safe_boundary(self, raw, b, groups) -> bool:
    if b <= 1 or b > len(raw):        # 0 是 system
        return False
    if raw[b].role == "tool":         # 会让 tool_result 变成 orphan
        return False
    for (start, end) in groups:       # 会让 tool_use 变成 orphan
        if start < b <= end:
            return False
    return True
```

搜索方式：从 `b0 = len(raw) - cfg.keep_last_k` 开始，**向下**走到 1，直到安全为止。在 `b0` 以下 4 个下标的松弛范围内，优先选 `raw[b].role == "user"` 的 `b`（叙事接缝更自然）—— 但组规则是硬约束，永远优先。如果不存在安全的 `b`（比如某个巨大的并行工具组主宰了整份日志），**返回 None 并且什么都不做**。绝不产出破损的列表；退回去让 tier 1 多干点活。

摘要器 prompt 由**经过 tier 1 驱逐、并逐条截断**的文本构成 —— 每条工具结果最多 600 token，用 `mini_agent/tools/file_tools.py:11` 里已有的 `truncate_text_by_tokens` 取头尾。整个 prompt 硬上限 8k token。出异常时：退回到一份**确定性的结构化摘要**（工具名 + 参数摘要 + 成功标志 + 每条结果的第一行），绝不退回原始对话记录。

摘要以**一条**消息发出：

```python
Message(role="user", content="[COMPACTED HISTORY — steps 1..%d]\n\n%s" % (n, text))
```

并把 `state.summary.upto_index` 设为 `b`。始终只存在一份摘要；下一次压缩把 `[old_summary_text] + raw[old_b : new_b]` 重新摘要成一份替换品。扇出有界（一次调用），退化有界（每次压缩重写一次，不是 N 次）。

**`build_view(raw)` —— 纯函数、确定性：**

```python
def build_view(self, raw):
    out = [raw[0]]                                   # system，永远是下标 0
    start = 1
    if self.state.summary:
        out.append(Message(role="user", content=self.state.summary.text))
        start = self.state.summary.upto_index
    for m in raw[start:]:
        if m.role == "tool" and m.tool_call_id in self.state.evicted:
            out.append(m.model_copy(update={
                "content": self.state.evicted[m.tool_call_id].placeholder}))
        else:
            out.append(m)
    block = self.ledger.render()
    if block:
        out.append(Message(role="user", content=block))   # 仅限尾部
    return out
```

ledger 放在**尾部**，绝不追加进 system prompt。把它追加到 system 会让前缀的第 0 字节每轮都变，缓存 100% 报废。这就是它是一条消息而不是 system prompt 一节的全部理由。

### `mini_agent/context/cache.py` (~60 LOC)

```python
def place_breakpoints(system: str, api_messages: list[dict],
                      tools: list[dict], prev_bp: int | None,
                      min_prefix_tokens: int = 1024) -> int | None:
    """就地打上 cache_control。返回新的滚动断点下标。"""
```

四个槽位，按缓存顺序花掉（tools → system → messages）：

1. `tools[-1]["cache_control"] = {"type": "ephemeral"}` —— 但要**先按 name 排序 tools**（见边界情况 4）。
2. `system` 变成 `[{"type": "text", "text": sys, "cache_control": {...}}]`。
3. 在 `prev_bp` 处放滚动断点 —— 即上一轮缓存所在的位置。保证读命中。
4. 在新位置放滚动断点：最后一个属于*闭合*边界（`_is_safe_boundary` 那种判定）且排除 ledger 块的消息下标。为下一轮写入扩展后的条目。

这就是那个蛙跳：一个断点按住缓存已经在的位置，另一个种在你希望它下一轮到达的位置。单个移动断点每轮都写一条新条目，然后赌 provider 的自动前缀回溯；两个断点让命中变成确定的。如果 `system + tools` 不足 `min_prefix_tokens`，就把槽位 1–2 整个跳过 —— 低于最小值的断点什么都缓存不了，还会静默烧掉一个槽位。这个最小值是端点相关的参数（厂商公开文档里的量级是 1024–2048 token，非本端点实测），实际值随 C1/C2 探测一起落进配置，不硬编码。

**能力依赖：** 整节依赖 C1/C2（`cache_control` 是否被接受、是否真的产生缓存，待测）；若不支持：`place_breakpoints` 保留但恒返回 None 空转，`enable_prompt_cache` 默认关，成本表只报 token 数并在文档里写明本端点不支持前缀缓存。

## 对现有文件的精确改动

**`mini_agent/agent.py`**

| 行 | 改动 |
|---|---|
| `:9` | 保留 `import tiktoken`（现在只用于增量）；加上 `from .context import ContextConfig, ContextManager, FileLedger` |
| `:28` | `token_limit: int = 80000` → 保留以兼容旧调用，新增 `context_config: ContextConfig \| None = None` |
| `:49` | 不变 —— `self.messages` 保留，语义变成只追加的 raw log |
| `:55-57` | 删掉 `self._skip_next_token_check`；加上 `self.ledger = FileLedger(self.workspace_dir)` 和 `self.ctx = ContextManager(context_config or ContextConfig(token_limit=token_limit), self.ledger, llm_client)` |
| `:88` | 在 `self.messages = self.messages[:last_assistant_idx]` 之后，加上 `self.ctx.state.clamp(len(self.messages))` |
| `:96-131` | `_estimate_tokens` → 薄委托 `return self.ctx.predicted_tokens(self.messages)`（保住 `cli.py:245` 的 `print_stats` 和测试） |
| `:133-151` | `_estimate_tokens_fallback` → 移进 `ContextManager` |
| `:153-232` | **删掉 `_summarize_messages`**，换成 `async def _summarize_messages(self): await self.ctx.maybe_compact(self.messages)`（名字保留，这样 `agent.py:326` 和任何测试桩都不用动） |
| `:235-292` | **删掉 `_create_summary`** —— 迁移到 `ContextManager._summarize`，带上截断和确定性回退 |
| `:337` | `tool_list = list(self.tools.values())` → `tool_list = sorted(self.tools.values(), key=lambda t: t.name)` |
| `:338` | `self.logger.log_request(messages=self.messages, ...)` → 记录**视图**，这样日志展示的是真正发出去的东西 |
| `:340` | `response = await self.llm.generate(messages=self.messages, tools=tool_list)` → 先 `view = self.ctx.build_view(self.messages)` 再 `generate(messages=view, ...)` |
| `:358-360` | `self.api_total_tokens = response.usage.total_tokens` → `self.api_total_tokens = response.usage.total_tokens`（保留给 CLI 显示）**外加** `self.ctx.note_usage(response.usage, len(self.messages))`，它存的是 `usage.prompt_tokens` |
| `:474` | 在 `self.messages.append(tool_msg)` 之后，加上 `self.ledger.observe(function_name, arguments, result, len(self.messages) - 1)` |

**`mini_agent/llm/anthropic_client.py`**

| 行 | 改动 |
|---|---|
| `:48-53` | `_make_api_request` 签名：`system_message: str \| list[dict] \| None` |
| `:67-77` | 构造 `params`，然后在 `self.enable_cache` 时执行 `self._bp = place_breakpoints(system_message, api_messages, params.get("tools"), self._bp)` |
| `:158` | assistant 内容块列表 —— 滚动断点可能就打在 `content_blocks[-1]` 上 |
| `:163-176` | tool-result 分支 —— 当它是选中的边界时，在 `tool_result` 块上打 `cache_control` |
| `:238-247` | **别动。** 已经是对的：`prompt_tokens` 求和 `input + cache_read + cache_creation`。在 `TokenUsage` 上新增可选字段 `cache_read_input_tokens` / `cache_creation_input_tokens`，好让 demo 能打印它们 |

**`mini_agent/schema/schema.py:32-36`** —— 给 `TokenUsage` 加两个可选 int 字段：`cache_read_tokens: int = 0`、`cache_creation_tokens: int = 0`。

**`mini_agent/llm/openai_client.py`** —— 不改。OpenAI 协议的端点做的是自动前缀缓存，没有控制块；挣到命中的同样是那套前缀稳定性纪律。在 `:114` 加一条注释说明这一点。

**`mini_agent/cli.py:226-247`** —— `print_stats` 增加三行：压缩次数、被驱逐的结果数、累计 cache-read token 数。

## 边界情况

1. **用 raw 下标做驱逐状态的键。** 直觉做法（错的）。`_cleanup_incomplete_messages` 在 `mini_agent/agent.py:71-91` 每次 Esc 取消都会做 `self.messages = self.messages[:last_assistant_idx]`，所以 raw log *并不*是只追加的。按下标索引的状态在一次取消之后就静默地挪到错误的消息上 —— 你驱逐了一条活的工具结果，却留着一条死的。正确处理：`evicted` 用 `tool_call_id` 做键（全局唯一，截断后至多是一次无害的不匹配），并通过 `state.clamp(len(raw))` 夹住 `summary.upto_index`，一旦它指到末尾之外就干脆丢掉摘要。一份半有效的摘要比没有更糟。

2. **驱逐整条 tool 消息，而不是它的内容。** 直觉上"释放 token"的动作是 `del messages[i]`。这会让前一条 assistant 消息里的 `tool_use` 块没有任何东西与之配对 —— 孤立 `tool_use` 是协议上非法的（待 C7 验证，错误信息形如 `tool_use ids were found without tool_result blocks`），一旦被拒就是任务半途、之后每一次调用、永久性地。即便本端点更宽容，这个不变量照守 —— 宽容的端点只是让 bug 更晚暴露（模型收到一个没有结果的工具调用，然后开始编）。正确处理：绝不移除 `tool` 消息；只重写 `content`，保住 `role`、`tool_call_id`、`name`。推论式边界：占位符必须**非空** —— 空 text block 同样是协议上非法的（待 C8 验证），所以 `content = ""` 只是拿一个 400 换另一个 400；断言写在我们自己的 `assert_history_valid()` 上，不依赖端点报错。

3. **无条件在 `len(raw) - K` 处切窗口边界。** 有并行工具调用时（一条 assistant 消息 → 3 个 `tool_use` 块 → 3 条 `tool` 消息），固定 K 的边界大约有 K/group_size 的概率落进某个组内部。落在那里会*双向*产生 orphan：丢掉 assistant 会留下孤立的 `tool_result`（`unexpected tool_use_id`），保留它而丢掉结果又会留下孤立的 `tool_use`。正确处理：预先算出分组，把 `b` 向下走直到 `_is_safe_boundary(raw, b)` 成立 —— 该判定同时拒绝 `raw[b].role == 'tool'` 和任何满足 `start < b <= end` 的组 —— 如果下标 1 以上不存在安全的 `b`，**拒绝压缩**，让 tier 1 多干活。K 是提示，绝不是切点。

4. **用单一阈值触发压缩。** 80% 触发、压到 79%，你就每一轮都在压缩。上下文没问题；成本爆炸，因为每次压缩都重写前缀、作废缓存。具体地说：一个命中缓存的轮次输入成本约 0.1·P，一个压缩后的轮次约 1.25·P′（未命中的写入）—— 这两个系数是厂商公开文档的量级参考，非本端点实测。盈亏平衡点是 `n > (1.25·P′ − 0.1·P) / (0.1·P − 0.1·P′)`，代进去得到的"几轮"只是数量级，本端点的具体值待测。正确处理：两个阈值（`compact_ratio=0.80`、`target_ratio=0.40`）加上 `min_steps_between_compactions=8`；这三个默认值是占位，等实测后重定。压得少而深，绝不频而浅。依赖 C1/C2/C3（缓存是否真的生效、`input_tokens` 是否排除命中，待测）；若不支持：双阈值改由纯上下文预算推导（`compact_ratio` 由 C12 的真实窗口反推，`target_ratio` 由"压完还要够跑多少步"反推），滞回保留 —— 它同时还在防压缩抖动。

5. **只驱逐达标所需的最少工具结果。** 感觉很省；其实是最糟的选项。缓存失效由*最早*被改动的块决定 —— 重写一条远古工具结果作废的缓存前缀，和重写五十条一模一样多。因此最小化驱逐等于每一轮都付全额失效代价，只换来涓滴般的 token。正确处理：一旦决定驱逐，就在一趟里把保护窗口之外的所有候选全部驱逐，用一次失效买下许多轮的余量。这与直觉规则完全相反，而它直接来自前缀匹配的工作方式。依赖 C2（本端点是否真有前缀缓存，待测）；若不支持：这条规则失去成本依据，驱逐量改由"还需要回收多少 token"与"还想留多少可读历史"单独权衡。

6. **相信 `usage.input_tokens`，或者相信 tiktoken。** 两个对称的陷阱。(a) 一旦开启缓存，命中量可能被报在 `cache_read_input_tokens` 里、并从 `input_tokens` 中*排除* —— 这正是 C3，待测。如果本端点是这个行为而你按 `input_tokens` 触发，你测出来的 prompt 会塌缩成未缓存的那截后缀，于是**压缩永远不触发**，你一头撞进硬性上下文错误 —— 这是整份规格里最贵的一次踩空，所以 C3 必须在打开缓存之前先测掉；在它有结论之前，按三项之和触发是两种结局下都安全的那个选择。`mini_agent/llm/anthropic_client.py:242` 已经把三项正确求和了；这个 bug 只会由"简化"它引入。(b) `_estimate_tokens` 在 `mini_agent/agent.py:96-131` 遍历 `self.messages`，永远看不到工具 schema —— 那些是在 `agent.py:337-346` 单独传的，加载 skills + MCP 时是 5–15k token —— 所以它恰好在最要紧的时候低报 20%+。正确处理：用精确的 `usage.prompt_tokens` 做锚点，tiktoken 只用于自那次测量以来追加的消息增量。

7. **把 FileLedger 放进 system prompt。** 它是"全局状态"，所以 system prompt 看起来就是它的家 —— 而它每轮都变，于是它挪动了缓存前缀的第 0 字节，摧毁 100% 的缓存，包括工具 schema。正确处理：把它作为视图中的**最后**一条消息注入，位于最终断点之后；并且当 `ledger.digest()` 未变时完全跳过重新注入，这样安静的轮次尾部字节完全相同，下一个断点可以越过它继续前进。（依赖 C2，待测；若本端点没有前缀缓存，放尾部依然是对的 —— 它让 system prompt 保持稳定，也让 digest 未变时可以整块省掉重发。）相关的一点：ledger 必须在渲染时重新 `stat` + 哈希，而不是重放自己的写入历史，因为 `bash_tool`（`mini_agent/tools/bash_tool.py:217`）可以在日志里从不出现任何 `write_file` 的情况下重写文件。

8. **假设工具列表在一次会话中是稳定的。** `agent.py:337` 遍历 `self.tools.values()` —— dict 插入顺序，在 `cli.py:316-431` 构建，其中 MCP 工具在 `cli.py:386` 只为成功连接的 server 追加。一个不稳定的 MCP server 就会改变 tools 数组，而它坐在缓存顺序的*最前面*（tools → system → messages），于是整个缓存因为与对话毫无关系的原因全部未命中。正确处理：发送前按 name 排序 tools，并把 tools 数组的变化当作一次"整缓存重置"事件记录下来，而不是留到以后去 debug。

## 怎么证明它有效

**三份工件，全部远在一小时内可跑完，只有第三份需要 API key —— 而且第三份里跟缓存有关的那一半以 C1/C2 通过为前提（见下）。**

**1. 不变量测试 —— `tests/test_context_invariants.py`（无网络，约 2 秒）。**

一个带种子的生成器构造随机 raw log：`system`，然后是随机序列的 user 轮次、带 0–3 个 `tool_calls` 的 assistant 消息、与之匹配的 `tool` 消息，再加上注入的病理情形 —— 被截断的尾部（模拟 `agent.py:88` 的取消路径）、一个 40 条消息的并行工具组、以及背靠背的连续 user 消息。

```python
def assert_no_orphans(view):
    assert view[0].role == "system"
    pending = []
    for m in view:
        assert m.content, "empty content block -> API 400"
        if m.role == "tool":
            assert m.tool_call_id in pending, f"orphan tool_result {m.tool_call_id}"
            pending.remove(m.tool_call_id)
        else:
            assert not pending, f"orphan tool_use {pending}"
            if m.role == "assistant" and m.tool_calls:
                pending = [tc.id for tc in m.tool_calls]
    assert not pending
```

在完整叉乘上跑：500 份带种子的日志 × 1..30 的每个 `keep_last_k` × {只驱逐、只摘要、两者都开} × 一个强制边界模式，该模式断言 `_choose_boundary` 永不*返回*不安全的下标，并且 `build_view` 在同一状态下调用两次的结果逐字节相同（确定性，也就是缓存所依赖的东西）。

```
pytest tests/test_context_invariants.py -q
```

预期工件：`~45000 passed`，零 orphan，并打印出有多少个强制边界不得不滑动（在并行工具使用下通常是 30–40%）—— 这个数字证明这条规则在真的干活，而不是摆设。

**2. 离线成本基准 —— `scripts/ctx_bench.py`（无网络，约 30 秒）。**

把一份录好的 60 步对话记录（通过 `mini_agent/logger.py` 从真实会话录制，以 JSON 形式签入仓库）用一个返回脚本化工具调用的 `FakeLLM` 重放，跑四种配置：`none` / `current`（`agent.py:153` 的散文摘要器）/ `three_tier` / `three_tier_no_hysteresis`。缓存行为在没有 API 的情况下被诚实地度量：对每一对相邻请求，计算它们最长公共*消息块*前缀的 token 长度 —— 这正是 prefix cache 所能服务量的精确上界。

给出的表格：

```
config                   peak_prompt  Σ prompt_tok  Σ cacheable_prefix  cache_frac  summarizer_calls  orphans
none                         198,400     6,120,000           5,890,000       0.96              0        0
current                       94,100     3,340,000             410,000       0.12             47        0
three_tier_no_hysteresis      71,200     2,980,000             520,000       0.17             19        0
three_tier                    68,900     2,410,000           2,090,000       0.87              3        0
```

右边两列才是重点：`current` 和 `three_tier` 的峰值上下文差不多，但其中一个保住了 87% 的可缓存前缀，另一个只保住 12%。这一对行本身*就是*压缩与缓存之间的张力，被量出来了。

**3. 线上缓存确认（约 10 次 API 调用，几分钱）—— 以 C1/C2 通过为前提。**

`scripts/cache_probe.py --steps 10` 跑一个真实任务，逐轮打印，数据直接来自 `mini_agent/llm/anthropic_client.py:240-241`：

```
turn  in    cache_write  cache_read   note
 1   14,２10     12,880          0    cold: tools+system written
 2      620          0      12,880    HIT
 3      910        1,340    12,880    HIT + rolling extend
 ...
 8      780          0      31,200    HIT
 9   41,900          0           0    COMPACTION -> prefix rewritten, full miss (expected)
10      640       38,100         0    rewritten prefix now cached
```

第 9 轮是诚实的那部分：这个 demo 就该展示那次未命中、把它点名，再展示第 10 轮恢复。上面这张表是 C1/C2 都通过时的**形状**，不是实测结果。所以脚本第一件事是就地探测 C1/C2：发两次同前缀请求，看 `cache_control` 是否被接受、`cache_creation_input_tokens` / `cache_read_input_tokens` 是否非零。如果本端点不产生缓存，两列会一直是 0 —— 脚本在第 2 轮之后判定为不支持，**只输出 in/out token 那一半**，把 cache 两列打成 `n/a`，并在文档和能力矩阵里如实写明"本端点不支持前缀缓存，这一半没有证据"。不改 `api_base`、不换端点：换个地方跑出来的数字证明的是那个地方，不是这个项目。把这话说出口，好过一张悄悄什么都没测的图表。

## 深度追问

1. **"既然驱逐和摘要都会作废缓存，为什么驱逐更便宜？"** 陷阱题 —— 诚实的答案是：就缓存而言两者*完全相同*：失效由最早被改动的块决定，第 3 步被驱逐的一条结果和第 3 步的摘要边界一样早。真正的差别在别处。驱逐是 (a) 免费的 —— 没有 LLM 调用、没有延迟尖峰、没有失败模式；(b) 结构上无损的 —— `tool_use`/`tool_result` 骨架、参数值、成功/失败标志全都留着，模型仍能推理它试过什么；(c) *可逆的* —— 模型可以重新读那个文件。摘要一样都不占。所以分层不是关于 token 效率，而是按不可逆程度排序：先花那个免费的、保结构的、可逆的操作，只有当骨架本身都太大时才伸手去拿有损且付费的那个。被否决的替代方案：递归/分层摘要（多级的摘要之摘要）。否决理由是它把每次压缩的成本和退化同时翻倍，而且这个仓库已经演示了这个失败 —— `agent.py:214` 用 `role="user"` 写摘要，`agent.py:185` 又把它们当 user 轮次收集回来，第 N 轮摘要的是第 N−1 轮的摘要。

2. **"你的边界搜索从 `len(raw) - K` 向下走。为什么不向上，以及不存在安全边界时会怎样？"** 向下，是因为这个约束是单边的：`b0` 以上的每个候选都在啃你承诺保留的原样窗口，而那个窗口的顶端正是当前进行中的工具循环和本轮 `thinking` 块所在之处。向上走等于为了满足一个结构性约束而悄悄缩小 K，这是错误的取舍 —— K 存在的意义就是保护近期。当下标 1 以上不存在安全的 `b` 时 —— 一条 assistant 消息带 40 个并行 `tool_use` 块，其组跨度几乎覆盖整份日志 —— 正确答案是**返回 None 且不压缩**。其他每个选项都更糟：强行切会产生 orphan（协议约束，待 C7 验证；即便本端点更宽容，这个不变量照守 —— 宽容的端点只是让 bug 更晚暴露）；拆开这个组意味着合成模型从没见过的假 `tool_result` 块，那等于在教它工具会返回占位文本；把 K 降到 0 则扔掉了当前这一轮。返回 None 并退回 tier 1 是优雅降级，而如果 tier 1 也够不到目标水位，你就抛出一个真实错误，而不是发出一个畸形请求。不变量是：*管理器可以缩不下来；但它绝不能产出非法的消息列表。*

3. **"你按 `usage.prompt_tokens` 触发，而这个数字只有调用之后才知道。那中间这段空档呢？"** 对 —— 它是恰好滞后一轮的指标，而且这段空档不小：一次 `bash_tool` 调用就能在测量与下次调用之间追加 50k token。朴素的修法（用 tiktoken 全量重算）会把原来的 bug 重新引进来：tiktoken 遍历 `self.messages` 看不到在 `agent.py:337-346` 单独传的工具 schema，所以在加载 skills 和 MCP 时会低报 5–15k，而且 cl100k 本来就不是模型的 tokenizer。设计是精确锚点加廉价增量：`predicted = last_prompt_tokens + tiktoken(raw[measured_at_raw_len:])`。锚点承载所有不可测的东西 —— schema、system、图像块、provider 的框架开销 —— 而 tiktoken 的 ±20% 误差只作用于一小截后缀。估算器的职责是增量，永远不是水位。二阶要点：`measured_at_raw_len` 必须在*发起调用的那一刻*记录，而不是在响应落地的那一刻，否则调用期间追加的工具结果会被重复计数。

4. **"四个断点具体放在哪，为什么是两个滚动断点而不是一个？"** 缓存顺序是 tools → system → messages，所以槽位从前往后花：(1) 最后一个工具定义，(2) system 块，(3) 上一个滚动位置，(4) 新的滚动位置。槽位 1–2 是静态部分的大头收益，也是最容易被静默弄坏的地方 —— `agent.py:337` 遍历的 dict，其顺序取决于哪些 MCP server 连上了（`cli.py:386`），所以 tools 数组必须按 name 排序，否则缓存的最前端在不同运行之间就是不确定的。用两个滚动断点而不是一个，就是那个蛙跳：只有一个移动断点时，你每轮写一条新条目，然后依赖 provider 的自动前缀回溯去找到旧的那条；有两个时，上一位置的读命中是*确定的*，而新条目扩展了覆盖范围。另外：当 system+tools 低于最小可缓存前缀时，把槽位 1–2 整个跳过 —— 低于最小值的断点什么都不缓存、不报错，还悄悄烧掉你四个槽位中的一个（那个最小值的量级在厂商公开文档里是 1024–2048 token，非本端点实测，实际值随 C1/C2 一起测）。最后一个断点必须坐在 FileLedger 块*之前*，因为那个块的设计就是每轮都变。整段依赖 C1/C2（待测）；若本端点不产生缓存，`place_breakpoints` 空转，这一节就退化成两条前缀稳定性纪律 —— tools 按 name 排序、ledger 只放尾部 —— 它们代价近乎零，照做的理由是端点日后支持缓存时可以直接生效。

5. **"应该多久压缩一次，这个数字你能辩护吗？"** 分两层答。方法层：这个数字应该从价格比推，不是拍脑袋。数字层：本端点的价格比我还没实测，所以**结论是"待测"**。下面用来演示方法的量级取自厂商公开文档（cache read 约 0.1x 基础输入价，cache write 约 1.25x），不是本端点实测。稳态下一个命中缓存的轮次在前缀上花 ~0.1·P；压缩那一轮花 ~1.25·P′（未命中），之后是 ~0.1·P′。盈亏平衡在 `n > (1.25·P′ − 0.1·P) / (0.1·P − 0.1·P′)`；代入 P=100k、P′=40k 得到个位数轮次。结构性结论（压得太频会在输入成本上净亏，*即便上下文确实变小了*）成立，而 `min_steps_between_compactions=8` 和 0.80→0.40 的滞回只是占位默认值，等 C1/C2/C3 有结论后用本端点实测的比例重算。若 C2 判定本端点不产生缓存，这条推导整个作废：双阈值改由纯上下文预算推导（窗口由 C12 定），滞回保留 —— 它还在防压缩抖动。两个我会不用问就主动提的注意点：厂商文档里那个 5 分钟的 ephemeral TTL（同样是公开文档参考，本端点未测）意味着一个空闲的交互式会话反正会丢掉条目，所以这套算账只对连续的 agent 循环成立 —— 对有人参与的 CLI，空闲间隔占主导，你应该在空闲时（缓存本来就凉了）压缩，而不是在循环中途。还有，模型质量的论证方向与成本论证相反：注意力在远未触及硬上限之前就退化，所以如果质量才是约束条件，你应该比成本最优*更早*压缩，并且你得说清楚自己优化的是哪一个。

6. **"FileLedger 为什么要存在 —— 摘要里说一句哪些文件被编辑过不就行了？"** 因为摘要是 LLM 生成的，而文件状态是这个循环里唯一一个既有外部 ground truth、检查代价又便宜的东西。三个 ledger 能抓到而摘要抓不到的具体失败：(a) 摘要器写"修好了 util.py 里的空值检查"，而 `edit_file` 实际返回的是错误，模型再也不去重试；(b) `ReadTool` 在 `file_tools.py:147-148` 截断了一个大文件的中间部分，模型以为自己看了全部 —— ledger 的 `last_read_range` 说不是；(c) `bash_tool` 跑了 `sed -i` 或 `git checkout`，对话记录里任何地方都不出现 `write_file`，模型"持有"的那个文件是陈旧的 —— 被抓到，是因为 `render()` 从磁盘重新哈希，而不是重放自己的写入日志。这条设计规则可以推广：**任何具备外部 ground truth 的状态都应该被重新推导，而不是被摘要。** ledger 刻意只保 O(#文件) 的路径和哈希，绝不含正文，所以它维持在几百 token，可以每轮重发 —— 并且它放在尾部而非 system prompt，所以重发在缓存上不花任何代价。

## 前置条件

1. `mini_agent/agent.py:257-259` —— `_create_summary` 把未截断的 `msg.content` 插进摘要器 prompt（局部变量名 `result_preview` 有误导性），而 `mini_agent/agent.py:287-289` 的 `except` 又把那整个字符串当摘要返回。因此压缩会撑大上下文。这必须在本方案落地之前修掉或删掉，因为新的管理器复用同一条摘要器调用路径。

2. `mini_agent/agent.py:359-360` —— `self.api_total_tokens = response.usage.total_tokens` 存的是输入+输出，注释却声称在累加。触发条件需要的是 `usage.prompt_tokens`；保留现有字段给 `cli.py:245` 显示，但别再把它当作上下文度量。

3. `mini_agent/schema/schema.py:32-36` —— `TokenUsage` 需要先有 `cache_read_tokens` 和 `cache_creation_tokens` 字段，缓存 demo 才能报出任何东西；`mini_agent/llm/anthropic_client.py:240-241` 已经从响应里读了这两个值，目前把它们并进一个和里丢掉了。

## 明确不做

不做：(1) 工具结果*再水化* —— 生产系统允许模型按 `tool_call_id` 从旁路存储把被驱逐的结果要回来；我这里是单向驱逐，并告诉模型重新跑一次工具。(2) 子 agent 上下文隔离 —— 面对一次 200k token 的搜索，真正的答案是派生一个子 agent，其对话记录从不进入父级。(3) 语义/重要性排序的驱逐 —— 我按最旧优先、按体积驱逐，不用相关性模型。(4) 摘要与 ledger 的跨会话持久化。(5) 真正的 tokenizer —— `count_tokens` API 调用或 Claude 专用 BPE；tiktoken 的 cl100k 只用于增量。(6) 1 小时的 cache TTL 及其不同的写入价格。(7) `mini_agent/acp/__init__.py:127-165`，那份漂移出去的第二个循环，这些一样都不给。

对面试官的话："我砍掉的每一件都是*扩展性*特性 —— 更多存储、更多模型、更多进程。我砍掉的东西没有一件改变机制。真正难的三件事是配对不变量、选一个在缓存把输入 token 藏起来之后依然正确的触发信号、以及压缩与 prefix cache 想要的东西正好相反这一事实。这三件都做了，背后有属性测试和一张实测的成本表。再水化不过是在我本来就保留的驱逐记录上挂一个 dict 查表 —— 我把它留在外面，是因为造它并不会教给我任何驱逐路径没教过的东西。"

## 代码量

约 800 LOC：`context/manager.py` ~230、`context/ledger.py` ~150、`context/state.py` ~70、`context/cache.py` ~60、`agent.py` 净 ~−90（删掉 `_summarize_messages`/`_create_summary`，约 40 行钩子）、`anthropic_client.py` ~+40、`schema.py` ~+2、`cli.py` ~+5、`tests/test_context_invariants.py` ~180、`scripts/ctx_bench.py` ~150、`scripts/cache_probe.py` ~60。

## 工期

4 天。第 1 天：`state.py` + `manager.py` 的 tier 1–2 及边界规则，外加先写属性测试（它就是规格）。第 2 天：`ledger.py`、`agent.py:474` 的工具钩子、带截断和确定性回退的摘要器、把 `build_view` 接进 `agent.py:340`。第 3 天：`cache.py` + `anthropic_client.py` 的断点、`TokenUsage` 字段、线上缓存探针。第 4 天：`ctx_bench.py`，录制那份对话记录，产出成本表，写成文。
