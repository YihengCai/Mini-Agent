# Mini-Agent：从演示项目到现代 coding agent

这是我的 coding agent 学习项目。

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供了约 4.1k LOC 的 agent loop、文件与 shell 工具、CLI、MCP/skills 加载器、日志和 ACP 适配器；我会在这条真实循环上研究现代 coding agent 的核心设计问题。

目标不是堆功能，也不在实现前宣称 SOTA。每个模块都要回答三个问题：当前代码怎样失败，正确边界在哪里，哪个一分钟内可运行的离线回归测试能检出关键实现缺失。

## 当前状态

项目仍处于“上游 baseline + 代码审计”阶段，还没有开始新的 harness 改造。代码可以运行，但保留了上游演示版本的重要限制：

- 一部分测试无法有效失败，另一部分会访问真实 API；
- 终端渲染与 agent loop 耦合，ACP 复制了另一份循环；
- Esc 没有真正取消正在运行的 LLM 或工具任务，消息历史清理还会删除已完成记录；
- 上下文压缩会破坏工具调用结构，失败时甚至可能扩大上下文；
- `edit_file` 会替换全部匹配，与工具 contract（工具向模型承诺的行为约定）中的“必须唯一”不一致；
- 没有权限引擎、工作区边界限制或操作系统沙箱。

不要把当前版本用于重要仓库或无人监督执行。当前已实现范围以本节、代码和离线测试为准。

## 当前学习重点

第一步是建立 LLM 测试替身，让真实 agent loop 可以在不访问网络的情况下得到可编排、可观察、确定性的模型响应。之后再基于测试暴露的真实耦合，处理核心循环与 CLI、ACP 的边界。

上下文管理、编辑、搜索、安全、检查点和 subagent 目前只是待研究问题，不代表方案或实现顺序已经确定。当前工作和进入下一步的条件见 [BUILD_LIST](docs/BUILD_LIST.md)。

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
mini_agent/                  上游 agent loop、模型客户端、工具、CLI、MCP/skills、ACP
tests/                       上游测试；后续加入离线不变量测试
docs/BUILD_LIST.md           当前工作与待研究问题
docs/UPSTREAM_AUDIT.md       上游代码审计
docs/specs/                  仅当前实现的短规格
docs/decisions/              ADR
docs/PITFALLS.md             实现过程中亲历的踩坑
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

- [BUILD_LIST](docs/BUILD_LIST.md)：当前做什么、未来有哪些待研究问题
- [上游审计](docs/UPSTREAM_AUDIT.md)：上游代码的已知问题
- [实现规格](docs/specs/README.md)：当前实现边界
- [决策记录](docs/decisions/README.md)：ADR 索引
- [PITFALLS](docs/PITFALLS.md)：实现过程中亲历且可复现的错误假设
- [coding agent 测试框架调查](docs/reference/agent-testing-survey.md)：外部项目证据

## 来源与许可证

上游 Mini-Agent 的 agent loop、工具、CLI、MCP/skills 和 ACP baseline 来自 MiniMax-AI；本项目通过 Git 历史区分上游代码与我的改造。项目继续使用 [MIT License](LICENSE)。
