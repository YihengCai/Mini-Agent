# ADR-0030：删除不可读取的 Note 半能力

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/tools/note_tool.py`、`mini_agent/cli.py`、`mini_agent/config.py`、`tests/test_note_tool.py`

## 背景

提交 `d09fa54` 中，CLI 只把 `SessionNoteTool` 注册为 `record_note`，`RecallNoteTool` 只存在于模块、导出和测试，没有进入真实运行时工具表（`git show d09fa54:mini_agent/cli.py | nl -ba | sed -n '480,498p'`；`git grep -n RecallNoteTool d09fa54 -- mini_agent tests`）。因此模型能把 JSON 写进工作区，却不能在后续 Turn 通过工具取回；README 也明确记录了这个缺口。

这条写入路径仍带来 210 行生产模块与 87 行专用离线测试（`git show d09fa54:mini_agent/tools/note_tool.py | wc -l`；`git show d09fa54:tests/test_note_tool.py | wc -l`），另有配置开关、CLI 接线和真实模型演示。它没有区分不可改写的会话事实、每次模型请求的选择视图、跨 Session 持久化和恢复，也没有容量或任务级评测证据，不能代表 coding agent 的记忆模块。

## 选项

1. **把 `recall_notes` 注册到运行时并继续加固 JSON 文件**：补齐表面读写，但随即需要定义并发、原子提交、容量、恢复和模型选择策略。
2. **保留单向 Note 作为演示草稿工具**：缩小承诺，但仍保留没有运行时消费者的持久化状态。
3. **删除 Note，等事实与请求视图 topic 具备失败证据后重做**：当前只维护真实 agent loop 所需的能力，不让半成品先塑造未来状态 contract。

## 决定

采用选项 3。删除 `SessionNoteTool`、`RecallNoteTool` 及整个 `note_tool.py`，同时删除工具导出、CLI 注册、`tools.enable_note`、配置示例、专用离线测试和外部记忆演示。旧配置中的 `tools.enable_note` 由现有严格配置模型按未知字段拒绝。

本项不改 Read、Write、Edit、bash、skill、MCP 或通用工具执行器，也不新增替代存储。真正的记忆能力以后从“会话事实记录与模型请求视图”topic 进入，先定义状态所有权、模型可见选择、持久化/恢复边界和一分钟内的离线判定。

## 为什么否决其他的

**否决立即注册读取工具**：它会把“能读回一个 JSON 数组”误当成记忆 contract，并让原子提交、并发更新、容量与恢复变成被动兼容负担。若记忆已经是当前 topic，且事实所有权、请求视图与任务级评测都已定义，这个最小文件实现反而可以作为可替换的持久化原型。

**否决单向草稿工具**：当前工具名称、字段和演示都承诺跨 Session 记忆，改文案不能创造读取消费者。若未来有一个明确只消费追加草稿的宿主流程，且这些内容被声明为可丢弃而非权威事实，单向工具可以是合理的小功能。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_session_integration.py tests/test_llm_adapters.py tests/test_config_provenance.py tests/test_background_shell_lifecycle.py tests/test_pytest_entrypoint.py -m 'not external'` 实测 `75 passed in 4.52s`。
- `rg -n 'SessionNoteTool|RecallNoteTool|enable_note|record_note|recall_notes' mini_agent tests` 无输出；配置与工具包导入、`compileall` 均通过。
- `.venv/bin/python -m pytest -q` 实测 `277 passed, 8 deselected in 14.11s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

生产代码净减 222 行，测试净减 300 行；没有加入替代抽象或迁移分支。ADR-0013 曾修复损坏文件被覆盖的真实缺陷，但它只加固了一个仍不可在运行时读取的工具，因此由本 ADR 推翻；当时的复现证据继续保留在历史记录中。
