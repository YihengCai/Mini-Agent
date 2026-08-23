# Mini-Agent：从演示项目到现代 coding agent

这是我的 coding agent 学习项目。

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供了约 4.1k LOC 的 agent loop、文件与 shell 工具、CLI、MCP/skills 加载器、日志和 ACP 适配器；我会在这条真实循环上研究并实现现代 coding agent 的关键机制。

目标不是堆功能，也不在实现前宣称 SOTA。每个模块都要回答三个问题：直觉实现会怎样失败，正确边界在哪里，哪个一分钟内可运行的离线测试能证明它。

## 当前状态

项目仍处于“上游 baseline + 设计审计”阶段，新增机制尚未标为“已验证”。代码可以运行，但保留了上游演示版本的重要限制：

- 一部分测试无法有效失败，另一部分会访问真实 API；
- 终端渲染与 agent loop 耦合，ACP 复制了另一份循环；
- Esc 没有真正取消正在运行的 LLM 或工具任务，消息历史清理还会删除已完成记录；
- 上下文压缩会破坏工具调用结构，失败时甚至可能扩大上下文；
- `edit_file` 会替换全部匹配，与工具 contract（工具向模型承诺的行为约定）中的“必须唯一”不一致；
- 没有权限引擎、工作区边界限制或操作系统沙箱。

不要把当前版本用于重要仓库或无人监督执行。每项能力的真实状态只看[机制表](docs/mechanisms.md)。

## 学习路线

| 阶段 | 机制 | 核心问题 |
|---|---|---|
| 0 | FakeLLM + 消息历史不变量 | 怎样让 agent loop 测试快速、结果确定，而且真的会失败 |
| 1 | 事件层 + 中断 + 流式输出 + steering | 怎样解耦控制流、渲染、取消操作和工具调用的因果关系 |
| 2 | 上下文管理 + 提示词缓存 | 怎样协调压缩正确性、窗口质量与稳定前缀 |
| 3 | 事务式编辑 + 诊断 | 怎样拒绝有歧义的补丁、发现读取后发生的文件变化，并限制诊断噪声 |
| 3 | Glob/Grep + 显式截断元数据 | 为什么静默截断是正确性问题 |
| 4 | 操作系统沙箱 + 结构化权限 | 为什么授权与内核强制执行必须分层 |
| 5 | AGENTS 信任边界 + 规划模式 | 为什么仓库指令不能授权动作，以及怎样通过工具列表强制执行模式 |
| 6 | 检查点 / 回退 | 怎样同时恢复文件系统与模型可见的消息历史，而不碰用户的 Git |
| 6 | subagent 上下文隔离 | 怎样限制委派结果进入父级上下文的内容 |
| 7 | 任务回归测试集 | 怎样区分模型、API、预算、超时与测试框架故障 |

详细顺序和完成标准见 [BUILD_LIST](docs/BUILD_LIST.md)。

## 设计原则

### 自己掌握循环

不引入 LangGraph、pydantic-ai 等 agent 框架。这个项目最值得学习的部分，就是状态怎样经过模型调用、工具调用、中断、压缩和恢复。

### 每个机制都要有失败测试

“实现了沙箱”不是结论，真实的操作系统拒绝测试才是。“实现了上下文管理器”也不是结论；还要证明随机工具调用组中没有未配对记录，而且摘要失败不会增大输入。

### 模型服务能力先探测再依赖

项目使用当前配置的 MiniMax 端点，通过 Anthropic 兼容消息协议通信。协议兼容不代表支持所有 vendor 扩展；缓存、流式输出、`thinking` 签名和 `usage` 语义必须先记录到[模型服务能力矩阵](docs/PROVIDER_CAPABILITIES.md)。

### 安全功能不发布半成品

普通学习模块可以逐步演进；沙箱和检查点不行。只有通过真实的平台与文件测试矩阵后才能标为“已验证”，否则只保留设计与限制。

### 记录原因

代码证明机制能工作；ADR、PITFALL 和外部调研记录为什么选择这个方案，以及什么条件下另一个方案更合适。

## 仓库结构

```text
mini_agent/                  上游 agent loop、模型客户端、工具、CLI、MCP/skills、ACP
tests/                       上游测试；后续加入离线不变量测试
docs/mechanisms.md           机制状态的唯一来源
docs/BUILD_LIST.md           实现顺序、依赖与完成标准
docs/UPSTREAM_AUDIT.md       上游代码审计
docs/specs/                  当前和下一阶段的实现规格
docs/decisions/              ADR
docs/PITFALLS.md             带复现命令的问题记录
docs/reference/              外部 coding agent 源码调研
docs/PROVIDER_CAPABILITIES.md 端点能力探测结果
```

## 运行上游 baseline

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

不要直接运行完整 `pytest`：部分上游测试会读取本地配置并访问真实 API，`tests/test_acp.py` 还有一个独立故障。

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
  tests/test_markdown_links.py
```

## 文档入口

- [BUILD_LIST](docs/BUILD_LIST.md)：接下来做什么
- [机制表](docs/mechanisms.md)：每个机制的当前状态
- [上游审计](docs/UPSTREAM_AUDIT.md)：上游代码的已知问题
- [实现规格](docs/specs/README.md)：实现边界
- [决策记录](docs/decisions/README.md)：ADR 索引
- [PITFALLS](docs/PITFALLS.md)：可复现的问题
- [coding agent 测试框架调查](docs/reference/agent-testing-survey.md)：外部项目证据

## 来源与许可证

上游 Mini-Agent 的 agent loop、工具、CLI、MCP/skills 和 ACP baseline 来自 MiniMax-AI；本项目通过 Git 历史区分上游代码与我的改造。项目继续使用 [MIT License](LICENSE)。
