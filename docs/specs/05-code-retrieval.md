# Glob/Grep + 显式截断元数据

> 状态：待事务式编辑后实现。PageRank repo map 已由 ADR-0004 取消。

## 要解决的问题

仓库没有 Glob/Grep 工具，搜索只能走原始 bash；`BashOutputResult.format_content()`（`mini_agent/tools/bash_tool.py:32-49`）没有输出限制，错误路径还会把完整 stderr 经 `agent.py:470` 写入消息历史。静默截断会让模型把“展示 N 条”误认为“总共 N 条”，因此是正确性问题，不只是 UI 问题。

## GlobTool

输入：pattern、可选根目录、结果上限。要求：

- 路径始终限制在工作区；
- 遵守 `.gitignore`、`.git/info/exclude` 和固定排除目录；
- 不跟随逃出工作区的符号链接；
- 在截断前完成排序；
- 输出使用相对工作区的路径；
- 截断元数据包含真实总数和排序方式。

有 `rg` 时使用 ripgrep；无 `rg` 时使用纯 Python 降级实现。两个后端必须对同一测试样例返回相同的路径集合。

## GrepTool

支持 `files_with_matches`、`content`、`count` 三种输出形式，默认只返回文件名。要求：

- 无效正则表达式返回错误 `ToolResult`；
- 跳过二进制文件和超过大小限制的文件，并在结果中说明跳过数量；
- `content` 模式包含路径、行号与有限的上下文行；
- 达到上限后继续计算真实总数，或明确标记 `total unknown`；不能伪造总数；
- 截断结果固定包含 `[truncated: showing N of M matches]` 或等价的总数未知元数据。

## Bash 输出限制

成功 stdout、成功 stderr 和错误 `ToolResult.error` 使用同一个 `clamp_output()`。保留开头和结尾，中间插入截断元数据；构建或测试的失败摘要常在结尾，不能只保留开头。先使用按字符和行数处理的快速路径，再考虑 token 统计，避免先对巨大输出运行完整 tokenizer。

## 验证

`tests/test_search_tools.py` 至少覆盖：

- rg 与 Python 后端的结果集合一致；
- 忽略规则、隐藏文件、符号链接和工作区边界；
- 排序发生在截断之前；
- 截断元数据中的 N/M 与测试样例相符；
- 无效正则表达式、二进制文件和超大文件有明确结果；
- 巨大的 stdout 与错误 stderr 都被限制，且结尾的错误摘要仍保留。

## 不在范围内

tree-sitter 符号索引、PageRank、embedding、文件监视器、monorepo 分片。需要这些能力时，先用外部代码定位任务证明 Glob/Grep 的不足，再开新 ADR。
