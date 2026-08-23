# 机制状态表

这页是项目进度的唯一真相源。README 只展示这里已经验证的能力；函数级设计在 [specs/](specs/)，取舍在 [decisions/](decisions/)，复现记录在 [PITFALLS.md](PITFALLS.md)。

状态：`待实现` / `待能力探测` / `实现中` / `已验证` / `后期设计` / `已取消`。只有实现、离线回归测试和 ADR 回看同时存在，才能标为 `已验证`。

| 机制 | 直觉实现为什么错 | 本项目边界 | 验收工件 | 状态 |
|---|---|---|---|---|
| [FakeLLM 路由](specs/00-measurement-rig_CN.md) | 单 FIFO 会被压缩器的带外 LLM 调用错消费 | `tools is None` 与主循环两条队列；耗尽和未消费都失败 | `tests/test_loop_scripted.py` | 待实现 |
| [全局历史不变量](specs/00-measurement-rig_CN.md) | 只在单个测试检查配对，其他路径仍可生成 orphan | 每次 fake 请求前检查 tool call/result 双向配对 | `assert_history_valid()` 的反例测试 | 待实现 |
| [事件缝](specs/02-event-seam-interrupt_CN.md) | 把 `print` 改成散落回调仍会复制控制流 | Agent 只发完整事件；CLI、ACP、JSONL 是 sink | `test_silent_mode`、renderer golden | 待实现 |
| [中断修复](specs/02-event-seam-interrupt_CN.md) | 截断历史会删除已落盘副作用的记录 | 为未完成 id 合成 tool result，绝不删除已完成结果 | `test_interrupt_leaves_no_orphan` + 无残留进程 | 待实现 |
| [Streaming / steering](specs/02-event-seam-interrupt_CN.md) | 首 token 后透明重试会拼接两个回答；轮次中注入会切断 tool 组 | provider 组装流；steering 只在 step 边界进入 | delta/retry 与 boundary 注入测试 | 待实现 |
| [分层上下文](specs/01-context-manager_CN.md) | 就地 prose 摘要会丢结构、重复摘要，失败时还能变大 | raw log 派生 view；tool eviction + 单一窗口摘要 | `tests/test_context_invariants.py` | 待实现 |
| [Prompt cache](specs/01-context-manager_CN.md) | 未测 endpoint 就设计断点会得到不可验证的空转代码 | C1–C3 通过后单独实现；否则不宣称支持 | `provider_probe` + 真实 usage 记录 | 待能力探测 |
| [事务性编辑](specs/04-transactional-edit_CN.md) | `str.replace()` 会静默多改；跨文件“原子”也常是假承诺 | 全量预验证、陈旧检查、单文件原子替换、范围诊断 | `tests/test_edit_engine.py` | 待实现 |
| [Glob/Grep 与截断](specs/05-code-retrieval_CN.md) | 静默截断会让模型把部分结果当全集 | ignore-aware 搜索、真实总数页脚、bash 头尾钳制 | `tests/test_search_tools.py` | 待实现 |
| [OS 沙箱](specs/03-sandbox-permissions_CN.md) | 检测到 `sandbox-exec` 不等于 profile 生效，错误配置会产生虚假安全感 | 先验证 macOS；其他平台未通过真实拒绝测试就不声明 | `scripts/sandbox_probe.py` | 待实现 |
| [结构化权限](specs/03-sandbox-permissions_CN.md) | `shlex.split()` 不是 shell parser，字符串白名单会放过复合命令 | 复合结构最严 verdict；解析失败 fail-closed；sandbox 才允许 ASK 降级 | `tests/test_permission_corpus.py` | 待实现 |
| AGENTS / SKILL 信任边界 | 文件文本进入 system role 会把项目内容升级成授权 | 文件、工具、MCP 都是带来源的数据；不能授权执行 | 恶意 AGENTS fixture 仍触发 ASK | 待实现 |
| Plan mode | prompt 里说“不要写”并没有移除写工具 | PLAN 下物理移除 mutating tools；人类决定闭合 exit tool call | 拒绝/接受后的历史与工具表测试 | 待实现 |
| [Checkpoint / rewind](specs/06-checkpoint-rewind_CN.md) | `git stash`/真实 index 会污染用户仓库，restore 还可能改行尾或漏新文件 | shadow store，同时恢复 raw log 与 ContextState | `tests/test_checkpoint.py` 的逐字节往返矩阵 | 后期设计 |
| [子 agent 隔离](specs/07-subagent-isolation_CN.md) | 返回自由散文只是嵌套聊天，父上下文仍不可控 | 只读工具、结构化有界报告、三类预算、证据验证 | `tests/test_subagent_isolation.py` | 后期设计 |
| Repo map / PageRank | 自写 gold 调出来的排序不可证伪，且不是当前前沿 agent 的必要路径 | 保留外部调研；实现预算投入 Glob/Grep | [ADR-0004](decisions/0004-cut-repomap-pagerank.md) | 已取消 |

## 维护规则

- 开始实现时改成 `实现中`；不要预填测试通过数、性能或成本。
- 标成 `已验证` 时，验收工件必须链接到真实测试名，状态列链接对应 ADR。
- 实现偏离 spec 时先记录偏离原因；模块边界或数据流改变则新开 ADR。
- endpoint、模型或 OS 相关结论进入 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 或平台 probe，不复制到多个文档。
