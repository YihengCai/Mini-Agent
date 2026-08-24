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
- 实际是：tuple 里的 Pydantic 消息、工具实例和参数字典仍可修改。初版实现中，接收器能改写随后发送给真实模型的摘要消息，也能通过 `ModelRequest.tools` 修改 Session 中实际使用的工具。
- 根因：浅层不可变容器不提供所有权隔离；只要事件和执行路径共享任意可变嵌套对象，观察者就仍是隐式写入者。
- 复现：下列测试故意修改 agent 与摘要请求事件；实现必须让真实模型请求、Session 历史和工具定义保持原值。

  ```bash
  .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_agent_session_offline.py::test_event_observer_cannot_mutate_model_input_or_session_state \
    tests/test_agent_session_offline.py::test_summary_model_call_is_turn_maintenance_not_a_step
  ```

  修复后实测输出为 `2 passed, 1 warning in 0.65s`；警告仍是既有的未知 `cache_dir` 配置。
- 教训：agent 事件边界要验证嵌套对象的所有权，而不只看最外层类型；观察接口应发送独立快照或真正的不可变值，尤其不能把同一消息对象同时交给日志接收器和模型客户端。
- 关联：`mini_agent/core/agent.py:404-498,609-681`、`mini_agent/core/events.py:42-81`、`tests/test_agent_session_offline.py:353-380,669-724`、[ADR-0004](decisions/0004-session-turn-step-lifecycle.md)、提交 `fdcd945`。

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
