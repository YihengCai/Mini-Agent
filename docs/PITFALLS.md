# 踩坑日志

> 一条一坑。每条必须带证据（`file:line` / 可复现命令 / 实测输出）和一句可迁移的教训。
> 设计决策记在 `docs/decisions/`，这里只记「我以为是 A，实际是 B」。追加式，新的排在最上面。
> 所有 `file:line` 对齐 commit `953b943`。实测环境：macOS 26.5.2 / arm64 / `.venv` Python 3.11.2 / pytest 8.4.2，命令均在仓库根执行。

---

## P-006 · `shlex.split` 之后，`;` 还粘在上一个 token 上

- **日期**：2026-08-24 ｜ **来源**：实测（权限引擎预研，仓库里目前没有任何 `shlex` 调用）
- **现象**：一条「允许 `npm run *`」的规则，会批准 `npm run build; npm publish`。判定器看不到第二条命令。
- **根因**：`shlex.split()` 只做 POSIX **词法**分析——处理引号、转义、空白——它不做 shell **语法**分析。`;`、`&&`、`|` 对它而言不是分隔符，只是普通字符，会粘在前一个 token 的尾巴上。实测 `shlex.split("npm run build; npm publish")` → `['npm', 'run', 'build;', 'npm', 'publish']`：`argv[0]` 是 `npm`，`publish` 只是第 5 个参数，第二条命令在这个 argv 结构里**根本不存在**。想拿到独立的 `;` 得换 `shlex.shlex(raw, posix=True, punctuation_chars=True)` 并置 `whitespace_split = True`，实测 → `['npm', 'run', 'build', ';', 'npm', 'publish']`。
  同一层还有三个同源坑，也是刚测的：
  - `shlex.split("echo '$X'")` 和 `shlex.split("echo $X")` 都返回 `['echo', '$X']`——**逐字节相同**。词法之后，引用与展开的区别已经被销毁了。
  - 对 `` echo `rm -rf ~` `` 调 `shlex.split` → ``['echo', '`rm', '-rf', '~`']``：反引号活过了词法，只是粘在 token 上。要抓它必须在词法**之前**。
  - `shlex.split("X=rm; $X -rf /")` → `['X=rm;', '$X', '-rf', '/']`：赋值、分号、变量展开三件事，词法层一件都识别不出来。
  - `shlex.split('echo "foo')` 抛 `ValueError: No closing quotation`。反射性的 `except: pass` 会把**解析失败变成不受检执行**。
- **复现**：

```bash
python3 -c "import shlex; print(shlex.split('npm run build; npm publish')); print(shlex.split(\"echo '\$X'\") == shlex.split('echo \$X'))"
# 实测输出（Python 3.11.2 与 3.14.6 一致）：
# ['npm', 'run', 'build;', 'npm', 'publish']
# True
```

- **教训**：词法器不是解析器。任何「切成 token 再做前缀匹配」的授权判定，默认放行的正是**你没看见的那些命令**；判定必须建在能表达复合结构的语法层上，且解析失败必须 fail-closed。
- **关联**：ADR-0003（`docs/decisions/0003-sandbox-gated-permissions.md`）· `docs/specs/03-sandbox-permissions_CN.md` · `docs/mechanisms.md`「结构化权限」行

---

## P-005 · `RecallNoteTool` 定义并导出了，但 `cli.py` 从不注册

- **日期**：2026-08-24 ｜ **来源**：代码审计
- **现象**：agent 会调 `record_note` 往 `.agent_memory.json` 写笔记，但它永远读不到自己写过什么。所谓「持久记忆」是只写不读的。
- **根因**：`mini_agent/tools/note_tool.py:128` 定义了 `RecallNoteTool`，`:141` 的 `name` 是 `recall_notes`；`mini_agent/tools/__init__.py:6` import 它、`:16` 把它列进 `__all__`。但整个 `mini_agent/` 里**唯一**的注册点是 `mini_agent/cli.py:431`，只 append 了 `SessionNoteTool`——`note_tool.py:42` 的 `name = "record_note"`，参数只有 `content` / `category`，纯写。ACP 那条路更彻底：`mini_agent/acp/__init__.py:100` 的 `tools = list(self._base_tools)`，note 工具一个都没有。
  也没有任何旁路把笔记读回上下文：`grep -rn agent_memory mini_agent` 只命中 3 行，全是路径字面量（`cli.py:431`、`note_tool.py:31`、`note_tool.py:131`），`mini_agent/config/system_prompt.md` 里连 `record_note` / `recall_notes` 都没提。
  `RecallNoteTool` 在 `tests/` 和 `examples/` 里被用了很多次（`test_note_tool.py`、`test_integration.py`、`test_session_integration.py`、`examples/03`、`examples/04`），但每一次都是直接 `RecallNoteTool(memory_file=...)` 手工实例化，没有一次经过 CLI 的装配路径——所以组件测试是绿的，产品里这个功能不存在。
- **复现**：

```bash
grep -rn "NoteTool(" mini_agent/cli.py mini_agent/acp/__init__.py
# 实测输出（只有一行，且是写入侧）：
# mini_agent/cli.py:431:        tools.append(SessionNoteTool(memory_file=str(workspace_dir / ".agent_memory.json")))
```

- **教训**：一个能力是否存在，取决于它有没有被装配进运行时的工具字典，不是有没有被 export、被 test 覆盖。测试直接 `new` 出对象来验，会让「组件正确」和「产品有这个功能」永久脱钩。记忆类功能尤其危险——写入侧正常工作会让整件事看起来是活的。
- **关联**：`docs/specs/00-measurement-rig_CN.md`（测试要跑装配路径，不是绕过它）· 暂无 ADR：这条不是选择，是遗漏

---

## P-004 · 测试套件结构性无法失败

- **日期**：2026-08-24 ｜ **来源**：实测
- **现象**：`pytest tests/test_agent.py -q` 永远是绿的。
- **根因**：三层叠加。
  1. **零 assert + 返回值当断言**。`tests/test_agent.py` 全文 `grep -c assert` = **0**，断言面全是 `print()`。三条分支全 `return True`：内容正确 `:81`、内容不匹配 `:84`、**文件根本没被创建** `:87`（注释还写着 "Agent might have completed differently"）；异常路径 `:94` 和 `:161` `return False`——而 pytest 忽略测试函数的返回值，`return False` 照样 pass。
  2. **打真实 API + 吞异常**。`tests/test_llm.py:52-57` 是 `except Exception: traceback.print_exc(); return False` 的形状，`tests/test_llm_clients.py`、`tests/test_integration.py` 同款。这三个文件都从 `mini_agent/config/config.yaml` 取 key 打线上 MiniMax API。慢、贵、flaky——这三条保证它们永远不会被跑。
  3. **`tests/test_acp.py` 当前是红的**，而红了没人管，因为整套跑不完（见下）。
- **实测**：
  - 把网络整个掐掉（代理指向 `127.0.0.1:1`），`pytest tests/test_agent.py -q` 依然 **`2 passed`**（两次复跑 25.43s / 25.17s）。不掐网是 `2 passed in 55.24s`。一个连 LLM 都够不着的 agent，这份测试给它发绿灯。
  - `timeout 240 .venv/bin/pytest -q -v` → **exit 124**。collected **122 items**，240 秒后卡死在 `tests/test_llm_clients.py`，进度停在 **18%**——即整套的八成从来没有被执行过。跑到那里为止已经有两个红：`tests/test_acp.py .F`、`tests/test_integration.py F.`。
  - `.venv/bin/pytest tests/test_acp.py -q` → **`1 failed, 1 passed in 1.03s`**。失败点在 `mini_agent/acp/__init__.py:111`：会话缺失时用 `NewSessionRequest(cwd=None)` 自动补建，pydantic 抛 2 个 validation error——`cwd` 要求 `str` 却收到 `None`，`mcpServers` 是必填字段却没传。
- **复现**：

```bash
HTTPS_PROXY=http://127.0.0.1:1 HTTP_PROXY=http://127.0.0.1:1 ALL_PROXY=http://127.0.0.1:1 .venv/bin/pytest tests/test_agent.py -q
# 实测输出：2 passed in 25.43s（复跑 25.17s；时长是重试退避，不是工作量）
```

- **教训**：一份测试的价值上限，等于它**能失败的方式的数量**。用返回布尔值代替 `assert`、用 `except Exception: print()` 包住主体，这个数量就是零。相信一份测试的绿之前，先构造一次让它变红——掐网是最便宜的那次。
- **关联**：`docs/specs/00-measurement-rig_CN.md` · `docs/BUILD_LIST_CN.md`「阶段 0：可信测试底座」

---

## P-003 · 取消在步骤边界删掉的，是**已经完成**的那一轮

- **日期**：2026-08-24 ｜ **来源**：代码审计
- **现象**：任务跑到一半按 Esc，终端打印 `Cleaned up 2 incomplete message(s)`。但那一步的工具早已执行完毕，文件就在磁盘上；历史却声称这事没发生过。
- **根因**：`mini_agent/agent.py:73-94` 的 `_cleanup_incomplete_messages()` 从尾部找到最后一条 `role == "assistant"` 的消息，然后 `self.messages = self.messages[:last_assistant_idx]`（`:93`）——**连那条 assistant 一起截掉**。它从不读 `tool_calls` 的 id，因此从不判断到底存不存在孤立的 `tool_use`。
  三个调用点里有两个作用在已闭合的轮次上：`agent.py:477-478` 是在 `:474` 把 tool result 追加进历史**之后**才到达的，此刻那条 assistant 已经被满足；`agent.py:318-319` 在下一步的开头，上一轮完整闭合，它无缘无故删掉一整轮。只有 `agent.py:397-398`（工具执行前）删的确实是未满足的东西。
  真正需要维持的不变量是「assistant 消息里每个 `tool_use` id，在下一条 assistant 之前都有配对的 `tool_result`」——这正是 `mini_agent/llm/anthropic_client.py:147-156`（写 `tool_use`）与 `:163-176`（写 `tool_result`，配对键是 `tool_use_id`）序列化进 wire format 的那条约束。而代码实现的是「没有未完成的东西」，两者不是一回事。
- **复现**：

```bash
.venv/bin/python -c "from mini_agent.agent import Agent; from mini_agent.schema import Message; a=Agent.__new__(Agent); a.messages=[Message(role='system',content='s'),Message(role='user',content='write a.py'),Message(role='assistant',content='ok'),Message(role='tool',content='Successfully wrote a.py',tool_call_id='c1',name='write_file')]; print('before',len(a.messages)); a._cleanup_incomplete_messages(); print('after',len(a.messages),[m.role for m in a.messages])"
# 实测输出：
# before 4
#    Cleaned up 2 incomplete message(s)
# after 2 ['system', 'user']
```

  这一轮是完整的——`assistant` 没有孤立的 `tool_use`，`tool` 结果也在场——它照样被删掉两条。而 `a.py` 已经在磁盘上了。
- **教训**：中断只能**合成**，不能删除。工具的副作用已经落在进程之外（文件系统、网络、子进程），历史是唯一还改得动的东西——改它只会让历史与世界分叉，而模型看不见分叉，下一轮就把同样的活重做一遍。修复的目标是把配对补齐（给未满足的 id 各补一条 `[interrupted by user before this tool completed]`），不是回退到某个「干净点」。
- **关联**：ADR-0002（`docs/decisions/0002-event-seam-before-context.md`）· `docs/specs/02-event-seam-interrupt_CN.md` · `docs/mechanisms.md`「中断修复」行

---

## P-002 · 压缩器把未截断的工具输出塞进摘要 prompt，失败时原样返回

- **日期**：2026-08-24 ｜ **来源**：代码审计 + 实测
- **现象**：上下文超阈值 → 触发压缩 → 上下文变得更大。
- **根因**：两处，叠在一起。
  1. `mini_agent/agent.py:258-259` 构造摘要 prompt 时，局部变量叫 `result_preview`，但它就是 `msg.content` 本身，**没有任何截断**——只在拼接时尾巴上缀了个装饰性的 `"..."`。所以摘要 prompt 的体量约等于刚刚溢出的那段历史，压缩这一步本身要按满额 input 计费。
  2. `agent.py:289-292` 的 `except Exception` 把 `summary_content`——那份未截断的逐字记录——当作摘要返回。`:214-218` 再给它套上 `"[Assistant Execution Summary]\n\n"`，以 `role="user"` 塞回消息列表。净效果是消息条数变少、**字符数变多**。
  还有第三层：摘要以 `role="user"` 插入，而下一次压缩在 `agent.py:186` 按 `msg.role == "user"` 收集下标——摘要会被当成用户轮次再摘要一遍，且每次压缩的串行 LLM 调用次数随轮数增长（每个 user 下标一次 `_create_summary`，`agent.py:214`）。
- **实测**：单条 50,000 字符的 tool result → 发给摘要器的 prompt 是 **50,476** 字符（未截断）；摘要器调用失败时，回退返回 **50,051** 字符，比原文更长（每条 tool result 加 `"  ← Tool returned: "` + `"...\n"` 共 21 字符，外加 round 头部），再被 `:218` 套上 31 字符的前缀塞回列表。
- **复现**：

```bash
.venv/bin/python - <<'PY'
import asyncio
from mini_agent.agent import Agent
from mini_agent.schema import Message

class BoomLLM:                      # 模拟摘要器调用失败（429 / 超时 / 断网）
    async def generate(self, messages=None, **kw):
        print("prompt chars sent to summarizer:", sum(len(m.content) for m in messages))
        raise RuntimeError("429")

a = Agent.__new__(Agent)
a.llm = BoomLLM()
msgs = [Message(role="tool", content="X" * 50000, tool_call_id="c1", name="read_file")]
out = asyncio.run(a._create_summary(msgs, 1))
print("input 50000 chars -> fallback summary", len(out), "chars")
PY
# 实测输出：
# prompt chars sent to summarizer: 50476
# ✗ Summary generation failed for round 1: 429
# input 50000 chars -> fallback summary 50051 chars
```

- **教训**：压缩路径必须有一条硬保证——**输出严格小于输入**，且失败时的回退是有损截断而不是原文。任何走网络的压缩步骤都会失败，回退分支决定了这个机制在最坏情况下是省钱还是烧钱；而最坏情况恰好发生在上下文最大、最贵的那一刻。附带一条：变量名不是证据，`result_preview` 一个字符都没截。
- **关联**：ADR-0001（`docs/decisions/0001-layered-context-manager.md`，已被 ADR-0007 取代）· `docs/decisions/0007-split-file-state-from-context.md` · `docs/specs/01-context-manager_CN.md` · `docs/mechanisms.md`「分层上下文」行

---

## P-001 · `edit_file` 用 `content.replace()` 全局替换，工具描述却承诺唯一匹配

- **日期**：2026-08-24 ｜ **来源**：代码审计
- **现象**：让 agent 改一处，文件里所有同名字符串一起被改，工具还是回 `Successfully edited`。
- **根因**：`mini_agent/tools/file_tools.py:280` 是 `new_content = content.replace(old_str, new_str)`——`str.replace` 默认 `count=-1`，全量替换。上面 `:273-278` 的守卫只检查 `old_str not in content`（**存在性**），从没检查 `content.count(old_str) != 1`（**唯一性**）。
  而工具描述明写着相反的契约：`:230-231` 是 `"The old_str must match exactly and appear uniquely in the file, otherwise the operation will fail."`，参数描述 `:246` 又补了一句 `"must be unique in file"`。模型是按描述行动的——它相信非唯一会失败，所以敢用短锚点。
  同一个守卫还有第二个洞：`old_str not in content` 对**空串恒为真**。实测 `old_str=""` 对内容 `'ab\n'` 执行，返回 `success=True`，文件变成 `'-a-b-\n-'`——`str.replace` 在每个字符边界插入一次。
  成功回执 `:283` 是 `f"Successfully edited {file_path}"`：不报替换了几处，不报文件还能不能解析。调用方拿不到任何能证伪「只改了一处」的数字。
- **复现**：

```bash
cd "$(mktemp -d)" && printf 'x = 1\ny = 1\nz = 1\n' > t.py && PYTHONPATH=/Users/flame/Work/Mini-Agent /Users/flame/Work/Mini-Agent/.venv/bin/python -c "import asyncio; from mini_agent.tools.file_tools import EditTool; r=asyncio.run(EditTool(workspace_dir='.').execute(path='t.py', old_str='1', new_str='2')); print(r.success, r.content); print(open('t.py').read())"
# 实测输出：
# True Successfully edited /.../t.py
# x = 2
# y = 2
# z = 2
```

- **教训**：工具描述是模型据以判断风险的契约；描述与实现的偏差不是文档 bug，是**正确性 bug**——它诱导模型做出错误的安全性假设。而且这类失败是静默的：成功回执里没有一个数字能让调用方发现替换了 3 处而不是 1 处。工具的成功返回值至少要携带一个可被证伪的量（替换处数、diff 行区间、写后校验结果）。
- **关联**：`docs/BUILD_LIST_CN.md`「阶段 3：让 agent 真正会改代码」· `docs/specs/04-transactional-edit_CN.md` · `docs/mechanisms.md`「事务性编辑」行
