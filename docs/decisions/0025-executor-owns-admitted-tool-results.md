# ADR-0025：执行器取得工具返回值所有权

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/tool_execution.py`、`tests/test_tool_execution.py`、提交 `da13e33`、[ADR-0008](0008-session-owned-tool-batch-executor.md)

## 背景

批次执行器会把 `ToolFinished.result` 深拷贝后交给同步观察者，却继续用工具原样返回的 `ToolResult` 构造模型消息（`git show da13e33^:mini_agent/core/tool_execution.py | nl -ba | sed -n '108,132p;162,185p'`）。工具若保留返回对象，观察者可以在 `ToolFinished` 期间通过该别名改写它；事件快照仍是 `original`，随后 Session 历史却变成 `mutated-after-event`。

新增故障注入在旧实现上实测 `1 failed, 20 deselected in 0.47s`。这说明“事件得到副本”只隔离了观察面，没有让执行器拥有接纳后的工具事实。

## 选项

1. **要求工具返回后不再修改对象**：保留零复制，但这个约定无法由现有 `Tool` contract 强制。
2. **在合法返回值接纳处立即深拷贝**：后续事件、模型投影与历史都从执行器自有对象派生。
3. **立即把返回值转换为不可变内部类型**：从类型层消除变异，但会同时改变事件和工具 contract。

## 决定

采用选项 2。`_execute_call()` 确认返回值是 `ToolResult` 后立即执行 `model_copy(deep=True)`（`mini_agent/core/tool_execution.py:162-185`）。未知工具、异常与非法返回本来就由执行器新建结果，不增加额外复制。

本轮不改变 `ToolResult` 字段、成功/失败归一、输出预算、事件类型、批次顺序、取消或调用标识符语义。

## 为什么否决其他的

**否决只靠工具自律**：可信宿主可以持有 Tool 引用，不等于工具应拥有已经交给 agent loop 的事实；不可执行的约定会把模型历史正确性依赖于每个叶子实现。若返回值是语言级不可变值，或所有工具都在同一信任边界内且有静态检查，这个方案才足够。

**暂不引入不可变内部类型**：当前 `ToolResult` 还被工具、事件和适配器广泛使用，仅为三个标量字段新建转换层会扩大公开 contract，却没有额外失败证据。若以后结果包含结构化嵌套数据，且多个消费者需要共享无复制读取，不可变内部类型反而可能更合适。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_tool_execution.py tests/test_tool_output_budget.py tests/test_agent_session_offline.py` 实测 `47 passed in 0.62s`。
- `tests/test_tool_execution.py:671-695` 让工具保留返回别名，观察者在 `ToolFinished` 中修改该别名；事件与 Session 历史都必须保留 `original`，工具自己的对象仍可变。
- 删除接纳处的深拷贝即恢复旧实现，对应回归实测 `1 failed, 20 deselected in 0.47s`。
- 显式排除 `external` 的完整集合实测 `315 passed, 9 deselected in 13.33s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现只增加一次接纳复制，没有改变后续事件复制和模型投影顺序。P-003 与 P-013 已经记录同一类嵌套所有权教训，本次是把既有原则覆盖到工具返回边界，因此没有另建 PITFALL。
