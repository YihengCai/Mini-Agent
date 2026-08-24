# Mini-Agent：从演示项目到现代 coding agent

这是我的 coding agent 学习项目。

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供了约 4.1k LOC 的 agent loop、文件与 shell 工具、CLI、MCP/skills 加载器、日志和 ACP 适配器；我会在这条真实循环上研究现代 coding agent 的核心设计问题。

目标不是堆功能，也不在实现前宣称 SOTA。每个模块都要回答三个问题：当前代码怎样失败，正确边界在哪里，哪个一分钟内可运行的离线回归测试能检出关键实现缺失。

## 当前状态

项目已在上游 baseline 上完成三项改造：`tests/` 中新增了脚本化 LLM 测试替身和真实 agent loop 的离线回归；文件工具改成了有界读取、唯一匹配和单文件原子替换；agent loop 也已移入不依赖终端的 `mini_agent/core/`。CLI 通过同步事件适配器渲染和写日志，原来的 ACP 适配器、命令入口与依赖已经删除。

`read_file` 现在返回 1-based 行窗口，编号正文最多 2000 个完整行或 50 KiB，并给出下一次 `offset`；`edit_file` 仅把 LF/CRLF 视为等价，其他文本必须精确匹配且只能出现一次。写入和编辑通过同目录临时文件和 `os.replace()` 提交，已有文件保留 CRLF 约定与权限位。代码可以运行，但仍保留重要限制：

- 部分上游测试仍无法有效失败或会访问真实 API；
- core 事件目前只是进程内同步通知，不是可持久化、可回放的轨迹格式，也还没有独立统计或基准评测消费者；
- Esc 没有真正取消正在运行的 LLM 或工具任务，消息历史清理还会删除已完成记录；
- 上下文压缩会破坏工具调用结构，失败时甚至可能扩大上下文；
- 文件工具仍接受绝对路径和解析到工作区外的路径，也没有读取版本回执或并发覆盖检测；
- 没有权限引擎、工作区边界限制、操作系统沙箱、跨文件回滚或检查点。

不要把当前版本用于重要仓库或无人监督执行。当前已实现范围以本节、代码和离线测试为准。

## 当前学习重点

LLM 测试替身已经落地：agent 与摘要调用共用一条按用途标注的全局脚本序列；用途错位、响应不足、响应剩余和工具调用配对错误都会使测试失败。测试会记录模型实际收到的消息和工具定义，并已覆盖真实工具循环、摘要交错、工具失败和最大步数。

文件工具改造也已经落地：读取预算、续读提示、歧义拒绝、CRLF、权限位和原子替换失败都有离线回归；删除唯一匹配判断、`os.replace()` 或超长行早停时，对应测试会转红。取舍见 [ADR-0002](docs/decisions/0002-bounded-and-atomic-file-tools.md)。

核心边界改造已经落地：消息、压缩、模型调用、工具执行和终止判断只在 `mini_agent/core/agent.py` 中运行；`mini_agent/cli_events.py` 消费同一条事件序列完成终端渲染与原有文本日志。不给 `event_sink` 时，真实 agent loop 可以无终端输出、无日志副作用地运行。事件顺序、摘要交错、CLI 输出与日志调用，以及 core 不导入 UI、日志或传输模块，都有离线回归。

ACP 没有真实外部客户端，也没有覆盖 JSON-RPC、stdio 或连接生命周期的端到端测试；继续维护它只会让协议层提前塑造执行框架。因此当前版本主动删除 ACP，而不是把 CLI 改成 ACP 客户端。重新引入协议层的条件见 [ADR-0003](docs/decisions/0003-remove-acp-and-extract-core-loop.md)。下一项工作尚未自动选择；继续从 [BUILD_LIST](docs/BUILD_LIST.md) 中挑选有当前失败证据的主题。

## 设计原则

### 自己掌握循环

不引入 LangGraph、pydantic-ai 等 agent 框架。这个项目最值得学习的部分，就是状态怎样经过模型调用、工具调用、中断、压缩和恢复。

### 每项改造都要证明测试敏感性

“实现了沙箱”不是结论，真实的操作系统拒绝测试才是。对普通改造，也要证明删除关键实现会让对应离线回归测试转红。

### 模型服务能力先探测再依赖

项目使用当前配置的 MiniMax 端点，通过 Anthropic 兼容消息协议通信。协议兼容不代表支持所有 vendor 扩展；当前实现需要依赖某项能力时，必须先把实测结果记录到[模型服务能力记录](docs/PROVIDER_CAPABILITIES.md)。

### 安全功能不发布半成品

普通学习模块可以逐步演进；沙箱和检查点不行。只有通过真实的平台与文件测试矩阵后才能标为“已验证”，否则只保留设计与限制。

### 记录原因

代码和测试证明改造能工作；ADR 记录实现过程中真正做出的取舍，PITFALL 记录亲历且可复现的错误假设。它们不会为未来阶段预写。

## 仓库结构

```text
mini_agent/core/             UI 无关的 agent loop 与进程内事件 contract
mini_agent/cli.py            终端输入、取消轮询和运行时组装
mini_agent/cli_events.py     终端渲染与原有文本日志的事件适配器
mini_agent/agent.py          兼容原有导入路径的薄转发层
mini_agent/                  模型客户端、工具、MCP/skills 与配置
tests/                       上游测试、LLM 测试替身与离线回归
docs/BUILD_LIST.md           当前工作与可选研究主题
docs/UPSTREAM_AUDIT.md       上游代码审计
docs/specs/                  仅当前实现的短规格
docs/decisions/              ADR
docs/PITFALLS.md             实现过程中亲历的踩坑
docs/reference/              外部 coding agent 源码调研
docs/PROVIDER_CAPABILITIES.md 端点能力探测结果
```

## 运行项目

要求 Python 3.10+，推荐使用 `uv`：

```bash
git submodule update --init --recursive
uv sync
cp mini_agent/config/config-example.yaml mini_agent/config/config.yaml
```

编辑 `mini_agent/config/config.yaml`，填入自己的 API key、端点和模型。`config.yaml` 与 `mcp.json` 包含密钥，已被 `.gitignore` 排除，不要提交。

请在可丢弃的工作区中启动：

```bash
uv run mini-agent --workspace ./workspace
```

执行一次非交互任务：

```bash
uv run mini-agent --workspace ./workspace --task "inspect the project and explain its agent loop"
```

查看日志：

```bash
uv run mini-agent log
```

## 离线测试

不要直接运行完整 `pytest`：部分上游测试会读取本地配置并访问真实 API。

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_tools.py \
  tests/test_bash_tool.py \
  tests/test_skill_loader.py \
  tests/test_skill_tool.py \
  tests/test_note_tool.py \
  tests/test_tool_schema.py \
  tests/test_terminal_utils.py \
  tests/test_session_integration.py \
  tests/test_markdown_links.py \
  tests/test_agent_loop_offline.py \
  tests/test_architecture_boundaries.py
```

以上命令在 2026-08-24 实测为 `124 passed`；同时有一条既有的 `cache_dir` 配置警告，不影响测试结果。

## 文档入口

- [BUILD_LIST](docs/BUILD_LIST.md)：当前工作、可选研究主题及其选择条件
- [上游审计](docs/UPSTREAM_AUDIT.md)：上游代码的已知问题
- [实现规格](docs/specs/README.md)：当前实现边界
- [决策记录](docs/decisions/README.md)：ADR 索引
- [PITFALLS](docs/PITFALLS.md)：实现过程中亲历且可复现的错误假设
- [coding agent 测试框架调查](docs/reference/agent-testing-survey.md)：外部项目证据
- [主流开源 coding agent 文件工具调查](docs/reference/file-tools-survey.md)：本轮借鉴与未借鉴的源码依据

## 来源与许可证

上游 Mini-Agent 的 agent loop、工具、CLI、MCP/skills 和 ACP baseline 来自 MiniMax-AI；本项目通过 Git 历史区分上游代码与我的改造。项目继续使用 [MIT License](LICENSE)。
