# MCP transport 的显式校验边界

> 状态：已实现。允许集合与校验位于 `mini_agent/tools/mcp_loader.py:18-36`，连接与 loader 强制点位于 `mini_agent/tools/mcp_loader.py:118-199,289-406`，离线回归位于 `tests/test_mcp.py:23-348`；取舍见 [ADR-0016](../decisions/0016-reject-explicit-invalid-mcp-transports.md)。

## 问题证据

原实现把任何未知显式 `type` 当作缺省：有 URL 就改写为 `streamable_http`，否则改写为 `stdio`。同时提供本地命令、远程 URL 和认证头时，`stdoi` 拼写错误会从本地子进程切到远程连接。

## 本轮不变量

1. 完全缺少 `type` 时保留自动推断：有非空 URL 为 `streamable_http`，否则为 `stdio`。
2. 显式值只接受四个已知名称，保持大小写不敏感；空值、未知值、`null` 和非字符串都无效。
3. 无效项在任何 transport 构造或登记前失败，诊断包含 server 名和原值。
4. 无效项只隔离当前 server；同一文件中的合法后续 server 继续加载。
5. 程序化构造连接也执行同一允许集合校验，连接分发没有未知值到 HTTP 的兜底分支。

## 不在范围

不新增完整 MCP 配置模型，不验证未知 server 字段、顶层 JSON 结构或超时数值；不改变命令/URL 的既有必填判断、配置文件来源、连接所有权、并发、重连、权限或真实网络能力。

## 离线验证

- 参数化拒绝未知字符串、空字符串、`null` 和整数；
- 缺省推断、四个合法值和大小写不敏感继续通过；
- loader 测试替身证明错误项零构造、合法后续项加载，并检查诊断中的 server 名与原值；
- 恢复宽松推断或移除连接构造校验时，各自回归转红。
