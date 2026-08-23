# ADR-0001：用三层上下文管理器替换 prose 摘要器

- 日期：2026-08-24
- 状态：已被 ADR-0007 取代
- 关联：docs/specs/01-context-manager_CN.md · docs/BUILD_LIST_CN.md「阶段 2：上下文与 cache」· docs/AGENT_ROADMAP_CN.md「上游基线审计」· docs/PITFALLS.md P-002 · docs/mechanisms.md「分层上下文」行 · ADR-0007

## 背景

现有压缩器是 `Agent._summarize_messages`（`agent.py:153-232`）加 `_create_summary`（`agent.py:235-292`），循环每步调一次（`agent.py:326`）。它把历史就地重建成 `system + [user_i + LLM 散文摘要_i]`，三个硬伤都在代码里：

1. **in-flight 轮次被摘要。** `agent.py:204-207`：最后一个 user 之后 `next_user_idx = len(self.messages)`，于是 `agent.py:210` 的 `execution_messages` 吃掉正在执行的这一轮。step 17 刚 `read_file` 拿到的带行号正文，在 step 18 变成一句"Assistant 读了 file.py"；step 19 的 `edit_file` 已经没有逐字来源。
2. **不幂等。** 摘要以 `role="user"` 插入（`agent.py:217`），下一次压缩在 `agent.py:186` 用 `msg.role == "user"` 重新收集轮次边界，把自己上一轮的产物当成新的 user 轮次。压缩次数越多，"user 消息"越多，`_create_summary` 的串行调用次数随之增长。
3. **摘要 prompt 用未截断的工具输出。** `agent.py:258-259` 的局部变量叫 `result_preview`，赋的是完整 `msg.content`，尾巴上那个 `...` 是拼进 f-string 的装饰。失败路径 `agent.py:289-292` 直接 `return summary_content` —— 把整份未截断的对话记录当摘要塞回去。**压缩可以让上下文变大。**

触发信号同样是坏的：`agent.py:174` 把 tiktoken 估算和 `api_total_tokens` OR 在一起，而 `agent.py:360` 是赋值不是累加；`token_limit` 默认 `80000`（`agent.py:28`），`cli.py:569-574` 和 `acp/__init__.py:102` 两个构造点都不传，`config.py` 里没有这个字段。

## 选项

1. **A —— 三层派生视图**：`build_view(raw_log, state) -> list[Message]` 是纯函数；tier 1 按 `tool_call_id` 重写 tool 消息 content（不删消息），tier 2 单条单调前进的窗口摘要且边界绝不切进 tool 组，tier 3 FileLedger 逐字挂在视图尾部。代价：约 800 LOC / 4 天，`self.messages` 的语义从"发出去的东西"变成"只追加的 raw log"，全仓库对它的读取点都要重新审。
2. **B —— provider 侧上下文管理**：用 Anthropic API 的 `context_management` / `clear_tool_uses_20250919`（AGENT_ROADMAP_CN.md §2.2 记录的对标项），服务端清旧 tool 结果、保留 `tool_use`/`tool_result` 骨架。代价：机制在别人进程里，只覆盖一条 provider 路径。
3. **C —— 只保留最近 K 条，不做摘要**：截断式滑窗，零 LLM 调用。代价：K 是硬边界，落进并行 tool 组就产生孤儿。
4. **D —— 语义/重要性排序驱逐**：用相关性给工具结果打分，先驱逐"不重要"的。代价：需要一个排序模型和一套能证明排序有效的离线评测。
5. **E —— tool 结果回填（re-hydration）**：驱逐时把原文写进旁路存储，给模型一个按 `tool_call_id` 取回的工具。代价：多一个存储、多一个工具、多一条失效路径。

## 决定

选 A。边界写死：驱逐是**单向**的（不做 E 的回填，模型要正文就重新跑工具）；只有**一份**摘要，`upto_index` 只增不减；FileLedger 只存路径 / sha / 计数，**永不含文件正文**，且注入位置是视图尾部而不是 system prompt；tokenizer 不换（tiktoken 的 cl100k 只用于"自上次精确测量以来"的增量，水位由 `usage.prompt_tokens` 承担）；不做跨会话持久化；不碰 `acp/__init__.py:127-165` 那份漂移出去的第二循环。

## 为什么否决其他的

**B —— provider 侧上下文管理。** 三个理由。(a) 覆盖面：本仓库 `anthropic_client.py:27` 的默认 `api_base` 是 `https://api.minimaxi.com/anthropic`，`openai_client.py` 那条路径根本没有对应能力，于是"上下文管理"变成一个只在某个端点上成立的属性。(b) 它管不到最贵的那半 —— cache 断点的放置、tools 数组的排序稳定性、FileLedger 这种"有外部 ground truth 就该重新推导而不是摘要"的状态，全在客户端。(c) 作品视角：机制在服务端，我什么都讲不出来。**什么条件下它反而是对的**：只面向某个提供 server-side 上下文管理的端点、且交付物不需要一张自己算的成本表 —— 那时它是更少代码、更少 bug 的正确选择，我会直接用它，并把省下的 4 天花在事务性编辑上。

**C —— 只保留最近 K 条，不做摘要。** 它是那个"平凡正确"的答案，代价全在别处：任务意图和早期约束一起被丢掉，模型在 step 40 重新发明 step 3 已经排除的方案。而且固定 K 不是安全的切点 —— 一条 assistant 带 3 个 `tool_use` 对应 3 条 tool 消息，K 落进组内会双向产生孤儿。**什么条件下它反而是对的**：工具结果都小、没有并行工具调用、任务是短程的（十步以内）—— 那时摘要的 LLM 调用是纯成本，滑窗更省更稳。

**D —— 语义/重要性排序驱逐。** 它把一个免费、确定性的操作换成一个要花钱、不确定、还需要自证有效的操作。而按最旧优先 + 按体积驱逐已经拿走了绝大部分收益，因为缓存失效由**最早**被改动的块决定 —— 排序再聪明，只要动的是同一条远古结果，作废的前缀一样多。**什么条件下它反而是对的**：配上 E 的回填做兜底（排错了还能取回来），并且有一套离线评测能给出"排序 vs 最旧优先"的 recall 差值 —— 没有这两样，它只是把不确定性引进了唯一一条必须确定的路径（`build_view` 的输出要逐字节可复现，否则缓存不可能命中）。

**E —— tool 结果回填（re-hydration）。** 这是生产系统真会做的事，砍掉它是刻意的：驱逐记录里已经有 `tool_call_id`，回填不过是在上面挂一个 dict 查表加一个新工具 schema。它**不改变任何机制** —— 配对不变量、边界规则、触发信号、缓存断点，一条都不动 —— 所以造它学不到驱逐路径没教过的东西。**什么条件下它反而是对的**：任务需要回看远古逐字结果（长审计、跨几十步的对账），或者上下文预算紧到必须激进驱逐、误驱逐率高到需要撤销键。

**顺带否决：递归/分层摘要（摘要的摘要）。** 每次压缩同时翻倍成本和退化，而本仓库已经把这个失败演示完了（`agent.py:217` 写 `role="user"`，`agent.py:186` 又收回来）。

## 怎么验证它是对的

- `pytest tests/test_context_invariants.py -q`：随机 raw log × `keep_last_k` 1..30 × {只驱逐, 只摘要, 两者}，断言 (a) 无孤立 `tool_use` / `tool_result`，(b) 无空 content block，(c) 同一 state 下 `build_view` 调两次逐字节相同。并打印"边界被迫下滑的比例"——这个数证明边界规则在真干活而不是摆设。**通过数与下滑比例：待测。**
- `python3 scripts/ctx_bench.py`：录制一份 transcript 离线重放，输出 `none` / `current`（今天 `agent.py:153` 那个）/ `three_tier` / `three_tier_no_hysteresis` 四行，列 `peak_prompt`、`Σ prompt_tok`、`Σ cacheable_prefix`、`cache_frac`。要看的是 `current` 与 `three_tier` 的**峰值接近而 cache_frac 拉开**这一对数字。**全部待测。**
- `python3 scripts/cache_probe.py --steps 10`：需 API key。逐轮打印 `cache_creation_input_tokens` / `cache_read_input_tokens`（数据源 `anthropic_client.py:240-241`）。注意默认端点是 MiniMax（`anthropic_client.py:27`），它可能忽略 `cache_control` —— 那时 `cache_creation_input_tokens` 恒为 0，脚本必须在第 2 轮后打警告，而不是画一张什么都没测的图。**待测。**
- 回归钉子：`anthropic_client.py:240-244` 把 `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` 求和成 `prompt_tokens` —— 这是本仓库里唯一正确的"上下文大小"度量，也是缓存开启后仍然成立的触发信号；写一个测试钉住它，防止有人"简化"掉。同时 `schema.py:40-45` 的 `TokenUsage` 只有 `prompt_tokens` / `completion_tokens` / `total_tokens` 三个字段，两个 cache 字段被折进和里丢掉了，所以**今天成本不可测**——这两个字段是成本表的前置条件。

## 回头看

> 待实现后补。
