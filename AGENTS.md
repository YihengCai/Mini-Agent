# AGENTS.md

给在这个仓库里干活的 agent——以及未来的我。**动手之前先读完这一页。**

## 这个项目是什么

项目派生自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)，baseline 对应提交是 `953b943`。上游提供 agent loop、工具、CLI、MCP/skill 加载器和 ACP 适配器，约 4.1k LOC。

**我的目标：研究真实 coding agent（Claude Code / Codex 等）的核心设计问题，把这个演示项目逐步改造成采用现代设计的 coding agent，并在真实 agent loop 上亲手实现、验证每个模块。这是学习项目；没有测试证据的能力不宣称已经实现或达到 SOTA。**

取舍标准只有三条：

1. **问题深度** —— 这个模块有没有“直觉实现会失败”的非显然边界？没有的话按普通功能处理，不为它制造研究叙事。
2. **测试敏感性** —— 能不能用一个一分钟内、不需要 API key 的离线回归测试证明它，并且删除关键实现后测试会转红？
3. **半成品的风险** —— 多数模块可以分阶段实现；沙箱和检查点不行。无法一次完成关键验证时，不进入实现，也不提前展开规格。

## 记录规范

这个项目的产出不只是代码，还有实现过程中真实发生的“为什么”。记录必须晚于问题出现，不能替未来的自己预写结论。

### 什么时候写一条 ADR（`docs/decisions/`）

只有当前实现已经开始，并且实际做出下列选择之一时才写：

1. 改变模块边界、公开 contract、状态所有权或安全模型；
2. 在两个都能落地的方案间作出会影响后续实现的选择；
3. 因已经出现的证据决定放弃原计划；
4. 推翻已经落地或已经由用户确认的 ADR。

候选方案只写在改动前简报里。未来阶段的设想、普通实现细节、命名、路线图排序和 agent 自己提出又取消的功能都不写 ADR。未经用户确认或实现检验，不得标为“已采纳”。

格式见 [`docs/decisions/0000-template.md`](docs/decisions/0000-template.md)。**「为什么否决其他的」是必写项**，而且要写清楚"在什么条件下被否决的那个反而是对的"——回顾和面试的价值主要在这一节。

### 什么时候写一条 PITFALL（`docs/PITFALLS.md`）

PITFALL 只记录实现或诊断过程中亲历的错误假设：我原以为是 A，实际证据证明是 B，而且这条教训能迁移到其他 agent 项目。

每条必须有**可复现命令**和**一句能迁移到别的 agent 项目上的教训**。没有复现命令就先做出复现再记——记不下来的坑等于没踩过。

对 baseline 的静态审计发现属于 [`docs/UPSTREAM_AUDIT.md`](docs/UPSTREAM_AUDIT.md)，不是个人踩坑。未来模块的风险预研属于改动前简报或外部调研，也不是 PITFALL。

### 写的时机

当前实现中真正做完决定后，在同一个会话里写；不要为尚未开始的阶段提前建档。写完在回复里用一行说明，方便我当场校对。

### 完成一项当前工作之后

1. 实现和离线回归测试都落地后，在 README 更新真实能力；
2. 如果过程中确有 ADR，补“回头看”：实际结果、偏差、是否需要新 ADR；
3. 如果亲历了可复现的错误假设，当场补 PITFALL。

## 动手之前：先给「改动前简报」

**任何修改或新增实现代码之前，先停下来给我一份简报，然后等我回应再动手。** 我需要在你改之前就看清边界、决策点和进度，而不是从代码差异里倒推。

简报固定五段，**总共不超过 15 行**，不要贴大段代码——它是地图，不是复述：

1. **现状**：这块现在怎么工作。讲清调用关系和状态，带 `file:line`。要说清“谁调谁、状态存在哪、谁持有它”，不要罗列文件。
2. **边界**：这次**会**改哪些文件和大致行区间；同样重要的是**不会**碰什么（尤其是看起来相关但我决定不动的部分）。
3. **决策点**：有哪几种做法、我打算选哪个、一句话理由。只有选择真正进入实现并命中 ADR 条件时才建档。
4. **影响**：会不会动到现有不变量、测试、公开 contract 或已实现能力。
5. **进度**：这一步对应 [`docs/BUILD_LIST.md`](docs/BUILD_LIST.md) 的哪项当前工作，完成后解锁什么。

什么时候可以省成一句话：**单文件、不引入新概念、不改任何接口或不变量**的改动（改错别字、补一条断言、调一个已经定好的常量）。除此之外一律走完整简报——包括"顺手重构一下"，那种恰恰最需要先划边界。

什么时候可以不等我确认：我已经明确说了"直接做"、"按 ADR-000X 实现"，或者本轮任务本身就是我下的具体指令。**其余情况给完简报就停。**

## 模型服务约束（硬约束）

这个项目跑在**当前配置的端点**上（现在是 MiniMax，走 Anthropic 兼容协议）。出于成本和兼容性：**不采用 OpenAI API，不采用 DeepSeek 模型；任何设计也不要推广或依赖 Anthropic 的模型与 API。**

要分清三件事，别混为一谈：

1. **协议格式** —— Anthropic 兼容消息协议是当前端点的兼容层。`mini_agent/llm/anthropic_client.py` 是协议客户端，不表示依赖 Anthropic 模型。
2. **vendor 能力** —— `cache_control`、服务端 `context_management`、extended-thinking 签名往返、`usage` 语义等一律先探测再依赖，结果记进 [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md)。
3. **外部设计** —— Anthropic / Claude Code / Codex / aider 的公开设计可以参考，但要写清楚借鉴内容以及为什么适用于本项目。

由此产生两条可执行的规则：

- 任何依赖 vendor 能力的当前设计必须引用能力记录；没有实测结论时，规格必须写明降级方案。
- 外部设计的价值可以引用 vendor 文档，但数字必须来自本项目端点的真实探测。外部数字必须标明来源，不能写成本项目结论。

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
- 新代码的回归测试必须离线运行；涉及模型调用时使用 LLM 测试替身。访问真实 API 的测试加 marker 并默认跳过。
- **不要为了让测试通过而删除或放宽断言。**

## 跑测试

**不要直接跑 `pytest`。** `tests/test_llm.py`、`tests/test_llm_clients.py`、`tests/test_integration.py` 会读 `mini_agent/config/config.yaml` 打**真实 API**，实测 240 秒跑不完（exit 124），而且花钱。

离线可跑的子集：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tools.py tests/test_bash_tool.py tests/test_skill_loader.py tests/test_skill_tool.py tests/test_note_tool.py tests/test_tool_schema.py tests/test_terminal_utils.py tests/test_session_integration.py tests/test_markdown_links.py tests/test_agent_loop_offline.py tests/test_agent_session_offline.py
```

## 和我协作时

- 讨论改动默认对标 Claude Code / Codex 的设计，而不是"能跑就行"
- 提方案要说清楚三件事：它解锁了什么、怎么量化、你否决了什么
- 发现上游代码缺陷，先确认能复现，再记进 `docs/UPSTREAM_AUDIT.md`；只有自己实现或诊断时的错误假设才记 PITFALL

## 红线

- 不要提交 `config.yaml` / `mcp.json`（含 API key，已在 `.gitignore` 里）
- 不要 push，不要直接往 `main` 提交，除非我明确要求
- 不要动 `mini_agent/skills/`（上游子模块）
- 不要为了让演示效果好看而建议切换端点或换模型（“把 api_base 指向 api.anthropic.com 就能跑通这一半”这类话，一律不写）
- 不要“顺手”修与当前任务无关的问题——baseline 问题记进 `docs/UPSTREAM_AUDIT.md`，保持改动范围清晰

## 目录索引

| 路径 | 是什么 |
|---|---|
| [`docs/decisions/`](docs/decisions/) | ADR，一条一个文件 |
| [`docs/PITFALLS.md`](docs/PITFALLS.md) | 实现过程中亲历且可复现的错误假设 |
| [`docs/BUILD_LIST.md`](docs/BUILD_LIST.md) | 当前工作与待研究问题 |
| [`docs/UPSTREAM_AUDIT.md`](docs/UPSTREAM_AUDIT.md) | 上游 baseline 审计 |
| [`docs/specs/`](docs/specs/) | 仅当前实现的短规格 |
| [`docs/reference/`](docs/reference/) | 外部源码调研 |
| [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md) | 已实际运行的端点能力探测 |

## 会话开始时

先读 `docs/BUILD_LIST.md` 的当前工作；若已有相关实现记录，再读对应 ADR 和 PITFALL。然后按「动手之前：先给改动前简报」出简报——**读完就改代码是不允许的**。
