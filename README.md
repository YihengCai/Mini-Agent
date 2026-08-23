# Mini-Agent：从 Demo 到现代 Coding Agent

这是我的 coding agent 学习工程。

项目 fork 自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，基线为 commit `953b943`。上游提供了约 4.1k LOC 的真实 agent loop、文件与 shell 工具、CLI、MCP/skills 加载、日志和 ACP 桥；我在这条真实循环上研究并亲手实现现代 coding agent 的关键机制。

目标不是把功能名堆成“另一个 Claude Code”，也不在机制落地前宣称 SOTA。每个模块都要回答三件事：直觉实现会怎样失败、正确边界在哪里、哪个一分钟内可跑的离线工件能证明它。

## 当前状态

目前仍处于“上游基线 + 设计审计”阶段，新增的核心机制尚未标为已验证。当前代码可以运行，但仍保留上游 demo 的重要限制：

- 测试中存在无法有效失败和会访问真实 API 的路径；
- 终端渲染与 agent loop 耦合，ACP 复制了另一份循环；
- Esc 没有真正取消运行中的 LLM/tool task，历史清理还会删除已完成记录；
- 上下文压缩会丢失 tool 结构，失败路径甚至可能扩大上下文；
- `edit_file` 的实现会替换全部匹配，与“必须唯一”的工具契约不一致；
- 没有权限引擎、workspace confinement 或 OS sandbox。

因此，不要把当前版本用于重要仓库或无人监督执行。每项能力的真实状态只看 [机制状态表](docs/mechanisms.md)。

## 我在学习和实现什么

| 阶段 | 机制 | 要学的核心问题 |
|---|---|---|
| 0 | FakeLLM + 历史不变量 | 如何让 agent loop 测试便宜、确定、而且真的会失败 |
| 1 | 事件缝 + 中断 + streaming + steering | 控制流、渲染、取消和 tool-call 因果关系怎样解耦 |
| 2 | 分层上下文 + prompt cache | 压缩正确性、窗口质量与前缀缓存为什么互相拉扯 |
| 3 | 事务性编辑 + 诊断 | 如何拒绝多义 patch、发现陈旧读取并限制诊断噪声 |
| 3 | Glob/Grep + 自描述截断 | 为什么静默截断是正确性 bug，而不只是显示问题 |
| 4 | OS sandbox + 结构化权限 | 授权判断与内核约束为什么必须是两层机制 |
| 5 | AGENTS 信任边界 + plan mode | 文件指令为何不能授权动作，模式如何从工具表强制 |
| 6 | checkpoint / rewind | 怎样同时恢复文件系统与模型看到的历史而不碰用户 Git |
| 6 | 子 agent 上下文隔离 | 委派如何限制进入父上下文的字节，而不只是嵌套聊天 |
| 7 | 小型任务回归 | 如何区分模型失败、API 失败、预算耗尽和 harness 故障 |

详细顺序、依赖和完成门槛见 [学习改造路线](docs/BUILD_LIST_CN.md)。

## 设计原则

### 1. 自己拥有循环

不引入 LangGraph、pydantic-ai 等 agent framework。这个项目最值得学习的部分就是状态如何穿过 model call、tool call、interrupt、compaction 和 recovery。

### 2. 机制必须可证伪

“实现了 sandbox”不算结论；真实 OS 拒绝测试才算。“实现了 context manager”也不算；随机 tool 组下无 orphan、失败时不增大上下文才算。

### 3. Provider 能力先探测再依赖

项目运行在当前配置的 MiniMax endpoint，通过 Anthropic-compatible messages 协议通信。wire format 不等于支持所有厂商扩展；cache、streaming、thinking signature 和 usage 语义先记录到 [endpoint 能力矩阵](docs/PROVIDER_CAPABILITIES.md)，再进入设计。

### 4. 安全能力不能半成品冒充完成

普通学习模块可以逐步演进；sandbox 和 checkpoint 不行。它们只有在真实平台/文件矩阵通过后才会进入“已实现”，否则只保留设计说明和限制。

### 5. 记录“为什么”

代码证明机制能工作；ADR、PITFALL 和外部调研记录为什么选这条路、什么条件下另一个方案反而正确。

## 仓库结构

```text
mini_agent/                 上游 agent loop、provider client、tools、CLI、MCP/skills、ACP
tests/                      现有测试；后续加入离线不变量测试
docs/mechanisms.md          机制状态的唯一真相源
docs/BUILD_LIST_CN.md       实现顺序、依赖与 Definition of Done
docs/AGENT_ROADMAP_CN.md    上游基线审计，不是 roadmap
docs/specs/                 当前/下一阶段实现规格；后期模块只有设计说明
docs/decisions/             ADR：为什么选这个方案
docs/PITFALLS.md            带复现命令的踩坑记录
docs/reference/             对真实 coding agent 项目的源码调查
docs/PROVIDER_CAPABILITIES.md 当前 endpoint 的能力探测结果
```

## 运行上游基线

要求 Python 3.10+。推荐使用 `uv` 管理环境。

```bash
git submodule update --init --recursive
uv sync
cp mini_agent/config/config-example.yaml mini_agent/config/config.yaml
```

编辑 `mini_agent/config/config.yaml`，填入你自己的 API key、endpoint 和模型。`config.yaml` 与 `mcp.json` 含密钥，已被 `.gitignore` 排除，不要提交。

在一个可丢弃的 workspace 中启动：

```bash
uv run mini-agent --workspace ./workspace
```

或者执行一次非交互任务：

```bash
uv run mini-agent --workspace ./workspace --task "inspect the project and explain its agent loop"
```

查看运行日志：

```bash
uv run mini-agent log
```

## 测试

不要直接运行完整 `pytest`：部分上游测试读取本地配置并访问真实 API，`tests/test_acp.py` 还有一个已确认的独立红灯。

当前离线子集：

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

随着机制落地，README 只会引用已经存在并通过的测试名；计划中的测试留在 [机制状态表](docs/mechanisms.md)，不伪装成测试结果。

## 文档入口

- 想看接下来做什么：[docs/BUILD_LIST_CN.md](docs/BUILD_LIST_CN.md)
- 想看每个机制当前做到哪：[docs/mechanisms.md](docs/mechanisms.md)
- 想看上游哪里坏了：[docs/AGENT_ROADMAP_CN.md](docs/AGENT_ROADMAP_CN.md)
- 想看方案边界：[docs/specs/README.md](docs/specs/README.md)
- 想看取舍：[docs/decisions/README.md](docs/decisions/README.md)
- 想复现踩坑：[docs/PITFALLS.md](docs/PITFALLS.md)
- 想看对标项目源码调查：[docs/reference/agent-testing-survey.md](docs/reference/agent-testing-survey.md)

## 归属与许可证

上游 Mini-Agent 的 agent loop、工具、CLI、MCP/skills 和 ACP 基线来自 MiniMax-AI；本 fork 会通过 Git 历史明确区分上游代码与我的机制改造。项目继续遵循仓库中的 [MIT License](LICENSE)。
