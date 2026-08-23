# Examples

这些文件来自上游，用来观察现有 API 和组装路径，不代表本项目已完成现代 coding agent 设计。需要模型的示例会读取本地配置并访问真实端点；请先确认 API key 和费用。

## 文件

| 文件 | 内容 | 需要真实 API |
|---|---|---|
| `01_basic_tools.py` | 直接调用 `ReadTool`、`WriteTool`、`EditTool`、`BashTool` | 否 |
| `02_simple_agent.py` | 创建最小 `Agent`，执行文件与 shell 任务 | 是 |
| `03_session_notes.py` | 直接装配 `SessionNoteTool` 与 `RecallNoteTool` | 部分步骤需要 |
| `04_full_agent.py` | 手工组合文件工具、note 工具、MCP 与 skills | 是 |
| `05_provider_selection.py` | 展示怎样选择 `LLMProvider` | 是 |
| `06_tool_schema_demo.py` | 自定义 `Tool` 数据结构与 `ToolResult` | 是 |

## 运行

不访问 API：

```bash
python examples/01_basic_tools.py
```

其他示例先准备配置：

```bash
cp mini_agent/config/config-example.yaml mini_agent/config/config.yaml
python examples/02_simple_agent.py
```

`config.yaml` 包含 API key，已经被 `.gitignore` 排除，不要提交。

## 已知限制

- `03_session_notes.py` 和 `04_full_agent.py` 会手工创建 `RecallNoteTool`；当前 CLI 与 ACP 的共享组装路径只注册写入侧，见[上游审计](../docs/UPSTREAM_AUDIT.md)。示例能运行不代表运行时具备相同能力。
- `02_simple_agent.py`、`04_full_agent.py` 等路径会调用真实模型，不属于离线测试。
- `04_full_agent.py` 是上游的组合示例，不是生产配置。
- 当前 `EditTool` 的 contract 与实现不一致，见[上游审计](../docs/UPSTREAM_AUDIT.md)。请只在临时工作区运行。

当前工作和待研究问题见 [BUILD_LIST](../docs/BUILD_LIST.md)，已实现范围见项目 [README](../README.md)。
