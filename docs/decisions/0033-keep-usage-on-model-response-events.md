# ADR-0033：usage 只保留在模型响应事件

- 日期：2026-08-26
- 状态：已采纳
- 关联：`mini_agent/core/agent.py`、`mini_agent/core/events.py`、`mini_agent/cli.py`、`tests/test_agent_loop_offline.py`

## 背景

提交 `b7ea4c4` 中，agent loop 遇到带 `usage` 的响应就用 `response.usage.total_tokens` 覆盖 Session 的 `_api_total_tokens`；后续响应没有 `usage` 时继续保留旧值（`git show b7ea4c4:mini_agent/core/agent.py | nl -ba | sed -n '120,165p;340,370p'`）。CLI 把这个“最近一次非空响应值”显示为累计含义的 `API Tokens Used`（`git show b7ea4c4:mini_agent/cli.py | nl -ba | sed -n '230,255p'`）。

两个 adapter 已把原始用量放进各自的 `LLMResponse.usage`，core 又通过 `ModelResponse.response` 发出同一响应快照。Session 镜像既不保存逐次事实，也没有经端点探测证明可以跨请求求和，却增加第二份状态并误导 CLI。

## 选项

1. **保留最后一次非空值并改名**：可做即时展示，但仍丢失每次响应的归属，且没有当前消费者。
2. **在 Session 中累计**：看似符合 CLI 文案，却假定不同 adapter、缓存与服务端扩展的 `usage` 可直接相加。
3. **只保留每个响应的原值**：删除 Session 镜像与累计文案；需要统计时由未来事件消费者按实测语义计算。

## 决定

采用选项 3。删除 `_api_total_tokens`、`AgentSession.api_total_tokens`、每 Step 覆盖逻辑和 CLI 的 `API Tokens Used` 行。adapter 的 `TokenUsage` 映射、`LLMResponse.usage` 与 `ModelResponse.response` 不变；usage 仍是观察数据，不进入上下文策略。

本项不新增统计器，不推断缺失 usage，不把 prompt/completion/total 字段跨协议归一成成本，也不运行真实端点探测。若以后任务级评测需要 token 或成本，应由事件消费者保留每次响应身份，并引用 `docs/PROVIDER_CAPABILITIES.md` 的实测语义。

## 为什么否决其他的

**否决保留最后值**：它只有在 UI 明确显示“最近一次模型响应”且有真实调试需求时才有意义；当前 `/stats` 展示 Session 统计，名称与状态生命周期冲突。

**否决直接累计**：相加能生成数字，却不能证明数字可比较或覆盖全部请求。若目标端点的缓存、推理、服务端上下文管理和缺失字段语义都已探测，且消费者定义了统计口径，Session 或独立聚合器才可以累计。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q tests/test_agent_loop_offline.py::test_reported_usage_remains_on_model_response_events tests/test_llm_adapters.py` 实测 `36 passed in 0.62s`。
- 回归让第一响应报告 `999999`、末响应不报告 usage；两个 `ModelResponse` 事件分别保留 `TokenUsage(total_tokens=999999)` 与 `None`，Session 不再暴露 `api_total_tokens`。
- 临时恢复公开镜像属性后，该回归实测 `1 failed in 0.48s`。
- `.venv/bin/python -m pytest -q` 实测 `270 passed, 8 deselected in 12.93s`；真实模型、用户 MCP 配置和网络测试未运行。

## 回头看

生产代码净减 17 行；测试只把对 Session 镜像的单值断言改为两个响应事件的逐项断言。删除的是误命名的派生状态，不是 adapter 已报告的原始观察数据。
