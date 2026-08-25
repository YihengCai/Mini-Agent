# 上游 baseline 审计

> 这不是路线图。当前工作和待研究问题以 [BUILD_LIST.md](BUILD_LIST.md) 为准。
>
> 审计对象：上游提交 `953b943`。以下 `file:line` 均对该提交核实。这里记录 baseline 缺陷；[`PITFALLS.md`](PITFALLS.md) 只记录后续实现过程中亲历的错误假设。

## 上游 baseline

上游是一个可运行的 agent 演示项目：

```text
cli.run_agent()
  -> Config.load()
  -> initialize_base_tools() + add_workspace_tools()
  -> Agent(llm, system_prompt, tools)
  -> Agent.run()
       -> 压缩消息历史
       -> llm.generate()
       -> 串行执行工具调用
       -> 追加工具结果
```

它已经提供：

- 与模型服务无关的 `Message` / `LLMResponse` 数据结构；
- Anthropic 兼容与 OpenAI 兼容客户端；
- Read/Write/Edit/Bash/Note 工具；
- MCP 与 skills 加载器；
- 交互式 CLI、非交互任务、日志和 ACP 入口。

它适合学习改造：agent loop 真实存在，关键组件仍足够小，可以逐步替换而不用引入 agent 框架。

## 主要设计问题

### 1. agent loop、渲染器与适配器耦合

`Agent.run()`（`mini_agent/agent.py:294-492`）直接输出步骤边框、推理内容、工具参数和结果；核心模块导入终端颜色与宽度工具。ACP 在 `mini_agent/acp/__init__.py:127-165` 复制了另一份循环，而且这份副本缺少主循环的压缩与日志路径。

要研究的问题：怎样让核心循环只保留控制流，并让 CLI、ACP 和测试共享同一条执行路径。具体输出接口尚未决定。

实现结果没有改写以上 baseline 证据：当前选择删除无真实客户端验证的 ACP，并用同步事件连接 core 与 CLI，见 [ADR-0003](decisions/0003-remove-acp-and-extract-core-loop.md)。

### 2. 消息历史不是可靠日志

`_summarize_messages()`（`mini_agent/agent.py:153-232`）原地重写 `self.messages`；`_cleanup_incomplete_messages()`（`mini_agent/agent.py:73-94`）在取消时截断尾部。工具副作用已经发生后再删除消息，会让模型可见的消息历史与磁盘状态不一致。

要研究的问题：怎样区分已经发生的记录与发给模型的请求视图；取消处理不能删除已经发生副作用的工具记录。

### 3. 工具执行没有统一策略层

主循环在 `mini_agent/agent.py:436` 直接调用 `tool.execute(**arguments)`；ACP 又有独立分发逻辑。Bash 接受原始 shell 字符串，文件工具不限制工作区边界，当前没有权限请求或操作系统沙箱。

要研究的问题：怎样建立统一的工具策略边界；权限判断负责表达用户意图，操作系统沙箱负责内核强制执行。

### 4. 编辑 contract 与实现不一致

`EditTool` 描述要求 `old_str` 唯一，`mini_agent/tools/file_tools.py:280` 却使用不限制次数的 `str.replace()`；成功结果不含替换次数、代码差异或诊断。

要研究的问题：唯一匹配、读取后文件变化检查、修改块预检、受限写入和限定在代码差异范围内的诊断。

### 5. 输出与搜索没有预算语义

项目没有 Glob/Grep 工具；搜索依赖 bash。`BashOutputResult.format_content()`（`mini_agent/tools/bash_tool.py:32-49`）没有硬限制，错误 stderr 还可绕过格式化逻辑进入消息历史。

要研究的问题：结构化搜索、忽略规则、显式截断元数据，以及成功与错误输出的统一限制。

实现结果没有改写以上 baseline 证据：当前在原始 `ToolFinished` 之后统一生成每条最多 64 KiB 的模型可见投影，成功与失败共用 UTF-8 字节预算，见 [ADR-0010](decisions/0010-model-facing-tool-output-budget.md)。原始事件、日志、工具缓冲与整批合计仍不受该预算限制。

### 6. 测试无法证明 agent loop 正确

现有测试混入真实 API，部分测试以 `return True/False` 代替断言并吞掉异常；离线测试集也包含已知 ACP 故障。

要研究的问题：可编排的 LLM 测试替身、模型请求中工具调用与结果的配对检查、可区分的结束原因和显式在线测试标记。

后续状态：脚本化 LLM 替身、配对检查与结构化停止原因已经落地；真实模型、用户 MCP 配置和网络测试现由 [ADR-0007](decisions/0007-explicit-opt-in-for-external-tests.md) 统一标记并从默认 pytest 排除。两份上游真实模型演示的弱断言仍保留，只是不再属于默认回归集合。

### 7. 模型服务能力未经探测

代码使用 Anthropic 兼容协议，不等于当前端点支持所有 vendor 扩展。缓存、流式输出、`thinking` 签名、并行工具调用与上下文上限都必须按 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 探测后再依赖。

### 8. 后台 shell 没有宿主资源所有权

`BackgroundShellManager` 把 shell 和 monitor 任务放在类变量，不同工具实例、CLI runtime 和事件循环因而共享可变状态（`mini_agent/tools/bash_tool.py:108-127`）。monitor 取消只调用 `task.cancel()` 就删除登记，没有等待取消清理收敛；温和终止超时后强杀，也没有再次等待 subprocess（`mini_agent/tools/bash_tool.py:96-105,135-188`）。

CLI 分别构造启动、读取与终止工具，退出路径却只调用 MCP 清理（`mini_agent/cli.py:303-328,399-448,805-806`）。这使后台进程和监控任务无法与创建它们的宿主生命周期对齐。

实现结果没有改写以上 baseline 证据：当前由一次 CLI runtime 持有实例 manager，显式注入三个工具，并在 shell、MCP 统一退出边界中等待收敛，见 [ADR-0009](decisions/0009-runtime-owned-background-shells.md)。

### 9. MCP 超时与连接使用进程级全局状态

默认超时是模块级可变对象，已经构造的 `MCPServerConnection` 在每次取值时重新读取它（`mini_agent/tools/mcp_loader.py:21-57,159-169`）。连接也只在 `await connect()` 成功后进入全局表，cleanup 会关闭并清空进程中的全部连接（`mini_agent/tools/mcp_loader.py:284-285,397-433`）；因此两个宿主无法隔离超时和关闭责任，连接建立期间取消还会落在登记之前。

实现结果没有改写以上 baseline 证据：当前由一次 CLI runtime 持有不可变超时快照和连接 manager，在首个连接 `await` 前登记，并串行关闭自己拥有的连接，见 [ADR-0011](decisions/0011-runtime-owned-mcp-connections.md)。

## 已确认缺陷索引

| 缺陷 | 证据 | 去向 |
|---|---|---|
| `edit_file` 多处/空串替换 | `mini_agent/tools/file_tools.py:273-281` | 当前已由唯一匹配校验修复；回归见 `tests/test_tools.py:333-408` |
| `read_file` 先全量读取再近似截断 | `mini_agent/tools/file_tools.py:11-60,123-148` | 当前改为 2000 行/50 KiB 有界窗口；回归见 `tests/test_tools.py:14-184` |
| `write_file` / `edit_file` 直接覆写，编辑时 CRLF 会被文本读取归一化 | `mini_agent/tools/file_tools.py:195-209,271-281` | 当前改为同目录原子替换并保留已有换行约定与权限位；回归见 `tests/test_tools.py:187-430` |
| 摘要失败可使上下文变大 | `mini_agent/agent.py:257-292` | 摘要输入和异常降级都没有大小上限 |
| 取消操作会截断已完成记录 | `mini_agent/agent.py:73-94,397-398,477-478` | 清理逻辑不检查工具调用标识符是否已经配对 |
| 测试返回值不会让 pytest 失败 | `tests/test_agent.py:72-94,146-161` | 测试返回布尔值并吞掉异常，而不是断言 |
| Note 读取工具未进入运行时注册表 | `mini_agent/cli.py:429-432`、`mini_agent/acp/__init__.py:100-102` | CLI 与 ACP 都经共享组装得到写入工具，但都没有注册 `RecallNoteTool` |
| 后台 shell 使用进程级共享表 | `mini_agent/tools/bash_tool.py:108-127` | 当前已改为 CLI runtime 持有的实例 manager；隔离回归见 `tests/test_background_shell_lifecycle.py` |
| monitor 取消和强杀不等待收敛 | `mini_agent/tools/bash_tool.py:96-105,181-188` | 当前 terminate/close 会等待 monitor，强杀后再次等待 subprocess |
| CLI 没有后台 shell 关闭入口 | `mini_agent/cli.py:805-806` | 当前正常、异常和取消路径都按 shell、MCP 顺序清理；取舍见 ADR-0009 |
| MCP 超时与连接使用进程级全局状态 | `mini_agent/tools/mcp_loader.py:21-57,159-169,284-285,397-433` | 当前由 CLI runtime 的 `MCPManager` 隔离并关闭；取消与重试回归见 `tests/test_mcp_runtime_ownership.py` |
| 成功与失败工具输出可无界进入模型历史 | `mini_agent/tools/bash_tool.py:32-49`、`mini_agent/agent.py:436-469` | 当前由批次执行器统一生成每条最多 64 KiB 的模型投影；原始事件与日志保持完整，取舍见 ADR-0010 |
| 配置解析重复默认值并静默忽略未知键 | `git show 7a013e9^:mini_agent/config.py` 的 `14-192` | 当前从模型字段派生根级分片，并由共享严格模型拒绝未知键；回归见 `tests/test_llm_adapters.py:48-255`，取舍见 ADR-0012 |
| Note 写入把损坏存储当成空列表并覆盖 | `git show 358f561^:mini_agent/tools/note_tool.py` 的 `69-114` | 当前读写工具共享对象数组校验，任何已有无效存储都失败并保留原字节；回归见 `tests/test_note_tool.py:60-90`，取舍见 ADR-0013 |
| MCP `isError` 正文没有进入内部错误字段 | `git show e6dded1^:mini_agent/tools/mcp_loader.py` 的 `72-84` | 当前在 MCP 转换边界把非空正文映射到 `ToolResult.error`；直接与批次回归见 `tests/test_mcp_tool_results.py` |
| 非正 `max_steps` 会接纳零模型请求的伪 Turn | `git show 768dd64^:mini_agent/core/agent.py` 的 `91-121,161-216,229-286` | 当前配置与公开 Session 构造入口都要求正数，且在 runtime 或文件副作用前失败；取舍见 ADR-0014 |
| 任意 `Current Workspace` 子串可抑制真实工作区事实 | `git show 95dfaa7^:mini_agent/core/agent.py` 的 `113-119` | 当前只有含本次绝对路径的完整事实块能抑制追加；模型请求回归见 `tests/test_agent_session_offline.py:153-188` |
| 主配置与系统提示词、MCP 配置会混用不同搜索来源 | `git show 167a839^:mini_agent/cli.py` 的 `544-680` | 当前相对伴随路径绑定已选主配置父目录，绝对路径保持；来源隔离回归见 `tests/test_config_provenance.py`，取舍见 ADR-0015 |
| 未知显式 MCP `type` 会被推断成其他 transport | `git show 3fe5709^:mini_agent/tools/mcp_loader.py` 的 `163-177,267-275,345-388` | 当前只对缺失字段自动推断，非法显式值隔离当前 server；回归见 `tests/test_mcp.py:67-76,297-348`，取舍见 ADR-0016 |
| 负 `max_retries` 会跳过首次调用并抛通用错误 | `git show 08c9f20^:mini_agent/retry.py` 的 `23-61,97-138` | 当前配置与运行时入口都要求非负，零值仍调用一次；回归见 `tests/test_retry.py`，取舍见 ADR-0017 |
| 非法退避数值会绕过、挂起或溢出有限上限 | `git show 1ce3dd6^:mini_agent/retry.py` 的 `23-75` | 当前两层入口要求有限定义域，零初值和有限幂溢出返回有界结果；回归见 `tests/test_retry.py:13-94`，取舍见 ADR-0018 |
| 同秒 Turn 日志使用同一路径并覆写已有事实 | `git show 1581771^:mini_agent/logger.py` 的 `19-41` | 当前以排他创建和确定性后缀独占新文件；回归见 `tests/test_logger.py`，取舍见 ADR-0019 |
| Skill 重扫保留已删除条目，重名来源静默覆盖 | `git show 9c15477^:mini_agent/tools/skill_loader.py` 的 `194-214` | 当前完整扫描后一次替换注册表并拒绝重名；回归见 `tests/test_skill_loader.py:115-157`，取舍见 ADR-0020 |
| `async_retry()` 忽略 `enabled`，两个 adapter 重复解释开关 | `git show 262761f^:mini_agent/retry.py` 的 `24-58,87-143`；`git show 262761f^:mini_agent/llm/anthropic_client.py` 的 `240-277`；`git show 262761f^:mini_agent/llm/openai_client.py` 的 `233-268` | 当前由重试 wrapper 单一持有开关，adapter 只保留协议调用；回归见 `tests/test_retry.py:98-131`，取舍见 ADR-0021 |

## 审计不直接决定实现

外部项目拥有某项功能，不构成本项目实现它的理由。以下内容必须先有本仓库能够暴露当前缺陷的回归测试：

- Repo map / PageRank：没有代码定位失败案例和外部基准前不进入实现；
- vendor 专用缓存或上下文管理：先探测；
- 检查点：必须逐字节还原且不碰用户 Git；
- subagent：必须证明父级上下文隔离，不以“能启动”为完成；
- 任务基准测试：出现无法由单模块回归测试覆盖的真实问题后再建。

审计的作用是找到值得研究的问题，不是替代路线图或提前写完整产品架构。
