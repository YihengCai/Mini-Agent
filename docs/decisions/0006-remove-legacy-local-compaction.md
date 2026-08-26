# ADR-0006：删除旧本地压缩，暂以完整历史直传

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/core/agent.py:90-127,227-390`、`mini_agent/core/events.py:11-82`、`mini_agent/config.py:111-164`、`tests/test_agent_session_offline.py:113-143`、`tests/test_llm_adapters.py:100-107`、提交 `3cbf242`

## 背景

旧实现由 `AgentSession` 同时持有模型可见消息、本地 token 估算、触发标志和摘要替换状态，每个 Step 前由 agent loop 调用摘要模型。它用固定的 `cl100k_base` 估算任意配置端点，并把生成的摘要伪装成新的 user 消息。`git show d8c5b06:mini_agent/core/agent.py | nl -ba | sed -n '239,497p'` 可复查该路径；其中摘要返回空字符串时仍会用只含 system 与原 user 消息的 `new_messages` 替换历史，已经执行的 assistant 与工具结果会被静默删除。

ADR-0005 已因 tokenizer、上下文上限和 `usage` 语义均未探测而把该能力默认关闭，但公开构造参数、配置、四类事件、摘要模型调用、测试用途标签和 `tiktoken` 依赖仍保留。默认关闭降低了触发概率，没有消除错误状态所有权或误启用风险。

## 选项

1. **保留默认关闭的旧实现**：不再扩大差异，但继续维护一套已知会破坏历史的公开 contract 与依赖。
2. **修补旧本地压缩**：补空摘要保护和更多本地估算规则，继续由 Session 原地替换模型历史。
3. **立即改用 vendor 上下文能力**：让 adapter 调用服务端上下文管理或依赖端点报告的用量。
4. **完整删除并明确降级**：Session 暂时保留完整模型历史，删除自动压缩状态、配置、事件、摘要调用与专用依赖；上下文管理作为后续独立研究重新设计。

## 决定

采用选项 4。`AgentSession` 只持有当前模型可见的完整消息序列，agent loop 每个 Step 直接读取该序列；adapter 报告的 `usage` 只保留为观察数据，不触发控制策略（`mini_agent/core/agent.py:122-127,252-267,329-390`）。当前明确没有自动上下文预算或压缩，长会话可能触及端点限制。

同时删除 `token_limit` 构造参数、`local_compaction_token_limit` 配置能力、四类 `Compaction*` 事件、摘要模型调用、固定 tokenizer 估算和 `tiktoken`/`regex` 依赖。旧配置键不静默忽略，而是在加载时明确报已删除（`mini_agent/config.py:116-120`）。删除摘要后所有模型事件都属于 Step，测试替身继续使用一个严格 FIFO，但不再保存恒定的用途标签（`tests/llm_test_double.py:12-27,73-119`）。这推翻 ADR-0001 的用途标签决定；其全局顺序、耗尽、剩余响应和首个违规保持可见等不变量继续保留。

本 ADR 不设计新的会话事实层、请求视图、上下文预算、截断策略、摘要格式或 vendor 降级方案，也不改变模型 adapter contract。

## 为什么否决其他的

- **保留默认关闭的旧实现**：已知危险路径不会因默认值而变安全，还会让配置和事件暗示项目拥有可用的上下文策略。如果近期已经有经过验证的替代实现要复用同一公开 contract，而且能保证旧路径不可触发，短期保留才可能降低迁移成本；当前没有这种实现。
- **修补旧本地压缩**：空摘要保护只能修一个症状，固定 tokenizer、把摘要伪装成 user 消息、事实与请求视图共用一份可变列表等边界仍未解决。如果目标端点的 tokenizer 和预算已经实测，摘要保真与工具调用配对也有离线变异测试，本地压缩反而可能是可移植的降级方案。
- **立即改用 vendor 上下文能力**：当前能力记录没有端点探测，直接依赖会违反“先探测再使用”的硬约束，并把 vendor 语义泄漏进 core。如果目标端点已经探测通过，adapter 能隔离差异，且另有不依赖该能力的明确降级方案，服务端管理才可能更可靠。

## 怎么验证它是对的

```bash
.venv/bin/python -m pytest -q \
  tests/test_agent_loop_offline.py \
  tests/test_agent_session_offline.py \
  tests/test_llm_adapters.py \
  tests/test_session_integration.py
UV_CACHE_DIR=/tmp/mini-agent-uv-cache uv lock --check
rg -n 'tiktoken|^name = "regex"$|\{ name = "regex"' pyproject.toml uv.lock
```

第一条命令实测为 `64 passed in 0.86s`；跨 Turn 测试证明第二次模型请求仍带第一轮完整 user/assistant 消息（`tests/test_agent_session_offline.py:112-143`），配置测试证明旧键明确失败。锁文件校验实测解析 `59` 个包；最后一条命令无输出，证明被删除依赖没有以孤儿包块残留。

## 回头看

实现实际删除了生产路径中的本地估算与摘要状态，也删除了只为区分摘要调用存在的事件和测试用途标签；普通模型事件、严格 FIFO、工具调用配对与报告用量观察均保持。定向回归、锁文件校验和标准离线集合 `157 passed in 10.08s` 均通过，没有引入替代压缩实现，也没有探测真实端点。

本次偏差是旧配置键不能随字段一起简单消失：`Config.from_yaml()` 会忽略未手工读取的根级键，所以最终增加了定向迁移错误，避免旧配置静默失效。通用未知字段治理仍留给后续配置单一真源工作。

2026-08-26：[ADR-0028](0028-config-file-matches-runtime-model.md) 让 YAML 直接经过严格运行时模型后，旧 `local_compaction_token_limit` 改为普通未知字段错误，不再需要专用迁移分支。压缩实现、配置能力与依赖仍保持删除，本决策不变。

同日，[ADR-0033](0033-keep-usage-on-model-response-events.md) 删除了 Session 中名为总计、实为最近一次非空响应值的 usage 镜像；adapter 映射仍随 `ModelResponse` 事件供观察，未经探测不进入控制策略的决定不变。
