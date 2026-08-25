# ADR-0014：配置与 core 共同拒绝非正 Step 预算

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/config.py`、`mini_agent/core/agent.py`、`tests/test_llm_adapters.py`、`tests/test_agent_session_offline.py`、提交 `768dd64`

## 背景

`max_steps` 的用户可见含义是一个 Turn 允许的 agent 模型请求数，Step 本身也定义为一次模型请求及其工具批次（`docs/specs/01-core-agent-loop.md`）。原 `AgentConfig` 和 `AgentSession` 却都接受 `0` 与负数；循环的 `range(1, max_steps + 1)` 不执行任何 Step，仍返回 `stop_reason="max_steps"`（`git show 768dd64^:mini_agent/core/agent.py | nl -ba | sed -n '91,121p;161,216p;229,286p'`）。

改动前用 `max_steps=0` 构造 Session 并提交输入，实测输出为 `workspace_created=True`、`stop_reason='max_steps'`、`model_requests=0`、`history_roles=['system', 'user']`。这让一个已经接纳用户事实、却没有任何 Step 的 Turn 看起来只是正常耗尽预算，也让无效配置先产生文件副作用。

## 选项

1. **只在 YAML 配置模型约束正数**：CLI 会较早失败，但程序化构造 `AgentSession` 仍能绕过。
2. **只在 `AgentSession` 构造时检查**：所有执行最终安全，但配置错误要等到模型客户端、manager 和工具组装之后才暴露。
3. **配置入口和公开 core 入口各自验证同一不变量**：配置尽早给出字段错误，core 不信任调用者来源，并在自己的任何副作用前失败。

## 决定

采用选项 3。`AgentConfig.max_steps` 使用 `Field(default=50, gt=0)`；`AgentSession.__init__` 在生成身份、读取工具元数据和创建工作区之前执行同步 `max_steps <= 0` 检查（`mini_agent/config.py:41-46`；`mini_agent/core/agent.py:91-114`）。两层都只约束正数，不改变合法预算的 Step 计数与 `max_steps` 停止原因。

本轮不合并负重试次数、MCP 超时或其他数值语义，也不改变 Pydantic 的现有类型转换。

## 为什么否决其他的

**否决只校验配置**：`AgentSession` 是公开 core 入口，离线测试和未来非 CLI 宿主都会直接构造；把安全性寄托在某一个组装器上会让 core contract 不完整。若 Session 只能由一个私有、已验证工厂创建且类型系统能禁止绕过，单层配置校验反而足够。

**否决只校验 Session**：它能阻止伪 Turn，却让配置加载表面成功，CLI 还会先构造模型客户端与 runtime 资源，错误位置和字段归属都更差。若 `max_steps` 只能在运行中动态计算、配置文件不直接表达它，core 单层校验反而是唯一真源。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_llm_adapters.py tests/test_agent_session_offline.py tests/test_agent_loop_offline.py -m 'not external'` 实测 `76 passed in 0.60s`。
- YAML 与直接构造分别用 `0`、`-1` 验证；Session 失败后目标工作区不存在，模型请求记录为空。
- 临时移除配置约束时 YAML 回归实测 `1 failed in 0.34s`；临时移除 core 守卫时直接构造回归实测 `1 failed in 0.73s`。
- 显式排除 `external` 的完整集合实测 `261 passed, 9 deselected in 13.85s`；外部模型、MCP 与网络测试本次未运行。

## 回头看

失败复现证明两层验证承担不同责任：配置模型负责在 runtime 资源出现前给出字段错误，Session 则负责让“一个 Turn 至少可能执行一个 Step”成为 core 自身不变量。最终实现没有把循环内部的空区间改成特殊停止原因，因为无效预算不应产生 Turn。
