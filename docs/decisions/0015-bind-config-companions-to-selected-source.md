# ADR-0015：配置伴随文件绑定到已选主配置来源

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/config.py`、`mini_agent/cli.py`、`tests/test_config_provenance.py`、提交 `167a839`

## 背景

CLI 先选择并解析一个 `config.yaml`，却没有把它的路径交给后续运行时；系统提示词与 MCP 配置随后分别重新执行开发目录、用户目录、包目录的全局搜索（`git show 167a839^:mini_agent/cli.py | nl -ba | sed -n '544,680p'`）。因此主配置可以来自用户目录，而模型可见指令或可执行工具定义来自优先级更高的开发目录。配置模板却明确声明 `config.yaml`、`mcp.json` 与 `system_prompt.md` 位于同一目录（`mini_agent/config/config-example.yaml:8-11,30-31,43`）。

新回归在临时目录同时放置已选主配置的伴随文件与全局搜索诱饵；把旧的两次全局搜索恢复到该回归中，3 个来源隔离用例全部转红（`tests/test_config_provenance.py:64-158`）。

## 选项

1. **继续独立全局搜索**：每个文件都能从现有优先级中找到，但同一次 runtime 会混合不同配置来源。
2. **把来源保存进 Pydantic `Config`**：调用者只传一个对象，但来源会进入或隐藏在配置值模型中，程序化构造、相等比较和序列化需要额外语义。
3. **显式传递已选主配置路径**：配置值模型保持纯粹，CLI 在组装 runtime 时把自己已经持有的来源交给伴随文件解析。

## 决定

采用选项 3。`resolve_config_companion()` 对绝对路径原样返回，对相对路径只与已选 `config.yaml` 的词法父目录拼接；不调用全局搜索、`.resolve()`、`expanduser()`，也不在纯函数中检查文件系统（`mini_agent/config.py:14-23`）。`run_agent()` 把主配置路径显式传给 runtime，再由同一来源解析 MCP 配置与系统提示词（`mini_agent/cli.py:635-690`）。

相对伴随文件缺失时不跨来源补齐：系统提示词使用既有内置降级，MCP 不调用 manager。显式绝对路径继续可用。本轮不改变主配置搜索优先级、`skills_dir`、`workspace_dir`、`../`、符号链接或不可读文件的行为。

## 为什么否决其他的

**否决独立全局搜索**：系统提示词和 MCP 配置都会改变 agent 能看见的指令或能执行的工具，静默拼接不同来源使主配置不能完整说明一次 runtime。若这些文件被明确设计为互相独立的覆盖层，并有可见的合并顺序、来源诊断与完整性策略，独立搜索反而可以成为正式 contract。

**否决把来源放进 `Config`**：`Config` 当前表示可由 YAML 或程序直接构造的配置值；新增普通字段会污染字段与序列化边界，隐藏私有状态又会让相等和往返语义不透明。若未来多个宿主都要热重载或组合来源，应引入显式、不可变的 `LoadedConfig(config, source_path)` 加载结果对象；那时由加载层持有来源反而优于逐层传参。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_config_provenance.py tests/test_background_shell_lifecycle.py` 实测 `32 passed in 0.60s`。
- 回归覆盖相对文件存在、相对文件缺失但其他来源有诱饵、显式绝对路径，以及 `run_agent()` 到 runtime 的来源传递（`tests/test_config_provenance.py:42-158`；`tests/test_background_shell_lifecycle.py:700-705`）。
- 临时把两处解析恢复为 `Config.find_config_file()`，来源回归实测 `3 failed, 2 passed in 0.56s`。
- 显式排除 `external` 的完整集合实测 `269 passed, 9 deselected in 13.12s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

显式传递只增加一个纯路径函数和两个关键字参数，没有给 Pydantic 模型增加隐藏状态。缺失伴随文件继续减少能力而不是扩大来源：提示词使用内置最小值，MCP 保持未加载。最终实现也保留了通用 `Config.find_config_file()`，只停止把它用于本次 CLI runtime 的伴随文件。
