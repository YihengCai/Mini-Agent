# ADR-0004：删除 PageRank repo map，只保留 Glob/Grep + 截断元数据

- 日期：2026-08-24
- 状态：已采纳（未实现）
- 关联：`docs/specs/05-code-retrieval.md` · `docs/BUILD_LIST.md` 阶段 3

## 背景

baseline 没有 Glob/Grep 工具，搜索依赖 bash；`BashOutputResult.format_content()`（`bash_tool.py:32-49`）没有硬限制，错误路径还可把完整 stderr 写入消息历史。原计划同时实现 tree-sitter 符号提取、文件图和 PageRank，但没有外部代码定位评测能证明排序质量。

## 选项

1. 完整的 aider 风格 repo map + personalized PageRank。
2. Glob/Grep + 显式截断元数据 + bash 首尾截断。
3. Embedding 检索。
4. subagent 扇出搜索。

## 决定

选择选项 2。Glob/Grep 遵守忽略规则与工作区边界；截断结果必须报告已展示数量和总数；bash 的成功与错误输出共用首尾截断限制。取消 PageRank 实现，但保留外部调研。

## 为什么否决其他的

PageRank 机制并不简单，但自己编写的标准答案无法独立证明排序质量。存在外部代码定位基准测试，或者产品只能构建一次静态索引而不能让 agent 主动搜索时，它更合适。

Embedding 检索能解决词汇不匹配，但代码编辑会造成索引过期和写放大。以读取为主的跨仓库搜索更适合它。

subagent 扇出能使用任务上下文搜索，但会增加延迟和 token 消耗，并依赖事件、上下文和权限机制。大型仓库且父级上下文隔离已经稳定时再考虑。

## 怎么验证

- ripgrep 与 Python 降级实现对同一测试样例返回相同路径集合；
- 排序在截断前执行，已展示数量和总数与测试样例一致；
- 无效正则表达式、二进制文件与超大文件返回明确元数据；
- 巨大的 stdout 和错误 stderr 都保留结尾的失败摘要。

## 回头看

> 待实现后补。
