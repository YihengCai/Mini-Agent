# 上游基线审计

> 这不是 roadmap。实现顺序以 [BUILD_LIST_CN.md](BUILD_LIST_CN.md) 为准，机制状态以 [mechanisms.md](mechanisms.md) 为准。
>
> 审计基准：上游 commit `953b943`。以下 `file:line` 对该提交核实；已做成复现的缺陷进入 [PITFALLS.md](PITFALLS.md)。

## 项目基线

上游是一个可运行的单 agent demo：

```text
cli.run_agent()
  -> Config.load()
  -> initialize_base_tools() + add_workspace_tools()
  -> Agent(llm, system_prompt, tools)
  -> Agent.run()
       -> compact messages
       -> llm.generate()
       -> execute tool calls serially
       -> append tool results
```

它已经提供：

- provider-neutral 的内部 `Message` / `LLMResponse` schema；
- Anthropic-compatible 与 OpenAI-compatible 客户端；
- Read/Write/Edit/Bash/Note 工具；
- MCP 与 skills 加载；
- 交互 CLI、非交互 task、日志和 ACP 入口。

这使它适合学习改造：循环真实存在，但关键机制仍足够小，可以逐层替换而不用引入 agent framework。

## 关键结构债务

### 1. 引擎、渲染与适配器耦合

`Agent.run()`（`mini_agent/agent.py:294-492`）直接输出步骤框、thinking、工具参数和结果；核心模块导入终端颜色与宽度工具。ACP 在 `mini_agent/acp/__init__.py:127-165` 复制了另一份循环，且缺少主循环的压缩与日志路径。

学习切口：事件是引擎唯一输出，CLI/ACP/logger/eval 成为消费者。

### 2. 历史不是可靠日志

`_summarize_messages()`（`mini_agent/agent.py:153-232`）就地重写 `self.messages`；`_cleanup_incomplete_messages()`（`mini_agent/agent.py:73-94`）在取消时截断尾部。工具副作用已经发生后再删除消息，会让模型看到的世界与磁盘分叉。

学习切口：raw log 与 provider view 分离；中断通过补齐因果记录修复，而不是删除。

### 3. 工具执行没有统一策略层

主循环在 `mini_agent/agent.py:436` 直接 `tool.execute(**arguments)`；ACP 又有独立 dispatch。Bash 接受原始 shell 字符串，文件工具不限制 workspace，当前没有审批或 OS sandbox。

学习切口：集中装配 GuardedTool；权限负责授权，内核沙箱负责约束后果。

### 4. 编辑契约与实现不一致

`EditTool` 描述要求 `old_str` 唯一，`mini_agent/tools/file_tools.py:280` 却用全局 `str.replace()`；成功返回不含替换数、diff 或诊断。复现见 [P-001](PITFALLS.md)。

学习切口：唯一匹配、陈旧检查、PatchSet 预验证、原子写和范围诊断。

### 5. 输出与搜索没有预算语义

项目没有 Glob/Grep 工具；搜索依赖 bash。`BashOutputResult.format_content()`（`mini_agent/tools/bash_tool.py:32-49`）无硬上限，失败 stderr 还可绕过正常 formatter 进入历史。

学习切口：结构化搜索、ignore 语义、自描述截断与成功/失败统一钳制。

### 6. 测试不能证明循环正确

现有测试混入真实 API，部分测试以 `return True/False` 代替断言并吞异常；离线全套也包含已知 ACP 红灯。完整复现见 [P-004](PITFALLS.md)。

学习切口：路由 FakeLLM、每次请求执行历史不变量、结构化结束原因和显式线上 marker。

### 7. Provider 能力未经探测

代码使用 Anthropic-compatible wire format，不等于当前 endpoint 支持所有厂商扩展。cache、streaming、thinking signature、并行 tool call 与上下文上限都必须按 [PROVIDER_CAPABILITIES.md](PROVIDER_CAPABILITIES.md) 实测后再依赖。

## 已确认缺陷索引

| 缺陷 | 证据 | 去向 |
|---|---|---|
| `edit_file` 多处/空串替换 | `file_tools.py:273-281` | [P-001](PITFALLS.md) |
| 摘要失败可使上下文变大 | `agent.py:257-292` | [P-002](PITFALLS.md) |
| 取消删除已完成轮次 | `agent.py:73-94` 及三个调用点 | [P-003](PITFALLS.md) |
| 测试返回值不让 pytest 失败 | `tests/test_agent.py` 等 | [P-004](PITFALLS.md) |
| note 工具只写不读 | `cli.py:431` 的装配路径 | [P-005](PITFALLS.md) |
| `shlex.split` 丢失 shell 结构 | 可复现 Python 命令 | [P-006](PITFALLS.md) |

## 不从审计直接推出实现

对标项目拥有某个功能，不构成本项目实现它的理由。以下内容必须经过本仓库失败工件筛选：

- PageRank repo map：已由 ADR-0004 取消；
- provider 专有 cache/context management：先探测；
- checkpoint：必须逐字节往返且不碰用户 Git；
- 子 agent：必须证明父上下文隔离，不以“能 spawn”为完成；
- task benchmark：至少两个机制落地后再建。

审计的作用是找到学习切口，不是替代 roadmap 或提前写完整产品架构。
