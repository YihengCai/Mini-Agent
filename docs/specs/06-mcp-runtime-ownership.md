# MCP 超时与连接的运行时所有权

> 状态：已实现。不可变超时、单连接和 runtime manager 位于 `mini_agent/tools/mcp_loader.py:21-411`；CLI 的注入与清理边界位于 `mini_agent/cli.py:351-440,491-543,622-663`；离线所有权回归位于 `tests/test_mcp_runtime_ownership.py:1-296`。

## 问题证据

baseline 的默认超时和连接表都是模块级可变状态；已经构造的连接会动态读取后来 runtime 写入的默认值，任一 cleanup 也会关闭进程中的全部连接。连接只在 `await connect()` 成功后登记，建立期间取消会留下没有 owner 的半连接（`git show 953b943:mini_agent/tools/mcp_loader.py | nl -ba | sed -n '21,57p;159,169p;284,285p;397,433p'`）。

## 本轮不变量

1. 一次 CLI runtime 持有一个 `MCPManager`；配置或模型客户端失败前不创建，工具加载与最终关闭使用同一实例，`/clear` 不替换它。
2. runtime 默认超时是不可变快照；连接和 `MCPTool` 不再读取模块级可变默认。server 明确提供的值优先，`0.0` 仍视为显式覆盖。
3. manager 在 `await connect()` 前登记连接。成功则保留到 runtime 退出，已自行清理的普通失败从表中移除，取消或意外逃逸仍能由 owner 找到。
4. `close()` 在首个 `await` 前封闭加载，用同一把锁串行化加载和多个关闭调用者；MCP/anyio transport 逐个关闭，不并行跨任务退出上下文。
5. 关闭尝试全部连接，只删除成功项；叶子连接也只有在 `AsyncExitStack.aclose()` 成功后才清空句柄。失败项留给下次关闭，首个异常对象原样抛出。
6. CLI 的顺序保持 runtime 主体、shell 关闭、MCP 关闭；失败优先级保持 runtime、shell、MCP，次级失败只诊断而不覆盖主异常。

## 不在范围

不改 stdio、SSE 或 streamable HTTP transport，不增加重连、并行连接、通用 Tool 生命周期、权限、配置来源、MCP `isError` 文本语义或真实网络能力。manager 是 CLI 宿主资源 owner，不是 agent 权限或沙箱边界。

## 离线验证

- 两个 manager 使用不同默认超时和连接表；关闭其一不影响另一个，重复关闭不重复副作用。
- `0.0` server 覆盖、不可变默认和 CLI 配置快照都有断言。
- connect 中途取消后 runtime close 仍找到连接；关闭中途取消后保留 transport 句柄，第二次 close 会真实重试。
- 两个并发 close 只关闭一次，关闭开始后拒绝加载；首个连接关闭失败不阻止后项，重试只处理失败项。
- CLI 正常、普通异常与取消都按 shell、MCP 顺序关闭同一 owner；配置和模型客户端早退不创建 runtime 资源。
