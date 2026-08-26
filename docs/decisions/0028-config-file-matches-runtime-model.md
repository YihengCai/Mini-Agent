# ADR-0028：配置文件直接匹配运行时模型

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/config.py`、`mini_agent/config/config-example.yaml`、`tests/test_llm_adapters.py`

## 背景

运行时 `Config` 已经按 `llm`、`agent`、`tools` 分组，YAML 却把前两组字段摊在根级。`Config.from_yaml()` 因而需要自行推导允许字段、检查必填项、维护旧字段迁移，再把同一份数据拆回运行时结构（`git show 73f1985:mini_agent/config.py | nl -ba | sed -n '75,179p'`）。为防这层分片漏掉合法字段，测试还要逐项构造所有非默认值（`git show 73f1985:tests/test_llm_adapters.py | nl -ba | sed -n '142,201p'`）。

这层转换没有表达不同语义，只在维护两种形状。仓库内也没有 `Config.load()` 调用；生产入口由 CLI 先选择路径，再调用 `Config.from_yaml()`（`rg -n 'Config\.load\(' mini_agent tests`；`mini_agent/cli.py:548-580`）。

## 选项

1. **继续维护扁平 YAML 到嵌套模型的映射**：旧文件无需迁移，但每个配置字段仍有额外分片和测试成本。
2. **同时接受扁平与嵌套两种结构**：可以提供兼容窗口，但加载器要判断形状、处理冲突并长期测试两套 contract。
3. **YAML 直接使用运行时结构**：只保留一次模型校验；旧文件需要按模板做一次手工迁移。

## 决定

采用选项 3。示例配置以 `llm`、`agent`、`tools` 为三个根级字段；`Config.from_yaml()` 只读取一个映射并调用 `Config.model_validate()`。`agent` 和 `tools` 的默认工厂进入 `Config` 本身，使 YAML 与程序化构造共享同一缺省语义（`mini_agent/config.py:75-122`）。

删除无调用的 `Config.load()`、根级字段分片、旧 `provider` / `local_compaction_token_limit` / `workspace_dir` 的定向提示，以及只识别示例 API key 字符串的加载器分支。旧字段仍由 `extra="forbid"` 拒绝，但不保留迁移专用文案。CLI 的主配置搜索和 ADR-0015 的伴随文件来源绑定不变。

## 为什么否决其他的

**否决继续映射扁平结构**：外部和内部字段没有不同含义，这层映射只制造第二种结构和漏字段风险。若项目已经发布稳定配置格式，且兼容成本小于用户迁移成本，保留显式映射反而合理；当前是学习仓库，没有这样的发布承诺。

**否决同时接受两种结构**：双格式需要定义同一字段在两处出现时的优先级，并让每个新字段继续覆盖两个输入面。若项目有版本号、弃用周期和迁移观测，兼容窗口会有价值；当前没有这些机制，接受两种结构只会把临时迁移变成长期分支。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_llm_adapters.py tests/test_config_provenance.py tests/test_background_shell_lifecycle.py::test_invalid_config_returns_before_runtime_resources_exist tests/test_background_shell_lifecycle.py::test_run_agent_uses_the_same_manager_for_runtime_and_cleanup -m 'not external'` 实测 `43 passed in 0.70s`。
- 嵌套最小配置直接获得 `agent` 与 `tools` 默认值；根级和四层嵌套错键继续由同一严格模型拒绝（`tests/test_llm_adapters.py:80-150`）。
- 示例回归要求根级恰好是 `llm`、`agent`、`tools`，补入本地值后可直接加载（`tests/test_llm_adapters.py:164-183`）。
- `.venv/bin/python -m pytest -q` 实测 `285 passed, 9 deselected in 13.90s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

`mini_agent/config.py` 净减 114 行，配置测试净减 85 行；主配置搜索顺序和伴随文件来源测试无需修改。实现时选择把 `agent` / `tools` 默认工厂放进 `Config`，避免结构迁移意外把原有可选分组变成必填项；没有出现需要双格式兼容层的证据。
