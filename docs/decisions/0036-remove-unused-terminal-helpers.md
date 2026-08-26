# ADR-0036：删除无消费者的终端辅助 API

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/utils/`、`tests/test_terminal_utils.py`

## 背景

`mini_agent/utils/__init__.py` 原来公开 `calculate_display_width()`、`truncate_with_ellipsis()` 和 `pad_to_width()`。对改动前提交 `9380c1d` 执行 `rg -n 'truncate_with_ellipsis|pad_to_width' mini_agent tests`，后两者除定义与导出外只有专用测试消费者；CLI 只调用宽度计算。执行 `rg -n 'Colors\.[A-Z_]+' mini_agent tests --glob '*.py'` 还显示 `BLACK`、`BLUE`、`MAGENTA`、`WHITE`、`BRIGHT_BLACK`、`BRIGHT_MAGENTA` 和四个背景色常量没有消费者。

这些实现及其 18 项正向测试描述的是一套可能有用的通用终端库，却没有参与当前 agent、CLI 或任何工具行为。

## 选项

1. **保留预备 API**：未来排版可能直接复用，但当前项目要继续维护没有使用场景的行为与测试。
2. **只取消导出**：把函数降为模块内部实现，保留兼容代码，却仍没有内部消费者证明其语义。
3. **删除零消费者实现**：只保留 CLI 当前需要的颜色值与宽度算法，用负向回归固定缩小后的导出面。

## 决定

采用选项 3。删除两个函数、10 个颜色常量及仅验证它们的测试；`Colors`、`calculate_display_width()` 和 CLI 的全部调用保持不变。

本轮不修改 `mini_agent/cli.py`、`mini_agent/cli_events.py`、文件/bash/skill/MCP 工具或终端宽度规则。

## 为什么否决其他的

**否决保留预备 API**：没有生产调用就无法从真实需求判断截断时是否应保留 ANSI 状态、怎样处理 grapheme cluster，继续测试当前偶然语义只会增加虚假稳定性。若 CLI 或另一个已实现宿主真的需要截断或填充，并且调用场景能给出离线回归，按该场景恢复最小实现才是对的。

**否决只取消导出**：内部保留不能减少行为和测试维护面，也没有兼容窗口需要支撑。若项目作为版本化库发布，且已有外部调用者需要迁移期，先弃用公开导出、暂留实现会更合适；当前仓库没有这种证据。

## 怎么验证它是对的

- `.venv/bin/python -m pytest tests/test_terminal_utils.py tests/test_agent_loop_offline.py tests/test_tool_execution.py -q` 实测 `54 passed in 0.69s`，覆盖宽度计算与 CLI 事件渲染。
- 临时恢复 `mini_agent.utils.truncate_with_ellipsis` 后，负向导出回归实测 `1 failed in 0.33s`；临时改动随后撤销。
- `.venv/bin/python -m pytest -q` 实测 `252 passed, 5 deselected in 13.63s`；外部 MCP 与网络测试本次未运行。

## 回头看

最终生产代码净减 106 行，测试净减 110 行并删除 17 项只服务于死代码的测试。完整离线集合保持通过；CLI 文件及其实际使用的 13 个颜色值和宽度算法没有改变，因此没有把 CLI 降为演示宿主。
