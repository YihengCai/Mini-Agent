# ADR-0016：拒绝显式非法的 MCP transport

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/tools/mcp_loader.py`、`tests/test_mcp.py`、提交 `3fe5709`

## 背景

原 `_determine_connection_type()` 只在 `type` 命中 `stdio`、`sse`、`http`、`streamable_http` 时保留显式值，其余字符串都按有无 `url` 推断；连接层又把除 `stdio`、`sse` 外的值全部交给 streamable HTTP（`git show 3fe5709^:mini_agent/tools/mcp_loader.py | nl -ba | sed -n '163,177p;267,275p'`）。现有测试还明确要求 `type: unknown` 加 `url` 得到 `streamable_http`（`git show 3fe5709^:tests/test_mcp.py | nl -ba | sed -n '23,69p'`）。

离线 loader 探针给 `type: stdoi` 的 server 同时提供本地命令、远程 URL 与认证头，原实现实际构造了远程 `streamable_http` 连接；也就是说，一个拼写错误足以改变 transport 和凭据去向。新回归把该 server 放在合法 server 前，要求前者零连接、后者继续加载（`tests/test_mcp.py:297-348`）。

## 选项

1. **继续把未知显式值当作缺省**：兼容原测试，但用户输入无法区分“未指定”和“写错”。
2. **任一非法 server 使整份 MCP 配置失败**：工具集合具有原子性，但一个局部错误会阻止所有独立 server。
3. **只隔离非法 server**：完全缺少 `type` 时继续推断；显式非法值给出诊断并跳过当前项，后续 server 保持现有循环语义。

## 决定

采用选项 3。运行时允许集合只有 `stdio`、`sse`、`http`、`streamable_http`；字符串仍大小写不敏感。只有配置中不存在 `type` 键时，才按 `url` 推断 `streamable_http`，否则推断 `stdio`。显式未知字符串、空字符串、`null` 或非字符串都抛出包含原值与允许集合的 `ValueError`（`mini_agent/tools/mcp_loader.py:18-36,289-296`）。

`MCPManager` 在单个 server 边界捕获该错误、输出 server 名并继续；错误项不会构造或登记连接（`mini_agent/tools/mcp_loader.py:366-406`）。`MCPServerConnection` 构造入口复用相同校验，连接分发也只显式接受四类 transport（`mini_agent/tools/mcp_loader.py:118-199`）。本轮不引入完整 MCP 配置模型，不改变未知字段、顶层 JSON、必填命令/URL、超时、并发、重连或真实网络能力。

## 为什么否决其他的

**否决继续推断未知显式值**：显式字段表达的是用户选择，把拼写错误解释成另一个 transport 会在发起连接前丢掉意图，诊断也只显示推断后的结果。若 `type` 被正式定义为可忽略的提示，并且底层能安全协商协议、显示最终目标且认证策略与 transport 无关，宽松推断反而可以成立。

**否决整份文件失败**：当前 loader 已对禁用项、缺少命令/URL 和连接失败逐 server 隔离；让类型错误单独升级为全局失败会改变更大的可用性 contract。若多个 MCP server 必须以固定集合原子启用，缺一项就不能正确或安全工作，整份文件预检后一次失败反而更合适。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_mcp.py tests/test_mcp_runtime_ownership.py tests/test_mcp_tool_results.py tests/test_background_shell_lifecycle.py` 实测 `64 passed, 5 deselected in 0.52s`。
- 四类显式非法值、连接构造入口及“非法在前、合法在后”的 loader 回归均不访问网络（`tests/test_mcp.py:67-76,136-142,297-348`）。
- 临时恢复非法值自动推断时，定向集合实测 `5 failed, 8 passed in 0.48s`；临时移除连接构造校验时，对应回归实测 `1 failed in 0.45s`。
- 显式排除 `external` 的完整集合实测 `274 passed, 9 deselected in 13.12s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

最终实现用一个运行时允许集合同时服务配置解析与连接构造，没有引入第二份 transport 名单。错误隔离发生在连接对象创建之前，因此错误项不会进入 manager 的所有权表；合法后续项仍沿用原有连接和关闭路径。
