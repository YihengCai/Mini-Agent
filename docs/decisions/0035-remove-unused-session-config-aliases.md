# ADR-0035：删除无消费者的 Session 配置别名

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/agent.py`、`tests/test_agent_session_offline.py`

## 背景

`AgentSession.llm` 与 `AgentSession.max_steps` 只是返回 `_llm` 与 `_max_steps` 的只读属性。对提交 `5c00708` 执行 `git grep -nF 'session.llm'`、`git grep -nF 'agent_session.llm'`、`git grep -nF 'session.max_steps'` 和 `git grep -nF 'agent_session.max_steps'`，生产代码没有消费者；唯一命中是测试给 `session.max_steps` 赋值并期待属性阻止写入。CLI 实际读取的是配置对象，agent loop 使用 Turn 接纳时的私有快照。

`active_turn` 还在读取时检查已完成句柄并调用 `_release_turn()`，但 runner 的 `finally` 已无条件执行同一释放，完成与失败回归都要求等待后状态为空（`mini_agent/core/agent.py:158-188`；`tests/test_agent_session_offline.py`）。这条惰性清理没有独立可达责任。

## 选项

1. **保留便利别名**：让未来宿主可以读取模型对象和 Step 预算，但继续维护没有当前用途的公开 contract。
2. **新增只读 Session 配置对象**：把模型、预算与工具视图统一成一个公开快照。
3. **只保留已消费观察面**：删除两个别名与重复清理；内部接纳快照不变，`tools`、`session_id`、`active_turn` 和历史接口保留。

## 决定

采用选项 3。公开构造参数不变；活动 Turn 继续固化模型、预算与工具执行器，但这些执行配置不再通过 `llm` 或 `max_steps` 二次公开。`active_turn` 只返回 runner 持有的句柄，释放仍由 runner 的 `finally` 单一负责。

本轮不修改 CLI、工具接口或执行器，也不删除 CLI 确实用于统计的 `tools` 观察面。

## 为什么否决其他的

**否决保留便利别名**：没有消费者就无法判断返回原对象是否是正确 contract；尤其模型对象可变，暴露它只会暗示宿主可以在运行中替换配置。若真实第二宿主需要显示预算或模型身份，应按它实际需要的不可变值新增最小观察接口。

**否决新增配置对象**：它会把两个无调用的属性扩张成新类型，却没有解锁当前研究 topic。若以后需要持久化、恢复或多客户端检查完整 Session 配置，且字段语义已经由回归固定，独立快照才更合适。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_agent_session_offline.py tests/test_agent_loop_offline.py tests/test_config_provenance.py -m 'not external'` 实测 `49 passed in 0.80s`。
- 回归直接替换 `_llm`、`_max_steps` 与 `_tool_executor`，证明活动 Turn 仍使用接纳快照；等待完成、失败和取消后的既有断言继续固定 runner 释放。
- 临时恢复 `llm` 属性后，缩小公开面的回归实测 `1 failed in 0.53s`；还原后两项关键回归为 `2 passed in 0.75s`。
- `.venv/bin/python -m pytest -q` 实测 `271 passed, 8 deselected in 14.02s`；外部模型、MCP 与网络测试本次未运行。

## 回头看

最终生产代码净减 10 行，测试为锁定缩小后的公开面净增 2 行。完整离线集合保持通过；CLI 仍通过 `tools` 与 `active_turn` 获得实际需要的观察信息，未出现新增配置快照对象的理由。
