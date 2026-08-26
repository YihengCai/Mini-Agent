# 模型可见工具输出预算

> 状态：已实现。预算函数位于 `mini_agent/core/tool_output.py:6-72`，统一挂钩位于 `mini_agent/core/tool_execution.py:114-132`；离线回归位于 `tests/test_tool_output_budget.py:99-207`，取舍见 [ADR-0010](../decisions/0010-model-facing-tool-output-budget.md)。

## 问题证据

`BashOutputResult` 会把 stdout 与 stderr 原样拼入 `content`（`mini_agent/tools/bash_tool.py:32-49`），MCP、skill 与批次执行器生成的失败也都可能产生无界文本。此前 `ToolBatchExecutor` 把成功 `content` 或 `Error: {error}` 直接写入模型消息；离线探针构造 200,000 字节的 Bash 结果后，实测 `content_bytes 200000`。

## 本轮不变量

1. 工具返回的 `ToolResult` 与先发出的 `ToolFinished` 表示原始执行事实，保持完整且互不共享可变对象；只有模型消息投影受预算约束。
2. 成功内容与失败文本共用一个入口；失败先拼接既有 `Error: ` 前缀，再计算完整模型文本的 UTF-8 字节数。
3. 每条模型工具消息最多 64 KiB；等于边界时逐字不变，超限时保留合法 UTF-8 的首尾，并在中间写入原始、保留、省略和上限字节数。标记本身计入上限。
4. Session 历史、下一次 `ModelRequest` 与 adapter 收到同一个有界视图；调用标识符、工具名、批次顺序和一项一结果的配对不变。
5. `get_skill` 的模型描述不再承诺超大结果必然完整；直接调用工具仍返回原始 `ToolResult`。

## 不在范围

本轮不限制工具执行期间的内存、`ToolFinished`、终端或日志，不限制整批工具消息的合计大小，也不实现按工具分页、持久化超限正文、自动摘要、流式输出、token 估算或上下文压缩。被 Bash 增量游标推进后又从模型视图省略的正文当前不可恢复；截断标记不暗示可以续读。

## 离线验证

- `.venv/bin/python -m pytest -q tests/test_tool_output_budget.py tests/test_tool_execution.py tests/test_skill_tool.py` 实测 `26 passed in 0.35s`。
- `.venv/bin/python -m pytest -q -m 'not external'` 实测 `230 passed, 9 deselected in 13.57s`。
- 暂时把 `mini_agent/core/tool_execution.py:128` 改成直接使用 `model_content` 后，集成回归实测 `1 failed, 1 warning in 0.44s`；恢复挂钩后同项为 `1 passed, 1 warning in 0.35s`。
