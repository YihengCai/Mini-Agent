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

### 6. 测试无法证明 agent loop 正确

现有测试混入真实 API，部分测试以 `return True/False` 代替断言并吞掉异常；离线测试集也包含已知 ACP 故障。

要研究的问题：可编排的 LLM 测试替身、模型请求中工具调用与结果的配对检查、可区分的结束原因和显式在线测试标记。

### 7. 模型服务能力未经探测

代码使用 Anthropic 兼容协议，不等于当前端点支持所有 vendor 扩展。缓存、流式输出、`thinking` 签名、并行工具调用与上下文上限都必须按 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 探测后再依赖。

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

## 审计不直接决定实现

外部项目拥有某项功能，不构成本项目实现它的理由。以下内容必须先有本仓库能够暴露当前缺陷的回归测试：

- Repo map / PageRank：没有代码定位失败案例和外部基准前不进入实现；
- vendor 专用缓存或上下文管理：先探测；
- 检查点：必须逐字节还原且不碰用户 Git；
- subagent：必须证明父级上下文隔离，不以“能启动”为完成；
- 任务基准测试：出现无法由单模块回归测试覆盖的真实问题后再建。

审计的作用是找到值得研究的问题，不是替代路线图或提前写完整产品架构。
