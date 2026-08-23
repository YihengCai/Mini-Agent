# Glob/Grep + 自描述截断

> 状态：待事务性编辑后实现。PageRank repo map 已由 ADR-0004 取消。

## 要解决的问题

仓库没有 Glob/Grep 工具，搜索只能走裸 bash；`BashOutputResult.format_content()`（`mini_agent/tools/bash_tool.py:32-49`）没有长度上限，失败路径还会把完整 stderr 经 `agent.py:470` 写进历史。静默截断会让模型把“展示了 N 条”误认为“总共 N 条”，因此是正确性 bug，不是 UI 问题。

## GlobTool

输入：pattern、可选起始目录、结果上限。要求：

- 路径始终限制在 workspace；
- 尊重 `.gitignore`、`.git/info/exclude` 和固定拒绝目录；
- 不跟随逃出 workspace 的符号链接；
- 在截断前完成排序；
- 输出使用 workspace-relative path；
- 截断页脚包含真实总数和排序方式。

有 `rg` 时使用 ripgrep；无 `rg` 时使用纯 Python fallback。两条后端必须对同一 fixture 返回相同路径集合。

## GrepTool

支持 `files_with_matches`、`content`、`count` 三种输出形状，默认只返回文件名。要求：

- 非法正则返回失败 ToolResult；
- 跳过二进制和超过配置上限的文件，并在结果中说明跳过数；
- content 模式包含路径、行号与有限上下文；
- 达到上限仍继续得到真实总匹配数，或明确写成“总数未知”；绝不能伪造总数；
- 截断文本固定包含 `[truncated: showing N of M matches]` 或等价的 unknown-total 版本。

## Bash 输出钳制

成功 stdout、成功 stderr、失败 `ToolResult.error` 三条路径使用同一个 `clamp_output()`。保留头部和尾部，中间插入省略说明；构建和测试的失败摘要常在末尾，不能只留头部。先做字符/行数快路径，再考虑 token 计量，避免为巨型输出先跑全量 tokenizer。

## 可失败工件

`tests/test_search_tools.py` 至少覆盖：

- rg 与 Python backend 结果集合一致；
- ignore、hidden、symlink 和 workspace confinement；
- 排序发生在截断之前；
- 截断页脚中的 N/M 与 fixture 相符；
- invalid regex、二进制和超大文件有可解释结果；
- 巨大 stdout 与巨大失败 stderr 都被钳制，且尾部错误摘要仍在。

## 明确不做

tree-sitter 符号索引、PageRank、embedding、文件监听、monorepo 分片。需要这些能力时，先用外部定位任务证明 Glob/Grep 的失败，再开新 ADR。
