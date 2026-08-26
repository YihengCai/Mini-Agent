# ADR-0031：调用标识符只约束未完成工具批次

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/tool_execution.py`、`tests/llm_test_double.py`、`tests/test_tool_execution.py`、ADR-0008、P-009

## 背景

提交 `a26e57f` 的 `ToolBatchExecutor` 持有一个永不清空的 `_claimed_call_ids`：每项执行前写入，后续任意 Step 或 Turn 出现相同值都会在副作用前失败（`git show a26e57f:mini_agent/core/tool_execution.py | nl -ba | sed -n '60,160p'`）。严格 LLM 测试替身也把调用标识符解释为完整历史唯一（`git show a26e57f:tests/llm_test_double.py | nl -ba | sed -n '30,70p'`）。

这个集合只拦截“相同 ID 再次出现”，模型改用新 ID 就能重复完全相同的工具副作用；它也没有持久化、恢复或与副作用原子提交的边界，进程退出后状态消失。调用标识符在现有内部 contract 中只用于把一个 assistant 工具调用与对应结果关联，不能充当幂等键或 at-most-once（最多执行一次）证据。

## 选项

1. **保留 Session 级隐藏集合**：继续保守拒绝相同 ID，但维持无界、不可恢复且可被换 ID 绕过的状态。
2. **把集合改成 Turn 级并定期清空**：限制增长，却为标识符发明当前协议没有提供的 Turn 作用域。
3. **只约束未完成批次**：生产预检拒绝同批空值、非法类型和重复值；完成配对后允许复用，不宣称副作用去重。

## 决定

采用选项 3。删除执行器的 `_claimed_call_ids`、逐项写入和历史重复分支；`_validate_batch()` 继续在首个副作用前检查调用类型、非空 ID 和批内重复。测试替身只要求未完成调用的 ID 唯一，并继续拒绝缺失、未知或重复结果；一组调用完成配对后，同一 ID 可以用于后续批次。

本项局部推翻 ADR-0008 的 Session 账本与相关取消认领语义。冻结工具注册、模型响应接纳快照、完整批次预检、串行执行、工具结果接纳快照、错误归一、事件顺序和 assistant/结果成组提交全部不变。真正的重复副作用防护必须从可持久事实、幂等 contract 或检查点 topic 进入，不能由模型生成的相关键代替。

## 为什么否决其他的

**否决 Session 级集合**：它为简单相关键建立第二份隐藏状态，却没有形成可恢复的执行事实。若协议明确保证标识符跨 Session 全局唯一、工具以该值作为幂等键，并且“已接纳/执行中/已完成”能与副作用原子持久化，这个集合才可能成为完整方案的一部分。

**否决 Turn 级集合**：清空时机更明确但语义仍是项目自行猜测，且不能阻止同一 Turn 内换 ID 重放。若未来 wire contract 明确把标识符唯一性限定在 Turn，并且 adapter 能一致验证，这个范围才合理；当前自然边界是一个尚未完成配对的工具批次。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_tool_execution.py::test_completed_call_id_can_be_reused_across_steps_and_turns tests/test_tool_execution.py::test_duplicate_ids_reject_the_batch_before_any_side_effect tests/test_agent_loop_offline.py::test_scripted_llm_rejects_invalid_tool_call_pairs` 实测 `6 passed in 0.51s`。
- 正向回归在同一 Session 跨两个 Step 与下一 Turn 三次复用 `reused`，三次副作用都发生，随后测试替身重新扫描完整历史；临时恢复生产账本时实测 `1 failed in 0.35s`，恢复测试替身的全历史集合时实测 `1 failed in 0.34s`。
- `.venv/bin/python -m pytest -q tests/test_tool_execution.py tests/test_agent_loop_offline.py tests/test_agent_session_offline.py tests/test_tool_output_budget.py` 实测 `67 passed in 0.73s`。
- `.venv/bin/python -m pytest -q` 实测 `273 passed, 8 deselected in 13.76s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

生产代码净减 11 行，测试净减 108 行；四项账本专用负向测试和专用取消工具由一个正向跨 Step/Turn 回归替代。同批重复 ID 的零副作用回归继续保留，因此删除的是未经协议支持的跨批状态，不是批次结构边界。
