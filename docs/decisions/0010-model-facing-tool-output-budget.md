# ADR-0010：在模型消息投影处约束工具输出

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/core/tool_output.py:6-72`、`mini_agent/core/tool_execution.py:114-132`、`tests/test_tool_output_budget.py:99-207`、提交 `07e272b`、[P-012](../PITFALLS.md)

## 背景

工具执行结果此前在 `ToolBatchExecutor` 中先以完整快照发出 `ToolFinished`，随后把成功 `content` 或失败 `Error: {error}` 原样写入模型消息（提交 `07e272b^` 的 `mini_agent/core/tool_execution.py:113-130`）。运行 `.venv/bin/python -c 'from mini_agent.tools.bash_tool import BashOutputResult; r=BashOutputResult(success=True, stdout="x"*200000, stderr="", exit_code=0); print("content_bytes", len(r.content.encode()))'` 实测 `content_bytes 200000`。

统一预算既要覆盖 Bash、MCP、note、skill、新工具和批次执行器自己生成的异常，又不能把供宿主观察与诊断的原始事实改写成截断文本。`read_file` 已把编号正文限制为 50 KiB，并把续读提示放在尾部（`mini_agent/tools/file_tools.py:12-14,372-385`）；Bash 的 `bash_id` 与 `exit_code` 也在尾部（`mini_agent/tools/bash_tool.py:32-49`）。

## 选项

1. **各工具自行截断**：可以使用行、分页或游标语义，但会漏掉 MCP、新工具和批次执行器生成的错误，并可能改变宿主看到的原始结果。
2. **在 `ToolResult` 层统一截断**：入口较早，但把执行事实与模型视图混为一体，事件、日志和可信宿主都会失去原文。
3. **在批次执行器的模型消息投影处统一截断**：先保留原始事件，再约束写入 Session 的模型视图；所有当前 adapter 共用同一结果。
4. **在每个 adapter 编码时截断**：最接近 wire，但 Session 仍保存无界内容，两个 adapter 还会复制同一策略。

## 决定

采用选项 3。`ToolBatchExecutor` 先发完整 `ToolFinished`，再形成当前成功或失败文本，并调用纯函数生成模型消息（`mini_agent/core/tool_execution.py:114-132`）。每条消息的硬上限为 64 KiB UTF-8 字节；这是高于既有 50 KiB `read_file` 正文及其尾部提示的本地预算，不是 vendor token 或端点上下文结论。

超限文本保留首尾，使用中间标记报告 `original_bytes`、`retained_bytes`、`omitted_bytes` 与 `limit_bytes`；标记计入上限，多字节字符只在合法边界保留（`mini_agent/core/tool_output.py:10-69`）。本决定只约束单条模型工具消息，不约束原始事件、日志、工具内部缓冲或整批合计大小。

## 为什么否决其他的

**否决各工具自行截断**：它不能覆盖未知工具、异常 traceback、非法返回与未来忘记接入策略的工具；Bash 增量读取还会让“工具已推进游标、模型却没收到全文”变成隐式行为。若某个工具拥有可验证的分页、持久化正文或续读 contract，工具专属预处理反而适合在统一硬上限之前保留领域语义。

**否决 `ToolResult` 层统一截断**：`ToolResult` 是执行结果，`ToolFinished`、CLI 日志和可信宿主都依赖它；在这里裁剪会让诊断事实与真正执行结果不一致。若系统明确规定所有消费者都只能看到有界结果，并另有原始正文的持久化所有权，这一层反而可能同时控制内存。

**否决 adapter 层截断**：它发生在 Session 已持有无界历史之后，无法控制下一 Step 前的内部消息，并会让两个 wire adapter 重复策略。若不同协议或端点已有实测且确实需要不同 wire 限额，adapter 可再施加更小的协议上限，但不能替代 core 的共同边界。

**没有只保留前缀**：`read_file` 的下一次 offset 和 Bash 的进程元数据在尾部，纯前缀会稳定丢掉控制信息。若输出只有开头具有语义，或正文已持久化且标记能提供可恢复引用，前缀策略反而更简单。

## 怎么验证它是对的

- 3 项新增回归覆盖精确边界、首个溢出、UTF-8、多字节合法性、成功与失败、截断元数据、原始事件、观察者变异、Session 历史、下一次模型请求和批次顺序（`tests/test_tool_output_budget.py:99-207`）。
- 定向命令实测 `26 passed in 0.35s`；显式排除 `external` 的完整集合实测 `230 passed, 9 deselected in 13.57s`。
- 暂时删除 `ToolBatchExecutor` 的截断挂钩后，`test_batch_keeps_raw_events_but_bounds_history_and_next_request` 实测转为 `1 failed, 1 warning in 0.44s`；恢复后为 `1 passed, 1 warning in 0.35s`。

## 回头看

实现本身只需要一个纯函数和批次执行器的单点挂钩，没有修改 `ToolResult` 或 adapter。审计却推翻了“统一边界不影响具体工具 contract”的初始假设：`get_skill` 原本向模型承诺 `complete content`，因此同步改成明确提示超大模型可见结果可能截断（`mini_agent/tools/skill_tool.py:23-28`），见 [P-012](../PITFALLS.md)。

当前首尾策略能保留常见尾部控制信息，但没有让被省略正文可恢复；整批调用仍可通过多条各自不超过 64 KiB 的结果累积成大请求。这两项必须分别有持久化引用/分页与批次预算证据后再进入，不能把本决定描述成完整上下文管理。
