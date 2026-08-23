# AGENTS.md

给在这个仓库里干活的 agent —— 以及未来的我。**动手之前先读完这一页。**

## 这个项目是什么

fork 自 [MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)（fork 点 `953b943`）。上游写了 agent 循环、工具实现、CLI、MCP/skill 加载器和 ACP 桥，约 4.1k LOC。

**我的目标：研究真实 coding agent（Claude Code / Codex 等）的关键机制，把这个 demo 逐层改造成采用现代设计的 coding agent，并在真实循环上亲手实现、验证每个模块。这是学习型工程；没有测试证据的能力不宣称为已实现或 SOTA。**

取舍标准只有三条：

1. **机制深度** —— 这个模块有没有"直觉实现是错的"那一层？没有的话它只是功能，不是机制。
2. **可证伪** —— 能不能用一个一分钟内、不需要 API key、**会失败**的工件证明它是对的？
3. **半成品的方向** —— 多数模块半成品 = 学习作品；沙箱和检查点半成品 = 负分（用户会默默信任它们）。这两个要么做完整，要么只写设计文档。

## 记录规范（这一页最重要的一节）

这个项目的产出**不只是代码，还有"为什么"**。回顾靠它，面试也靠它。所以：

### 什么时候写一条 ADR（`docs/decisions/`）

命中任一就写，**在动手之前或紧随其后写，不要攒**：

1. 在两个及以上可行方案里做了选择——哪怕当时觉得显然
2. 决定**不做**某件事，或砍掉原计划里的东西
3. 改变了模块边界、实现顺序或依赖关系
4. 发现某个"标准做法"在这里不适用，走了别的路
5. 推翻了之前的 ADR（**新开一条**，把旧的状态改成 `已推翻（见 ADR-XXXX）`，不要原地改写历史）

不用写 ADR 的：纯实现细节、命名、任何 diff 本身就能说明白的事。

格式见 [`docs/decisions/0000-template.md`](docs/decisions/0000-template.md)。**「为什么否决其他的」是必写项**，而且要写清楚"在什么条件下被否决的那个反而是对的"——回顾和面试的价值主要在这一节。

### 什么时候写一条 PITFALL（`docs/PITFALLS.md`）

1. 我以为是 A、实际是 B，且这个误解花了超过 15 分钟
2. 某个 API / 工具的行为与文档不符或反直觉（`shlex`、seatbelt、git plumbing、Anthropic 对空 block 和孤立 `tool_use` 的 400 规则……）
3. 一个 bug 的根因不在它报错的位置
4. 一个"看起来能用"的实现其实在静默出错

每条必须有**可复现命令**和**一句能迁移到别的 agent 项目上的教训**。没有复现命令就先做出复现再记——记不下来的坑等于没踩过。

### 写的时机

**在同一个会话里做完决定就写，不要留到最后。** 事后补的 ADR 会丢掉当时否决的选项，而那正是最值钱的部分。写完在回复里用一行说明写了哪条，方便我当场校对。

### 落地一个机制之后

1. 更新 [`docs/mechanisms.md`](docs/mechanisms.md) 对应行：状态 + 抓回归的测试名
2. 补上对应 ADR 的「回头看」一节（实际效果、翻没翻车、要不要推翻）
3. 如果实现改变模块边界或数据流，先写新 ADR；普通实现细节以代码为准，及时收敛 spec，避免长期维护一份平行实现

## 动手之前：先给「改动前简报」

**任何修改或新增实现代码之前，先停下来给我一份简报，然后等我回应再动手。** 我需要在你改之前就看清边界、决策点和进度，而不是从 diff 里倒推。

简报固定五段，**总共不超过 15 行**，不要贴大段代码——它是地图，不是复述：

1. **现状**：这块现在怎么工作。讲机制，带 `file:line`。要说清"谁调谁、状态存在哪、谁持有它"，不要罗列文件。
2. **边界**：这次**会**改哪些文件和大致行区间；同样重要的是**不会**碰什么（尤其是看起来相关但我决定不动的部分）。
3. **决策点**：有哪几种做法、我打算选哪个、一句话理由。**这里如果出现真正的取舍，就是一条 ADR**（见上面的触发条件），简报里先点名，落地时补记录。
4. **影响**：会不会动到现有不变量、测试、或其他机制的假设；对 [`docs/mechanisms.md`](docs/mechanisms.md) 哪一行有影响。
5. **进度**：这一步属于 [`docs/BUILD_LIST_CN.md`](docs/BUILD_LIST_CN.md) 的哪个阶段、机制表哪一行，做完之后那一行的状态变成什么。

什么时候可以省成一句话：**单文件、不引入新概念、不改任何接口或不变量**的改动（改错别字、补一条断言、调一个已经定好的常量）。除此之外一律走完整简报——包括"顺手重构一下"，那种恰恰最需要先划边界。

什么时候可以不等我确认：我已经明确说了"直接做"、"按 ADR-000X 实现"，或者本轮任务本身就是我下的具体指令。**其余情况给完简报就停。**

## Provider 约束（硬约束）

这个项目跑在**当前配置的 endpoint** 上（现在是 MiniMax，走 Anthropic 兼容协议）。出于成本和兼容性：**不采用 OpenAI API，不采用 DeepSeek 模型；任何设计也不要推广或依赖 Anthropic 的模型与 API。**

要分清三件事，别混为一谈：

1. **Wire format** —— Anthropic 兼容的 messages 协议是当前 endpoint 的兼容层。`mini_agent/llm/anthropic_client.py` 是**协议客户端**，不是厂商站队，继续用。
2. **厂商专有能力** —— `cache_control`、server-side `context_management`、extended thinking 的 signature 往返、特定的 usage 字段语义……**一律先探测再依赖**，结果记进 [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md)。
3. **设计理念与公开文章** —— Anthropic / Claude Code / Codex / aider 的公开设计**可以且应该参考**，但要写清楚借鉴了什么、为什么在这里也成立。

由此产生两条可执行的规则：

- **任何依赖厂商能力的设计，必须引用能力矩阵里的一行。** 那一行还是「待测」，设计里就必须写降级路径（这个能力不可用时怎么办），不能假设它在。
- **机制的价值论证可以引用厂商文档**（例如"前缀缓存让重复前缀便宜一个数量级"这个道理），**但数字必须来自对本项目 endpoint 的实测**。引用来的数字要标注来源，且不能出现在结论里。

## 写作要求

- 中文，密度高，不要填充语和翻译腔。标识符、路径、`file:line`、API 字段名、命令、错误信息保留英文。
- 每个事实要有证据：`file:line`、可复现命令、或实测输出。
- **不许编数字。** 没实测过就写「待测」。任何写进文档的数字都要能被重跑出来——面试官第一件事就是重跑你的 bench。

## 代码约定

- Python ≥ 3.10。只用现有依赖（pydantic / anthropic / openai / httpx / mcp / prompt_toolkit / tiktoken / pyyaml）。**新增依赖必须在 ADR 里说明理由。**
- **不引入 agent 框架**（LangGraph、pydantic-ai 之类）。自己拥有那个循环是这个项目唯一有意思的地方。
- 优先外科手术式改动：新模块 + 少量挂钩，而不是重写。要能说清楚改了哪些行、为什么。
- 新代码的测试必须**离线可跑**（用假 LLM）。打真实 API 的测试标 marker 并默认跳过。
- **不要为了让测试通过而删断言或放宽断言。** 这个仓库已经有过一次教训，见 `docs/PITFALLS.md#P-004`。

## 跑测试

**不要直接跑 `pytest`。** `tests/test_llm.py`、`tests/test_llm_clients.py`、`tests/test_integration.py` 会读 `mini_agent/config/config.yaml` 打**真实 API**，实测 240 秒跑不完（exit 124），而且花钱。

离线可跑的子集：

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_tools.py tests/test_bash_tool.py tests/test_skill_loader.py tests/test_skill_tool.py tests/test_note_tool.py tests/test_tool_schema.py tests/test_terminal_utils.py tests/test_session_integration.py tests/test_markdown_links.py
```

`tests/test_acp.py` 当前是**红的**，那是个真 bug（`mini_agent/acp/__init__.py:111`），不是环境问题，**不要"修"测试**。

## 和我协作时

- 讨论改动默认对标 Claude Code / Codex 的设计，而不是"能跑就行"
- 提方案要说清楚三件事：它解锁了什么、怎么量化、你否决了什么
- 发现上游代码里的坑，先确认能复现，再记进 `docs/PITFALLS.md`

## 红线

- 不要提交 `config.yaml` / `mcp.json`（含 API key，已在 `.gitignore` 里）
- 不要 push，不要直接往 `main` 提交，除非我明确要求
- 不要动 `mini_agent/skills/`（upstream 的 submodule）
- 不要为了让 demo 好看而建议切换 endpoint 或换模型（"把 api_base 指向 api.anthropic.com 就能跑通这一半"这类话，一律不写）
- 不要"顺手"修与当前任务无关的 bug —— 记进 `PITFALLS.md` 或 `KNOWN_ISSUES.md`，保持 diff 可读

## 目录索引

| 路径 | 是什么 |
|---|---|
| [`docs/mechanisms.md`](docs/mechanisms.md) | **对外那一页**：机制 / 直觉实现 / 为什么错 / 我的做法 / 测试 / 状态 |
| [`docs/decisions/`](docs/decisions/) | 决策记录（ADR），一条一个文件，编号递增 |
| [`docs/PITFALLS.md`](docs/PITFALLS.md) | 踩坑日志，追加式，新的在最上面 |
| [`docs/BUILD_LIST_CN.md`](docs/BUILD_LIST_CN.md) | 实现顺序、依赖与完成门槛 |
| [`docs/AGENT_ROADMAP_CN.md`](docs/AGENT_ROADMAP_CN.md) | 上游基线审计（不是 roadmap） |
| [`docs/specs/`](docs/specs/) | 当前/下一阶段的中文实现规格；后期模块只留设计说明 |
| [`docs/reference/`](docs/reference/) | 外部调查：提供证据，不自动成为本项目方案 |
| [`docs/PROVIDER_CAPABILITIES.md`](docs/PROVIDER_CAPABILITIES.md) | endpoint 能力矩阵：哪些厂商能力实测可用，哪些必须降级 |

## 会话开始时

读三样东西再动手：`docs/mechanisms.md`（当前进度）、`docs/PITFALLS.md` 最近三条、以及 `docs/BUILD_LIST_CN.md`（当前阶段与依赖）。

然后按「动手之前：先给改动前简报」出简报——**读完就改代码是不允许的**。
