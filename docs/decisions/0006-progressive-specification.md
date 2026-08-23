# ADR-0006：规格按实现地平线逐步展开，只维护中文版本

- 日期：2026-08-24
- 状态：已采纳（已实现）
- 关联：`docs/specs/README.md` · `docs/BUILD_LIST_CN.md` · 基线 commit `64ac75d`

## 背景

基线 commit `64ac75d` 新增 8,673 行文档，而排除 `mini_agent/skills/` 后，项目 Python 实现为 5,138 行。命令：

```bash
wc -l AGENTS.md CLAUDE.md docs/AGENT_ROADMAP_CN.md docs/BUILD_LIST_CN.md docs/PITFALLS.md docs/PROVIDER_CAPABILITIES.md docs/mechanisms.md docs/decisions/*.md docs/reference/*.md docs/reference/surveys/*.md docs/specs/*.md
rg --files mini_agent -g '*.py' -g '!mini_agent/skills/**' -0 | xargs -0 wc -l
```

问题不是文档多，而是实现尚未开始时已经维护七对等长中英规格，并把 checkpoint、subagent、PageRank 写到函数和 LOC 级。漂移已经发生：roadmap 仍要求 `AsyncIterator`，事件 spec 已否决它；ADR-0004 已取消 PageRank，对应 spec 却仍是完整实现方案。

## 选项

1. 保留全量中英实现规格，靠实现时逐份更新。
2. 只保留一页 roadmap 和源码注释，删除 specs/ADR/reference。
3. 中文单一维护版本；当前/下一阶段写实现级 spec，后期阶段只留不变量与进入条件；外部调研降为非规范证据。

## 决定

选 3。删除七份英文镜像；把 checkpoint 与 subagent 收敛为设计说明；删除已取消的 PageRank 实现方案；把 roadmap 改为只描述顺序，把 upstream audit 与状态表分开。

## 为什么否决其他的

**选项 1** 在设计稳定、需要同时服务中英文贡献者时是对的；当前没有实现作为锚点，双语只会把一次变化变成两次同步，并让旧设计看起来仍然有效。

**选项 2** 在短期原型里是对的；本项目的学习产出包含“为什么”，删掉 ADR、PITFALL 和外部调查会丢掉最有价值的推理证据。

## 怎么验证它是对的

- `docs/specs/` 不再有成对英文镜像；
- `README -> BUILD_LIST -> mechanisms -> specs/ADR` 的链接检查通过；
- 同一机制只有 `mechanisms.md` 持有状态；
- 后期设计说明不再包含预计 LOC、完整类签名或未经实现的 benchmark 数字。

## 回头看

本次整理已按上述边界落地。实现第一个机制后复查：如果短 spec 仍不足以开工，再用实际 diff 补充，而不是恢复全量预写。
