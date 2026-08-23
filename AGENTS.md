# AGENTS.md

给在这个仓库里干活的 agent——以及未来的我。**动手之前先读完这一页。**

## 这个项目是什么

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供 agent loop、工具、CLI、MCP/skill 加载器和 ACP 适配器，约 4.1k LOC。

**我的目标：研究真实 coding agent（Claude Code / Codex 等）的关键机制，把这个演示项目逐步改造成采用现代设计的 coding agent，并在真实 agent loop 上亲手实现、验证每个模块。这是学习项目；没有测试证据的能力不宣称已经实现或达到 SOTA。**

取舍标准只有三条：

1. **机制深度** —— 这个模块有没有"直觉实现是错的"那一层？没有的话它只是功能，不是机制。
2. **失败测试** —— 能不能用一个一分钟内、不需要 API key、删除关键实现后会失败的离线测试证明它？
3. **半成品的风险** —— 多数模块可以分阶段实现；沙箱和检查点不行。这两个要么做完整，要么只写设计说明。

## 记录规范（这一页最重要的一节）

这个项目的产出**不只是代码，还有"为什么"**。回顾靠它，面试也靠它。所以：

### 什么时候写一条 ADR（`docs/decisions/`）

命中任一就写，**在动手之前或紧随其后写，不要攒**：

1. 在两个及以上可行方案里做了选择——哪怕当时觉得显然
2. 决定**不做**某件事，或砍掉原计划里的东西
3. 改变了模块边界、实现顺序或依赖关系
4. 发现某个"标准做法"在这里不适用，走了别的路
5. 推翻了之前的 ADR（**新开一条**，把旧的状态改成 `已推翻（见 ADR-XXXX）`，不要原地改写历史）

不用写 ADR 的：纯实现细节、命名、任何代码差异本身就能说明白的事。

格式见 [`docs/decisions/0000-template.md`](docs/decisions/0000-template.md)。**「为什么否决其他的」是必写项**，而且要写清楚"在什么条件下被否决的那个反而是对的"——回顾和面试的价值主要在这一节。

### 什么时候写一条 PITFALL（`docs/PITFALLS.md`）

1. 我以为是 A、实际是 B，且这个误解花了超过 15 分钟
2. 某个 API / 工具的行为与文档不符或反直觉（`shlex`、seatbelt、git plumbing、Anthropic 对空 block 和孤立 `tool_use` 的 400 规则……）
3. 一个问题的根因不在它报错的位置
4. 一个"看起来能用"的实现其实在静默出错

每条必须有**可复现命令**和**一句能迁移到别的 agent 项目上的教训**。没有复现命令就先做出复现再记——记不下来的坑等于没踩过。

### 写的时机

**在同一个会话里做完决定就写，不要留到最后。** 事后补的 ADR 会丢掉当时否决的选项，而那正是最值钱的部分。写完在回复里用一行说明写了哪条，方便我当场校对。

### 落地一个机制之后

1. 更新 [`docs/mechanisms.md`](docs/mechanisms.md) 对应行：状态 + 回归测试名
2. 补上对应 ADR 的“回头看”：实际结果、偏差、是否需要新 ADR
3. 如果实现改变模块边界或数据流，先写新 ADR；普通实现细节以代码为准，及时更新规格

## 动手之前：先给「改动前简报」

**任何修改或新增实现代码之前，先停下来给我一份简报，然后等我回应再动手。** 我需要在你改之前就看清边界、决策点和进度，而不是从代码差异里倒推。

简报固定五段，**总共不超过 15 行**，不要贴大段代码——它是地图，不是复述：

1. **现状**：这块现在怎么工作。讲机制，带 `file:line`。要说清"谁调谁、状态存在哪、谁持有它"，不要罗列文件。
2. **边界**：这次**会**改哪些文件和大致行区间；同样重要的是**不会**碰什么（尤其是看起来相关但我决定不动的部分）。
3. **决策点**：有哪几种做法、我打算选哪个、一句话理由。**这里如果出现真正的取舍，就是一条 ADR**（见上面的触发条件），简报里先点名，落地时补记录。
4. **影响**：会不会动到现有不变量、测试或其他机制的假设；对 [`docs/mechanisms.md`](docs/mechanisms.md) 哪一行有影响。
5. **进度**：这一步属于 [`docs/BUILD_LIST.md`](docs/BUILD_LIST.md) 的哪个阶段、机制表哪一行，做完之后那一行的状态变成什么。

什么时候可以省成一句话：**单文件、不引入新概念、不改任何接口或不变量**的改动（改错别字、补一条断言、调一个已经定好的常量）。除此之外一律走完整简报——包括"顺手重构一下"，那种恰恰最需要先划边界。

什么时候可以不等我确认：我已经明确说了"直接做"、"按 ADR-000X 实现"，或者本轮任务本身就是我下的具体指令。**其余情况给完简报就停。**

## 模型服务约束（硬约束）

这个项目跑在**当前配置的端点**上（现在是 MiniMax，走 Anthropic 兼容协议）。出于成本和兼容性：**不采用 OpenAI API，不采用 DeepSeek 模型；任何设计也不要推广或依赖 Anthropic 的模型与 API。**

要分清三件事，别混为一谈：

1. **协议格式** —— Anthropic 兼容消息协议是当前端点的兼容层。`mini_agent/llm/anthropic_client.py` 是协议客户端，不表示依赖 Anthropic 模型。
2. **vendor 能力** —— `cache_control`、服务端 `context_management`、extended-thinking 签名往返、`usage` 语义等一律先探测再依赖，结果记进 [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md)。
3. **外部设计** —— Anthropic / Claude Code / Codex / aider 的公开设计可以参考，但要写清楚借鉴内容以及为什么适用于本项目。

由此产生两条可执行的规则：

- 任何依赖 vendor 能力的设计必须引用能力矩阵。状态仍是 `待探测` 时，规格必须写明降级方案。
- 机制的价值可以引用 vendor 文档，但数字必须来自本项目端点的真实探测。外部数字必须标明来源，不能写成本项目结论。

## 文档语言

- 文档使用中文句法。已有广泛接受译法的词使用中文，例如“代码、测试、文件、状态、事件、上下文、提示词、沙箱、检查点、不变量、降级方案”；不要写成 `code`、`test`、`file`、`state`、`event`、`context`、`prompt`、`sandbox`、`checkpoint`、`invariant`、`fallback`。
- `coding agent`、`agent`、`agent loop`、`contract`、`vendor`、`subagent`、`steering`、`baseline` 固定使用英文，不翻译成“编程智能体”“智能体”“智能体循环”“契约”“厂商”“子智能体”等。模型上下文和用量语境中的 `token` 也保留英文，不写“词元”；安全认证语境仍使用已有译法“访问令牌”。
- 难以翻译的英文名词首次出现时可以写成 `term（中文说明）`，后文直接使用英文；中文说明用于解释含义，不另造中文术语。已有广泛接受中文译法的词仍使用中文，例如“代码”而不是 `code`。
- 不造词，不使用生僻译法。写“事件层”“测试框架”“实现阶段”，不写“事件缝”“测试底座”“实现地平线”“回灌”。不确定译法时先列出候选词让用户决定。
- 路径、`file:line`、API 字段、命令、错误信息、测试名保持原文。直接引用可以保留英文，其余说明必须是自然中文，不能写成中英词语交替的混合句。
- 每个事实要有证据：`file:line`、可复现命令、或实测输出。
- **不许编数字。** 没有探测就写 `待探测`，没有测量就写 `待测`。任何数字都必须能重新运行得到。

## 代码约定

- Python ≥ 3.10。只用现有依赖（pydantic / anthropic / openai / httpx / mcp / prompt_toolkit / tiktoken / pyyaml）。**新增依赖必须在 ADR 里说明理由。**
- **不引入 agent 框架**（LangGraph、pydantic-ai 等）。自己掌握 agent loop 是这个项目最重要的学习内容。
- 优先外科手术式改动：新模块 + 少量挂钩，而不是重写。要能说清楚改了哪些行、为什么。
- 新代码的测试必须离线运行（用 FakeLLM）。访问真实 API 的测试加 marker 并默认跳过。
- **不要为了让测试通过而删除或放宽断言。** 见 `docs/PITFALLS.md#P-004`。

## 跑测试

**不要直接跑 `pytest`。** `tests/test_llm.py`、`tests/test_llm_clients.py`、`tests/test_integration.py` 会读 `mini_agent/config/config.yaml` 打**真实 API**，实测 240 秒跑不完（exit 124），而且花钱。

离线可跑的子集：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tools.py tests/test_bash_tool.py tests/test_skill_loader.py tests/test_skill_tool.py tests/test_note_tool.py tests/test_tool_schema.py tests/test_terminal_utils.py tests/test_session_integration.py tests/test_markdown_links.py
```

`tests/test_acp.py` 当前是**红的**，那是个真实问题（`mini_agent/acp/__init__.py:111`），不是环境问题，**不要“修”测试**。

## 和我协作时

- 讨论改动默认对标 Claude Code / Codex 的设计，而不是"能跑就行"
- 提方案要说清楚三件事：它解锁了什么、怎么量化、你否决了什么
- 发现上游代码里的坑，先确认能复现，再记进 `docs/PITFALLS.md`

## 红线

- 不要提交 `config.yaml` / `mcp.json`（含 API key，已在 `.gitignore` 里）
- 不要 push，不要直接往 `main` 提交，除非我明确要求
- 不要动 `mini_agent/skills/`（上游子模块）
- 不要为了让演示效果好看而建议切换端点或换模型（“把 api_base 指向 api.anthropic.com 就能跑通这一半”这类话，一律不写）
- 不要“顺手”修与当前任务无关的问题——记进 `PITFALLS.md` 或 `KNOWN_ISSUES.md`，保持改动范围清晰

## 目录索引

| 路径 | 是什么 |
|---|---|
| [`docs/mechanisms.md`](docs/mechanisms.md) | 机制状态、边界与验证方式 |
| [`docs/decisions/`](docs/decisions/) | ADR，一条一个文件 |
| [`docs/PITFALLS.md`](docs/PITFALLS.md) | 可复现问题记录，新的在最上面 |
| [`docs/BUILD_LIST.md`](docs/BUILD_LIST.md) | 实现顺序、依赖与完成标准 |
| [`docs/UPSTREAM_AUDIT.md`](docs/UPSTREAM_AUDIT.md) | 上游 baseline 审计 |
| [`docs/specs/`](docs/specs/) | 当前和下一阶段的实现规格；后期只留设计说明 |
| [`docs/reference/`](docs/reference/) | 外部源码调研 |
| [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md) | 端点能力矩阵与降级方案 |

## 会话开始时

读三样东西再动手：`docs/mechanisms.md`（当前进度）、`docs/PITFALLS.md` 最近三条、以及 `docs/BUILD_LIST.md`（当前阶段与依赖）。

然后按「动手之前：先给改动前简报」出简报——**读完就改代码是不允许的**。
