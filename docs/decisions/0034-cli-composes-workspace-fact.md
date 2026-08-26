# ADR-0034：工作区事实由 CLI 组装，core 不持有路径

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/cli.py`、`mini_agent/core/agent.py`、`tests/test_config_provenance.py`、`tests/test_agent_session_offline.py`

## 背景

CLI 已经选择运行时工作区、创建目录，并把同一路径交给工作区工具；`AgentSession` 又接收 `workspace_dir`、保存 `Path`、再次创建目录并改写系统提示词（`git show d533585:mini_agent/cli.py | nl -ba | sed -n '440,477p;596,659p;911,936p'`；`git show d533585:mini_agent/core/agent.py | nl -ba | sed -n '83,123p'`）。core 的 agent loop 除构造阶段外不读取该路径，`system_prompt` 公开别名也没有消费者。

这让一个 UI 无关的对话对象承担 CLI 宿主策略和文件副作用；程序化调用者即使只想运行内存中的模型测试，也会得到一个隐式工作区。需要保留的真实行为只有：CLI 模型请求包含本次绝对工作区，偶然标题或旧路径不能抑制它，准确事实块不重复。

## 选项

1. **继续由 Session 持有路径**：保留现有参数、目录副作用和提示词改写，宿主无需组装完整提示词。
2. **新增通用 workspace 模块**：由一个新对象统一路径、目录、提示词与工具组装，为未来宿主预留扩展点。
3. **CLI 组装工作区事实**：CLI 继续持有运行时路径，并在创建 Session 前完成提示词；core 只保存调用者交来的完整提示词。

## 决定

采用选项 3。`AgentSession` 删除 `workspace_dir`、`workspace_dir` 状态、目录创建、提示词改写和无消费者的 `system_prompt` 别名；CLI 用一个纯函数把准确工作区事实追加到已完成配置与 Skill 处理的提示词，再构造 Session。

本轮不改变 CLI 的工作区选择、成功运行时的目录创建、文件/bash/skill/MCP 工具、路径越界能力、权限或沙箱，也不把 CLI 降为只展示 core 的演示宿主。

## 为什么否决其他的

**否决继续由 Session 持有路径**：路径没有参与 Session 的执行不变量，只在构造时制造宿主特定副作用；保留它会让 core 的公开 contract 暗示不存在的工作区能力。若未来所有 core 调用都必须拥有并验证同一种路径边界，而且该边界直接参与工具授权，Session 持有经过验证的工作区对象反而可能合适。

**否决新增通用 workspace 模块**：当前只有 CLI 需要选择路径并组装提示词，新模块只会把十余行确定性字符串处理包装成提前设计的抽象。若出现第二个真实宿主，而且两者必须共享目录生命周期、路径规范化与权限 contract，这个模块才有可验证的共同责任。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_config_provenance.py tests/test_agent_session_offline.py tests/test_agent_loop_offline.py tests/test_tool_execution.py tests/test_tool_output_budget.py tests/test_session_integration.py -m 'not external'` 实测 `73 passed in 0.79s`。
- `tests/test_config_provenance.py` 固定偶然标题、旧路径、准确块幂等和 CLI runtime 实际交给 Session 的完整提示词；`tests/test_agent_session_offline.py` 固定 core 逐字保留宿主提示词。
- 临时删除 `_run_configured_runtime()` 的组装挂钩后，三个配置来源场景实测 `3 failed in 0.70s`；恢复后定向集合全绿。
- `.venv/bin/python -m pytest -q` 实测 `271 passed, 8 deselected in 13.81s`；外部模型、MCP 与网络测试本次未运行。

## 回头看

迁移没有新增 workspace 类或修改任何工具；生产代码删除的 Session 状态与新增的 CLI 纯函数相抵后净减 3 行，测试净减 61 行。原工作区事实回归只需移动所有权，模型可见内容没有放宽；完整离线集合保持通过，被否决的通用抽象仍无必要。
