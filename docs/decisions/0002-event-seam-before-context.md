# ADR-0002：先做事件缝，再做上下文管理

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：docs/specs/02-event-seam-interrupt_CN.md · docs/specs/01-context-manager_CN.md · docs/BUILD_LIST_CN.md「阶段 1：事件化执行内核」· ADR-0001 · docs/PITFALLS.md P-003

## 背景

A 档四件的技术依赖是弱的，但**工件**的依赖是硬的。旗舰模块（ADR-0001 的三层上下文）的招牌交付物是一张成本表：`peak_prompt` / `Σ prompt_tok` / `cacheable_prefix` / `cache_frac` 逐轮的数。这些数只能来自逐轮事件，而今天引擎唯一的输出通道是 TTY：`mini_agent/agent.py` 全文 30 处 `print(`（`grep -c "print(" mini_agent/agent.py` → `30`），其中压缩事件就是 `agent.py:180-183` 的两条 `print`。

耦合的账单已经产生了，不是推演：`acp/__init__.py:127-165` 是主循环的第二份拷贝，而且已经漂移 —— `grep -n "summarize\|logger.log_" mini_agent/acp/__init__.py` 返回空，即 ACP 那条路径既不压缩也不记日志。所以"先做上下文"意味着把压缩事件写成第 31、32 条 `print`，一周后为了 `ctx_bench.py` 能读到它们再全部重写一遍。

中断这条线也在同一处等着：`cli.py:775` 建了 task，`cli.py:778-781` 只轮询 `agent_task.done()` 并 `cancel_event.set()`，**从不调用 `.cancel()`**，于是 `cli.py:786` 的 `except asyncio.CancelledError` 是死代码；而前台 `bash` 的 timeout 上限是 600 秒（`bash_tool.py:328-329`）。

## 选项

1. **A —— 先事件缝（4 天），再上下文（4 天）**：`Agent(on_event=...)` + `ConsoleRenderer` 落地后，压缩层从第一天就发结构化事件。
2. **B —— 先旗舰**：上下文先做，事件缝随后补，压缩事件先用 `print` 顶着。
3. **C —— 两件并行**：两条线同时开工，最后合并。

## 决定

选 A。事件缝只做**最小解耦**，不做完整重构：`run()` 保住 `-> str` 契约（spec 02 §0 已否决 `AsyncIterator` 方案，理由是它把最难的中断修复交给了消费者的自觉，且会打断所有外部调用点 —— 实测 13 处：`cli.py:587`、`cli.py:775`、`tests/test_agent.py:66,147`、`tests/test_integration.py:107,194,231`、`examples/02_simple_agent.py:90,182`、`examples/03_session_notes.py:170,214`、`examples/04_full_agent.py:145,245`，每一处都写成 `result = await agent.run()`），引擎侧只新增一个 `await self._emit(event)`，渲染、截断（今天 `agent.py:460-463` 那个 300 字符上限）、ANSI 全部搬进 `ConsoleRenderer`。流式、steering、权限提示这三件挂在同一条缝上，但不在这一步做完。

## 为什么否决其他的

**B —— 先旗舰。** 表面理由是"先做最有价值的"，实际后果是把最贵的模块建在一个一周后要拆的地基上：压缩事件写成 `print`，`ctx_bench.py` 只能刮 stdout（而 stdout 里混着 ANSI 转义和 58 列宽的框，`agent.py:329-336`），成本表的可信度直接被"你怎么拿到这些数的"打掉。**什么条件下它反而是对的**：如果上下文管理的验收**只**依赖离线重放 —— `ctx_bench.py` 拿录制好的 transcript 喂给 `build_view`，根本不经过 `Agent.run()` —— 那顺序确实无所谓，这一半的确不需要事件。但另一半（`cache_probe.py` 的逐轮 `cache_read` / `cache_creation`）要的是运行期 usage，仍然要缝；而且真正的判据是：如果你打算展示的是"压缩让账单涨过"，你需要的是一条时间轴，不是一次快照。

**C —— 两件并行。** 两件都改 `agent.py` 的同一段循环体，撞车是确定的而不是概率的：事件缝要动 `agent.py:180-183`（压缩 print）、`agent.py:329-336`（58 列宽的 step 框）、`agent.py:381-387`（thinking / content print）、`agent.py:460-465`（工具结果 print 与那个 300 字符上限）；上下文要动 `agent.py:326`（触发点）、`agent.py:339`（`tool_list` 排序）、`agent.py:345`（改传 `build_view` 的结果）、`agent.py:360`（`note_usage`）、`agent.py:474`（ledger 钩子）。两份 diff 在 `run()` 里交错，合并冲突的解不是机械的 —— 要重新判断每一处到底该发事件还是该改视图。**什么条件下它反而是对的**：两个人分工，并且**先**把 `mini_agent/events.py` 的事件类型定死当契约（`CompactionStarted` / `CompactionFinished` 的字段先写死），那时两条线只在一个不会变的接口上相遇。一个人做，并行没有任何收益 —— 它只是把两件事的返工叠在一起。

**顺带否决：先做权限引擎（沙箱那件）。** BUILD_LIST_CN.md §3「必须成对」写得很清楚：权限提示**就是**事件缝的一次往返（引擎问、渲染器答、引擎阻塞等待）。先做权限，那个提示只能自己长一个 TTY 交互，UI 就分叉第三次 —— `acp/__init__.py:127-165` 已经把分叉的代价演示过一遍了。而且 `cli.py:754` 的 Esc 监听线程正以 cbreak 模式占着 stdin，提示要和它协调，这本身就是事件缝该解决的问题。

## 怎么验证它是对的

- `pytest tests/test_silent_mode.py -q`：`Agent(on_event=None)` 跑完一个脚本化任务，断言 `capsys` 捕到的 `stdout == ""`。这条测试永久杀死"print 又溜回引擎"这类回归。配套的静态判据：`grep -c "print(" mini_agent/agent.py` 今天是 **30**，落地后应为 **0**。
- `pytest tests/test_interrupt_leaves_no_orphan.py -q`：带对照断言 —— 新路径（给每个未满足的 `tool_use` 合成一条 `content="[interrupted by user before this tool completed]"` 的 tool 消息）让历史 **+1**；把今天的 `_cleanup_incomplete_messages`（`agent.py:73-94`，截断发生在 `agent.py:93`）拿来跑同一个 fixture 是 **−3**。两个整数就是全部论证。**具体数字待测**（取决于 fixture 里那一轮有几个工具调用）。
- **顺序本身的验收判据**：`scripts/ctx_bench.py` 的数据源必须是 `events.jsonl`，不是 `subprocess` 捕获的 stdout。如果写到一半发现要解析 stdout，说明顺序选错了 —— 这是这条 ADR 唯一可证伪的地方。
- 现场演示：运行中插话、按 Esc，然后 `pgrep -f sleep` 返回空。`pgrep` 才是重点 —— 它证明子进程被杀了而不是变成孤儿（今天 `bash_tool.py:398-409` 只在 `asyncio.TimeoutError` 分支 `process.kill()`，缺 `except asyncio.CancelledError: process.kill(); raise`）。
- **待测**：事件缝落地后 `acp/__init__.py:127-165` 能净删掉多少行（换成一个 `AcpSink`）。这个数是"我找到了缝"最直接的证据。

## 回头看

> 待实现后补。
