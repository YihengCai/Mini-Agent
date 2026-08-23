# ADR-0006：实现规格按阶段逐步展开，只维护中文

- 日期：2026-08-24
- 状态：已采纳（已实现）
- 关联：`docs/specs/README.md` · `docs/BUILD_LIST.md` · baseline 提交 `64ac75d`

## 背景

在 baseline 提交 `64ac75d` 的工作树中，新增文档共 8,673 行；排除 `mini_agent/skills/` 后，Python 实现为 5,138 行。测量命令：

```bash
wc -l AGENTS.md CLAUDE.md docs/AGENT_ROADMAP_CN.md docs/BUILD_LIST_CN.md docs/PITFALLS.md docs/PROVIDER_CAPABILITIES.md docs/mechanisms.md docs/decisions/*.md docs/reference/*.md docs/reference/surveys/*.md docs/specs/*.md
rg --files mini_agent -g '*.py' -g '!mini_agent/skills/**' -0 | xargs -0 wc -l
```

问题是七对中英规格在实现前已经展开到类、LOC 和基准测试层级，并且路线图、规格与 ADR 之间出现偏差。

## 选项

1. 继续维护完整的中英双语规格。
2. 只保留路线图和代码注释，删除规格、ADR 和外部调研。
3. 只维护中文；当前和下一阶段写实现规格，后期只写不变量与进入条件。

## 决定

选择选项 3。删除对应的英文版本；检查点和 subagent 只保留设计说明；取消 PageRank 实现规格；`BUILD_LIST` 只记录顺序，`mechanisms` 单独记录状态，外部调研只作为证据。

## 为什么否决其他的

完整双语适合设计已经稳定且需要双语协作者的项目；当前会产生同步成本和过期设计。

只保留路线图适合短期原型；本项目的学习结果还包括 ADR、PITFALL 与外部证据，不能删除。

## 怎么验证

- 实现规格没有另一种语言的对应版本；
- 状态只在 `mechanisms.md` 维护；
- README → BUILD_LIST → mechanisms → 规格与 ADR 的链接全部可解析；
- 后期设计说明不包含未经实现的类、LOC 估算或基准测试数字。

## 回头看

第二轮整理进一步删除个人项目不需要的 CONTRIBUTING/CODE_OF_CONDUCT、重复的英文调研原稿和 `_CN` 文件后缀，并保留一份中文综合调研，避免丢失外部证据。第三轮根据用户反馈修正规则：已有成熟译法的词使用中文；只有名称、标识符和没有稳定译法的专有概念保留英文。规则已经写入 `AGENTS.md`。
