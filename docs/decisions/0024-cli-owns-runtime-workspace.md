# ADR-0024：CLI 单一持有运行时工作区

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/config.py`、`mini_agent/cli.py`、`tests/test_llm_adapters.py`、提交 `4f6d5cc`

## 背景

上游 `AgentConfig.workspace_dir` 会从 YAML 读取并进入配置对象（`git show 953b943:mini_agent/config.py | nl -ba | sed -n '31,38p;131,136p'`），示例配置也公开了该字段。运行时却始终由 CLI 依据 `--workspace` 或当前目录生成路径，再把同一个值传给工作区工具与 Session（`mini_agent/cli.py:651-714,968-980`）；代码没有读取 `config.agent.workspace_dir`。

因此用户修改 YAML 后不会改变任何运行时事实，配置对象却会成功保留并序列化这个值。实现前的四项定向测试同时证明旧字段仍在默认输出、程序化构造与 YAML 加载中被接纳，实测 `4 failed, 44 deselected in 0.48s`。

## 选项

1. **让配置参与工作区选择**：定义配置、命令行和当前目录的优先级，使原字段真正生效。
2. **由 CLI 单一持有并删除配置字段**：保留现有运行时行为，模型与 YAML 都拒绝旧字段，YAML 错误指向 `--workspace`。
3. **继续接纳但忽略旧字段**：不破坏旧配置文件，只在文档中说明它无效。

## 决定

采用选项 2。`AgentConfig` 不再声明 `workspace_dir`，示例配置同步删除；YAML 加载在通用未知字段检查前给出定向迁移错误（`mini_agent/config.py:53-57,128-156`）。CLI 继续以 `--workspace` 优先、当前目录兜底，并把结果作为一次运行时的唯一工作区事实。

本轮不改变路径展开、目录创建、Session 工作区事实、工具路径能力或安全边界。

## 为什么否决其他的

**否决让配置参与选择**：当前运行时在加载配置前就由入口确定工作区，引入配置值需要新建优先级、相对路径来源与多入口一致性 contract，却没有现有失败要求这些能力。若未来出现非 CLI 宿主，且工作区必须随一份可移植运行配置声明，这个方案反而合适，但应先明确来源和覆盖规则。

**否决继续静默接纳**：成功加载一个永远无效的路径会制造错误的控制感，也使严格配置模型不能代表真实能力。若项目正处于有兼容窗口、版本化弃用警告和迁移观测的发布周期，暂时接纳才可能合适；本学习项目没有这些兼容条件，定向失败更诚实。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_llm_adapters.py tests/test_config_provenance.py tests/test_agent_loop_offline.py` 实测 `77 passed in 0.69s`。
- `tests/test_llm_adapters.py:157-160,231-243` 分别固定模型字段集合、程序化拒绝和带 `--workspace` 指引的 YAML 拒绝。
- 临时删除定向迁移分支后，YAML 回归因只得到通用未知字段错误而转红，实测 `1 failed, 47 deselected in 0.43s`；恢复后两项工作区回归为 `2 passed, 46 deselected in 0.34s`。
- 显式排除 `external` 的完整集合实测 `314 passed, 9 deselected in 13.18s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现只删除一份从未进入运行时的配置状态，没有改动 CLI 选择与传递路径。公开 YAML contract 变窄，但旧配置会得到可执行的迁移指引；没有出现需要让配置重新参与工作区选择的证据。
