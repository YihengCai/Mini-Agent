# 踩坑日志

这里只记录实现或诊断过程中亲历的错误假设：原以为是 A，实际证据证明是 B。

每条记录必须包含：

- 当时为什么会相信 A；
- 最小复现命令和实测输出；
- 根因，而不只是报错位置；
- 一句能迁移到其他 agent 项目的教训；
- 相关实现、测试和 ADR。

对上游 baseline 的静态审计发现统一记在 [`UPSTREAM_AUDIT.md`](UPSTREAM_AUDIT.md)，未来模块的风险预研不记作踩坑。

## P-001 · 删除一个依赖不代表锁文件只会变化一个包

- 日期：2026-08-24
- 原以为：从 `pyproject.toml` 删除 ACP 后运行 `uv lock`，锁文件只会删除 `agent-client-protocol` 的包块和项目依赖项。
- 实际是：当前 `uv` 除了删除 ACP，还把锁文件从 `revision = 1` 更新为 `revision = 3`，并把既有清华索引 URL 统一改成默认 PyPI URL；一次性目录中实测产生 `909 insertions(+), 923 deletions(-)`。
- 根因：锁文件同时受项目依赖、生成它的 `uv` 版本和解析时索引配置影响；重新生成会规范化整份文件，不保证保留旧格式与来源，即使依赖图只少一个包。
- 复现：在仓库根目录运行下列命令；`fe6a682` 是删除 ACP 前、core 已拆出的提交。

  ```bash
  repro_dir="$(mktemp -d)"
  git archive fe6a682 | tar -x -C "$repro_dir"
  cp "$repro_dir/uv.lock" "$repro_dir/uv.lock.before"
  sed -i.bak '/"agent-client-protocol>=0.6.0",/d' "$repro_dir/pyproject.toml"
  (cd "$repro_dir" && uv lock)
  git diff --no-index --stat "$repro_dir/uv.lock.before" "$repro_dir/uv.lock"
  ```

  本次实测输出：

  ```text
  .../{uv.lock.before => uv.lock} | 1832 ++++++++++----------
  1 file changed, 909 insertions(+), 923 deletions(-)
  ```

- 教训：任何 agent 项目更新锁文件后都要先审查索引、格式和全文件 diff；不要把包管理器的规范化改写误当成业务依赖变更，必要时保留原格式做最小修改并用 `uv lock --check` 验证一致性。
- 关联：`pyproject.toml:11-27`、`uv.lock`、提交 `cd9ae14`、[ADR-0003](decisions/0003-remove-acp-and-extract-core-loop.md)。

## P-002 · `create_task()` 之前没有占位就不算原子接纳

- 日期：2026-08-24
- 原以为：`start_turn()` 是同步方法，只要先检查活动句柄、再调用 `create_task()`、最后保存新句柄，同一个事件循环里就不会接纳两个 Turn。
- 实际是：任务工厂是可重入的；它可以在 `create_task()` 返回前再次调用 `start_turn()`。初版实现此时还没有写入活动句柄，审查实测两个输入都被追加、两个 Turn 都被调度。任务工厂直接抛错时，初版还会留下没有 Turn 的用户消息。
- 根因：异步任务创建不是无外部代码的普通赋值；自定义 task factory 和 eager task factory 都能在返回句柄前执行代码。接纳不变量必须在进入这个可重入边界前预占，失败时再回滚输入、编号和活动槽。
- 复现：下列测试分别安装重入和拒绝任务创建的工厂；删除 `mini_agent/core/agent.py:199-201` 的预占或把它移到 `create_task()` 之后，第一项会接纳重入输入，删除 `:213-221` 的回滚则第二项失败。

  ```bash
  .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_agent_session_offline.py::test_turn_admission_reserves_before_reentrant_task_creation \
    tests/test_agent_session_offline.py::test_failed_task_creation_rolls_back_turn_admission
  ```

  修复后实测输出为 `2 passed, 1 warning in 0.71s`；警告仍是既有的未知 `cache_dir` 配置。
- 教训：任何 agent harness 的“检查后启动异步工作”都要把任务创建、回调注册和事件发射视为可重入边界；先预占所有权，再调用外部机制，并为创建失败设计完整回滚。
- 关联：`mini_agent/core/agent.py:174-230`、`tests/test_agent_session_offline.py:178-231`、[ADR-0004](decisions/0004-session-turn-step-lifecycle.md)、提交 `fdcd945`。

## P-003 · `frozen=True` 只冻结事件外壳

- 日期：2026-08-24
- 原以为：把事件定义成 frozen dataclass，并用 tuple 保存消息和工具，就足以让 `AgentEventSink` 成为只读观察者。
- 实际是：tuple 里的 Pydantic 消息、工具实例和参数字典仍可修改。初版实现中，接收器能改写随后发送给真实模型的消息，也能通过 `ModelRequest.tools` 修改 Session 中实际使用的工具。
- 根因：浅层不可变容器不提供所有权隔离；只要事件和执行路径共享任意可变嵌套对象，观察者就仍是隐式写入者。
- 复现：下列测试故意修改普通模型请求与响应事件；实现必须让真实模型请求、Session 历史和工具定义保持原值。

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_agent_session_offline.py::test_event_observer_cannot_mutate_model_input_or_session_state
  ```

  删除旧压缩后的实测输出为 `1 passed in 0.74s`。测试继续覆盖消息、响应与工具定义的深快照；摘要专属用例已由 ADR-0006 随该调用一起删除。
- 教训：agent 事件边界要验证嵌套对象的所有权，而不只看最外层类型；观察接口应发送独立快照或真正的不可变值，尤其不能把同一消息对象同时交给日志接收器和模型客户端。
- 关联：`mini_agent/core/agent.py:312-390`、`mini_agent/core/events.py:42-69`、`tests/test_agent_session_offline.py:340-367`、[ADR-0004](decisions/0004-session-turn-step-lifecycle.md)、[ADR-0006](decisions/0006-remove-legacy-local-compaction.md)、提交 `fdcd945`。

- 后续复现：2026-08-25 同一问题在工具调度边界再次出现。初版 `ToolBatchExecutor` 用 `**tool_call.function.arguments` 传参，以为外层 kwargs 副本足以隔离调用；实际嵌套字典仍与事件和 assistant 消息共享。删除执行前的深拷贝后，`.venv/bin/python -m pytest -q tests/test_tool_execution.py::test_tool_argument_mutation_cannot_change_events_or_history` 会稳定转红。

## P-004 · 事件分层正确不等于 CLI 已表达分层

- 日期：2026-08-25
- 原以为：`CliEventSink` 已分别处理 `TurnStarted`、`StepStarted`、`StepFinished` 和 `TurnFinished`，用户自然会看到与 core 一致的 Session、Turn、Step 语义。
- 实际是：初版 CLI 把 Step 画成顶层标题并写成 `completed`，正常 `TurnFinished(end_turn)` 却完全不显示；一个包含两次模型请求的 Turn 因而只显得像两个顶层执行段。第一次修正还用了绿色勾表达 `end_turn`，并在非交互入口和模型错误路径重复输出 Turn 信息，仍会暗示任务成功或混淆唯一终止事实。
- 根因：事件类型只保证机器可见的归属；省略哪一层、文案、颜色、图标和重复输出同样会改变用户理解。观察适配器必须单独验证层级和终止语义，不能把“消费了正确事件”等同于“表达了正确 contract”。
- 复现：下列测试用两次 agent 模型请求构造一个 Turn，并分别检查层级、中性结束标记、模型错误去重和帮助文案；恢复 `6147f7d^:mini_agent/cli_events.py` 的 `completed` 与静默 `end_turn` 分支时前两项会失败。

  ```bash
  .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_agent_loop_offline.py::test_cli_event_sink_preserves_rendering_and_run_logging \
    tests/test_agent_loop_offline.py::test_cli_event_sink_renders_step_and_turn_stop_facts \
    tests/test_agent_loop_offline.py::test_cli_event_sink_does_not_repeat_model_failure_details \
    tests/test_agent_loop_offline.py::test_cli_help_uses_session_turn_and_interruption_semantics
  ```

  修复后实测输出为 `4 passed, 1 warning in 0.75s`；警告仍是既有的未知 `cache_dir` 配置。
- 教训：任何事件驱动的 agent 客户端都要用“一个 Turn 包含多个 Step”的真实序列测试用户可见层级，并把“交还控制权”与“任务成功”在文字和视觉符号上同时分开。
- 关联：`mini_agent/cli.py:157-185,294-344,766-844`、`mini_agent/cli_events.py:26-193`、`tests/test_agent_loop_offline.py:386-525`、[ADR-0004](decisions/0004-session-turn-step-lifecycle.md)、提交 `6147f7d`。

## P-005 · 禁用插件也会撤销它注册的配置项

- 日期：2026-08-25
- 原以为：标准测试命令用 `-p no:cacheprovider` 只会禁止 pytest 写缓存，既有的未知 `cache_dir` 配置警告只是无害的环境噪声。
- 实际是：`cache_dir` 正是 pytest 内建 `cacheprovider` 插件注册的配置项；同一测试禁用插件时实测为 `1 passed, 1 warning in 0.43s`，保留插件时为 `1 passed in 0.43s`。
- 根因：插件专属配置的生命周期属于插件；禁用插件不仅关闭其运行行为，也撤销它向 pytest 注册的命令行和配置选项。
- 复现：在仓库根目录分别运行下列命令；第一条产生 `PytestConfigWarning: Unknown config option: cache_dir`，第二条没有 warning。

  ```bash
  .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_markdown_links.py
  .venv/bin/python -m pytest -q tests/test_markdown_links.py
  ```

- 教训：测试工具的插件配置和插件启停必须作为一个整体维护；如果决定禁用插件，就要同时删除它的专属配置，不能把配置警告长期当作无害噪声。
- 关联：`pyproject.toml:51-54`、`AGENTS.md:114-119`、`README.md:119-138`、提交 `1a64d0c`。

## P-006 · 关闭项目重试不等于 SDK 不会重试

- 日期：2026-08-25
- 原以为：adapter 的 `RetryConfig(enabled=False)` 已经绕过项目重试装饰器，所以一次 `generate()` 最多发出一次 HTTP 请求。
- 实际是：Anthropic 与 OpenAI SDK 自己都有独立重试策略；不传 `max_retries` 时，当前安装版本的客户端对象都显示为 `2`。启用项目重试时，两层还会叠加，实际尝试次数不再由一处配置决定。
- 根因：项目重试层只包住 `_make_api_request()`，不能改变 SDK 传输层的默认策略；两个层都拥有同一失败的重试权，却没有共同的计数和事件 contract。
- 复现：下列命令只构造 SDK 客户端，不访问网络。

  ```bash
  .venv/bin/python -c 'import anthropic, openai; a=anthropic.AsyncAnthropic(api_key="k", base_url="https://example.test"); o=openai.AsyncOpenAI(api_key="k", base_url="https://example.test/v1"); print("anthropic", a.max_retries); print("openai", o.max_retries)'
  ```

  2026-08-25 实测输出：

  ```text
  anthropic 2
  openai 2
  ```

- 教训：任何 agent 的外部调用只能有一个可观察的重试策略所有者；采用 SDK 时要显式关闭或纳入它的默认重试，并用客户端构造参数测试锁住这个边界。
- 关联：`mini_agent/llm/anthropic_client.py:42-46`、`mini_agent/llm/openai_client.py:44-48`、`tests/test_llm_adapters.py:220-426`、[ADR-0005](decisions/0005-explicit-model-api-adapters.md)、提交 `204c022`。

## P-007 · `uv lock --check` 不会清理不可达包块

- 日期：2026-08-25
- 原以为：从项目依赖删除 `tiktoken` 后，只要 `uv lock --check` 通过，就能证明锁文件没有残留它和独占依赖 `regex` 的包块。
- 实际是：在已经一致的锁文件末尾追加一个没有任何依赖者的 `unused-orphan` 包块后，`uv lock --check` 仍以退出码 0 报告 `Resolved 60 packages in 21ms`；删除该包块后当前项目实际解析 59 个包。
- 根因：`--check` 验证项目依赖能否由锁文件一致解析，不负责把锁文件规范化为只含从项目根可达的包；额外包块可以同时满足检查。
- 复现：下列命令在临时目录重放提交 `3cbf242` 的依赖文件，追加一个不可达包块，不修改当前工作树也不访问模型 API。

  ```bash
  repro_dir="$(mktemp -d)"
  git archive 3cbf242 pyproject.toml uv.lock README.md | tar -x -C "$repro_dir"
  printf '\n[[package]]\nname = "unused-orphan"\nversion = "1.0.0"\nsource = { registry = "https://pypi.tuna.tsinghua.edu.cn/simple" }\n' >> "$repro_dir/uv.lock"
  (cd "$repro_dir" && UV_CACHE_DIR="$repro_dir/.uv-cache" uv lock --check)
  ```

  2026-08-25 实测输出为 `Resolved 60 packages in 21ms`，退出码为 0。
- 教训：agent 项目删除依赖时，锁文件一致性检查必须再配合依赖图、针对包名的搜索和完整 diff；不能把 `lock --check` 当成不可达包垃圾回收器。
- 关联：`pyproject.toml:11-23`、`uv.lock`、[ADR-0006](decisions/0006-remove-legacy-local-compaction.md)、提交 `3cbf242`。

## P-008 · 避开真实模型测试不等于默认集合已经离线

- 日期：2026-08-25
- 原以为：默认 pytest 的外部风险只来自 `tests/test_agent.py` 和 `tests/test_integration.py` 两个真实模型模块，继续维护离线文件白名单即可避免费用与网络访问。
- 实际是：`tests/test_mcp.py` 还混有 5 项外部测试；前三项读取用户 `mcp.json` 后才判断是否跳过，可能先启动、连接或调用已配置的 server，后两项主动访问不可达地址。绕过当前收集门但只做 `--collect-only` 时，三份文件共出现 33 项，其中 9 项拥有这些外部能力。
- 根因：按文件名和“是否调用模型”分类测试，没有按真实副作用分类；跳过判断发生在外部连接之后也不能充当安全门。
- 复现：第一条命令只收集测试，并显式绕过当前两层门控以重现旧边界；第二条使用当前默认入口。两条都不执行测试体。

  ```bash
  .venv/bin/python -m pytest --collect-only -q --noconftest \
    -o 'addopts=--strict-markers' \
    tests/test_agent.py tests/test_integration.py tests/test_mcp.py
  .venv/bin/python -m pytest --collect-only -q \
    tests/test_agent.py tests/test_integration.py tests/test_mcp.py
  ```

  2026-08-25 实测第一条为 `33 tests collected in 0.69s`；第二条只留下 24 项离线 MCP 测试并报告 9 项被排除。
- 教训：agent 项目的测试安全边界要按“会不会读取用户配置、访问真实端点、启动配置驱动的服务或触网”标记，并在收集阶段统一强制；文件白名单和测试体内的 skip 都不足以证明默认离线。
- 关联：`conftest.py:11-41`、`pyproject.toml:50-58`、`tests/test_mcp.py:325-493`、`tests/test_pytest_entrypoint.py:66-112`、[ADR-0007](decisions/0007-explicit-opt-in-for-external-tests.md)、提交 `8616739`。

## P-009 · 整批预检不等于整批调用都已开始

- 日期：2026-08-25
- 原以为：工具批次完整预检通过后，在首个副作用前一次性认领全部调用标识符，最能保证取消期间不会重复副作用。
- 实际是：首个工具抛出 `CancelledError` 时，后项没有产生 `ToolStarted`、也从未执行，但初版账本已经永久占用后项标识符；下一 Turn 单独重试它会得到 `tool_protocol_error`。与此同时，已启动首项确实需要保持认领，不能简单把认领推迟到成功返回后。
- 根因：批次结构合法、调用被接纳和副作用开始是三个不同状态；`BaseException` 可以在合法批次中途逃逸。完整预检必须先于全部副作用，但调用标识符只能在该项即将发出 `ToolStarted` 并执行时认领。
- 复现：把 `mini_agent/core/tool_execution.py:98-106` 改回“预检后、循环前把整批标识符写入账本”，再运行：

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_tool_execution.py::test_cancellation_claims_started_id_but_not_unstarted_batch_ids
  ```

  初版语义会让 `not-started` 的下一 Turn 重试失败；修复后实测输出为 `1 passed in 0.44s`，并同时断言取消路径只有 `ToolStarted(1)`、没有 `ToolFinished` 或后项工具事件。
- 教训：任何 agent 工具执行器都要把“整批验证”和“逐项副作用所有权”分开；最多执行一次账本应在每项真正开始前认领，不能把尚未启动的合法后项当成已执行事实。
- 关联：`mini_agent/core/tool_execution.py:90-158`、`tests/test_tool_execution.py:566-612`、[ADR-0008](decisions/0008-session-owned-tool-batch-executor.md)、提交 `528da1f`。

## P-010 · 统一 finally 不等于资源边界和主异常都正确

- 日期：2026-08-25
- 原以为：用一个 `try/finally` 包住整个 `run_agent()`，在里面依次关闭 shell manager 和 MCP，就同时覆盖了所有退出路径和异常优先级。
- 实际是：初版 wrapper 连配置缺失和解析失败都包在 owner 内，这些尚未取得资源的早退也会清理全局 MCP 并永久替换事件循环异常处理器。同时，`_quiet_cleanup()` 抛出的 `CancelledError` 会覆盖正在重抛的 runtime 主异常。
- 根因：`finally` 只保证控制流会进入清理，不能自动定义“从哪里开始拥有资源”，也不会保留正在传播的异常；任何后续 `await` 抛出 `BaseException` 都可以成为新主因。
- 复现：下列回归分别锁定配置早退和 runtime/MCP 双失败；把 manager 创建移回配置读取之前，或删除 MCP `BaseException` 的显式收集，对应测试会转红。

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_background_shell_lifecycle.py::test_invalid_config_returns_before_runtime_resources_exist \
    tests/test_background_shell_lifecycle.py::test_runtime_error_wins_over_mcp_cleanup_cancellation
  ```

  修复后实测为 `3 passed in 0.61s`。
- 教训：agent 宿主要把资源取得边界、清理顺序和失败优先级分别写成可验证的 contract；一个外层 `finally` 不能替代这三个决定。
- 关联：`mini_agent/cli.py:512-666`、`tests/test_background_shell_lifecycle.py`、[ADR-0009](decisions/0009-runtime-owned-background-shells.md)、提交 `9a088b6`。

## P-011 · 串行幂等不等于并发关闭安全

- 日期：2026-08-25
- 原以为：`close()` 遍历当前 shell，等待全部 terminate 和 monitor，且第二次串行调用不重复终止，就已经证明 manager 可幂等关闭。
- 实际是：初版 `close()` 在首个 `await` 前没有封闭 `track()`，关闭途中可以加入不在快照里的新 shell；两个并发 `close()` 还会同时 terminate 同一进程。
- 根因：幂等的对象不只是“返回后的最终表”，还包括关闭期间的接纳门和多个调用者对同一副作用的所有权。串行重复测试没有产生这两种交错。
- 复现：下列故障注入在首个 terminate 中设置门，关闭途中同时尝试新登记和第二个 `close()`。删除 `_closed` 门或 `_close_lock` 会稳定转红。

  ```bash
  .venv/bin/python -m pytest -q \
    tests/test_background_shell_lifecycle.py::test_close_seals_registration_and_serializes_concurrent_callers
  ```

  修复后实测为 `1 passed in 0.42s`。
- 教训：任何拥有 agent 副作用的管理器都要在首个可重入边界前封闭新接纳，并用交错调度测试证明多个关闭调用者只触发一次副作用。
- 关联：`mini_agent/tools/bash_tool.py:109-238`、`tests/test_background_shell_lifecycle.py`、[ADR-0009](decisions/0009-runtime-owned-background-shells.md)、提交 `9a088b6`。

## 模板

```markdown
## P-NNN · 一句话描述错误假设

- 日期：YYYY-MM-DD
- 原以为：
- 实际是：
- 根因：
- 复现：
- 教训：
- 关联：
```
