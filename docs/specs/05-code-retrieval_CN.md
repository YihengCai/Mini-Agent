# Glob/Grep + repo map

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Repo-aware code search: Glob/Grep tools + a tree-sitter repo map (PageRank-ranked symbol index)`


## 一句话

给 agent 两个它现在完全没有的廉价结构化检索原语——一对识别 gitignore、带硬性结果上限的 Glob/Grep（外加 bash 输出截断，这样一条跑偏的 `grep -r` 吃不掉整个窗口）——在此之上再加一份 tree-sitter repo map：逐文件抽取符号定义与引用，按引用图上的 personalized PageRank 给文件排序，把排名靠前的符号渲染进一个固定 token 预算，每个会话注入一次，编辑时刷新。

## 为什么这是难点

coding agent 在陌生仓库里的第一轮就是闭眼猜。模型手里有一个任务（"修一下 retry backoff"），面对一个它看不见的文件系统。后面的一切——读哪个文件、编辑落不落在正确的层、要不要烧掉 30 步——都由第一次检索决策定死。这是所有其他 agent 能力的*入口*：上下文管理只有在正确的东西一开始就进了上下文时才有意义。

难点在于两个显而易见的方案都失败。把结构全倒出来负担不起：这个仓库里朴素的 `Path('.').rglob('*')` 返回 6301 个文件，而 ripgrep 是 364 个（2026-08-23 取样；08-24 复测 7072 / 391 —— 绝对值随工作树变动，**要点是约 18 倍的比值**，引用时请重新取样）；单条 `rg -n "def " .` 产出 76550 个字符（约 20k tokens），而 Mini-Agent 今天原封不动交给模型（bash_tool.py:412-429 解码后直接返回，没有任何上限）。让模型自己瞎摸又慢又有损：没有先验它会 grep 错词，拿到 400 条命中，然后截断悄无声息地骗它。

有意思的中间地带是一个*排序*问题，不是搜索问题：364 个文件里，哪 25 个应该在模型开口提问之前就知道其存在？这是一个图问题——重要性从一个文件流向它所引用符号的定义文件——而要答好，就得处理符号名冲突、dangling 节点、任务条件化、token 预算，以及并发编辑下的缓存失效。这五件合起来，正好就是"读过 aider 那篇博客"和"真做过一遍"之间的差距。

## 仓库现状

**完全没有搜索能力。** `mini_agent/tools/__init__.py:3-6` 导出的就是 `ReadTool, WriteTool, EditTool, BashTool, SessionNoteTool, RecallNoteTool`。`cli.py:418-427` 只注册 Read/Write/Edit。没有 Glob，没有 Grep，没有目录列举。系统提示词（`mini_agent/config/system_prompt.md:41-51`）一边告诉模型 "Verify file existence before reading"、"Don't guess - use tools to discover missing information"，一边没给它任何能发现东西的工具，除了裸 `bash`。

**于是所有发现动作都落到 bash，而 bash 无上限。** `bash_tool.py:391-396` 用 `create_subprocess_shell` 跑命令，`bash_tool.py:411-413` 完整解码 stdout/stderr，`bash_tool.py:423-429` 返回它们。`BashOutputResult.format_content`（`bash_tool.py:31-47`）把 stdout + stderr + exit code 拼进 `content`，不做任何长度检查。接着 `agent.py:470` 把 `result.content` 直接塞进一条 `tool` 消息。仓库根目录一次 `grep -rn` 约 20k tokens，而 `token_limit` 是 80000（`agent.py:28`）——一条命令吃掉四分之一预算，两条就触发 `agent.py:153-233` 那个有损的散文式压缩器。

**失败路径比成功路径更糟。** `bash_tool.py:418-421` 用*整份* stderr 构造 `error_msg = f"Command failed with exit code {rc}" + "\n" + stderr_text.strip()`，而 `agent.py:470` 对任何失败的工具都发送 `f"Error: {result.error}"`。于是 `find / -name '*.py'`（exit 1，权限错误在 stderr 上堆出几 MB）连 `content` 那层格式化都彻底绕过了。

**唯一存在的那处截断位置不对，本身还是个隐患。** `file_tools.py:11-60` 的 `truncate_text_by_tokens` 只在 `ReadTool.execute`（`file_tools.py:147-148`，32k token 上限）里用。它的第一个动作是对*整个*字符串做 `encoding.encode(text)`（`file_tools.py:32-33`），然后才判断要不要截断——对可能几十 MB 的输入做 O(n) 的 tiktoken 计算，而且没有一个廉价的字符数预检。

**没有任何形式的结构化索引。** `mini_agent/` 里没有任何东西解析源码。系统提示词唯一的方位信息是 `agent.py:41-44`，追加一句话写明 workspace 路径。`tiktoken` 已经是依赖、也已经用于预算计算（`agent.py:96-131`），所以量具是有的；被量的东西没有。

**两条约束设计的缝。**（1）`add_workspace_tools`（`cli.py:399-432`）被 CLI（`cli.py:543`）和 ACP server（`acp/__init__.py:101`）共用，所以在那里挂一个钩子就同时覆盖两条循环。（2）压缩器在 `agent.py:186` 的 `user_indices` 扫描把*每一条* `role == "user"` 的消息当成轮次边界，而注入的 repo-map 消息会破坏它。

## 最小实现

## New files

```
mini_agent/tools/search_tools.py      ~330 LOC   GlobTool, GrepTool, ripgrep + pure-Python backends
mini_agent/tools/_ignore.py            ~90 LOC   gitignore parsing + walk pruning (shared by both backends)
mini_agent/context/__init__.py          ~5 LOC
mini_agent/context/symbols.py         ~200 LOC   Tag, TreeSitterExtractor, RegexExtractor
mini_agent/context/repo_map.py        ~380 LOC   scan, cache, graph, pagerank, rank, render, budget fit
scripts/bench_repo_map.py             ~120 LOC   the demonstration
tests/test_search_tools.py            ~ 90 LOC
tests/test_repo_map.py                ~ 70 LOC
```

新增依赖（就一个，有理由）：`tree-sitter-language-pack>=0.7` —— 约 160 种语法的预编译 wheel，不需要编译器。import 用 try/except 包住；import 失败时 `RegexExtractor` 覆盖 py/js/ts/go，所以仓库永远不会硬依赖它。

---

## Layer 1 — Glob、Grep 与 bash 截断

### `mini_agent/tools/_ignore.py`

```python
DENY_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".tox", ".next", "target", ".idea"}

class IgnoreRules:
    """最小 .gitignore 匹配器：字面量、*、**、dir/、前导 /、取反。"""
    def __init__(self, root: Path): ...
    @classmethod
    def load(cls, root: Path) -> "IgnoreRules":   # root/.gitignore + root/.git/info/exclude
    def ignored(self, rel: str, is_dir: bool) -> bool: ...

def walk_files(root: Path, rules: IgnoreRules, include_hidden: bool = False,
               max_files: int = 20_000) -> Iterator[Path]:
    """os.walk 配 followlinks=False，就地裁剪 dirnames，记录已访问的
    (st_dev, st_ino) 以打破 symlink 环，到 max_files 停止。"""
```

### `mini_agent/tools/search_tools.py`

```python
RG = shutil.which("rg")

class GlobTool(Tool):
    name = "glob"
    # params: pattern (str, required), path (str, default workspace), limit (int, default 200)
    async def execute(self, pattern: str, path: str | None = None,
                      limit: int = 200) -> ToolResult
```
后端 A（`rg --files --hidden --glob '!.git' -g <pattern>`），后端 B（`walk_files` + 对相对路径做 `fnmatch`，`**` 展开）。结果在截断前**按 mtime 倒序排序**——这是整个工具里最重要的一行：在 5000 个匹配上加 200 文件上限时，正是按新旧排序让这个上限无害。输出是换行拼接的相对路径，截断时再加一行字面量 `[showing 200 of 4193 matches, newest first]` 页脚。

```python
class GrepTool(Tool):
    name = "grep"
    # params: pattern (regex, required), path, glob (file filter),
    #         output_mode: "files_with_matches" (default) | "content" | "count",
    #         case_insensitive: bool, context: int (0..3), head_limit: int (default 50)
    async def execute(self, pattern: str, path=None, glob=None,
                      output_mode="files_with_matches", case_insensitive=False,
                      context=0, head_limit=50) -> ToolResult
```
后端 A 外壳调用 `rg --no-heading --line-number --color never --hidden --glob '!.git'`（外加 `-l` / `-c` / `-C n` / `-i` / `-g <glob>`），按字节预算读 stdout，这样病态 pattern 撑不爆内存。后端 B 用 `re` 编译 pattern，用 `walk_files` 遍历，以二进制打开文件，跳过前 8192 字节里含 `b"\x00"` 或超过 2 MB 的文件，用 `errors="replace"` 解码，并在收集到 `head_limit` 条结果后跳出整个遍历。

渲染（token 经济性就是重点）：
- `files_with_matches`：裸相对路径，一行一个。
- `count`：`path:N`，倒序排列。
- `content`：按文件分组，`path:LINE: text`，每行硬性截到 200 字符并加 `…`。
- 截断时始终追加 `[truncated: showing N of M matches — narrow the pattern or pass a glob]`。永远不静默截断。

两个工具都校验解析后的路径没有跑出 `workspace_dir`（与 `file_tools.py:112-114` 相同的 `Path.is_absolute()` 写法），并在正则非法时返回 `ToolResult(success=False, ...)` 而不是抛异常。

### Bash 截断 —— 两处改动，都在 `bash_tool.py`

在文件顶部附近加：

```python
MAX_OUTPUT_LINES = 200      # 100 head + 100 tail
MAX_LINE_CHARS  = 2000
MAX_OUTPUT_CHARS = 60_000   # absolute backstop

def clamp_output(text: str, label: str = "output") -> str:
    """按行的中间挖空式截断。字符数守卫在最前，不用 tokenizer。"""
```
算法：`len(text) <= 8000` 时原样返回（快路径，不做 split）。否则 `splitlines()`，把每行截到 `MAX_LINE_CHARS`，若 `len(lines) > MAX_OUTPUT_LINES` 则保留前 100 行和后 100 行，用 `\n... [{label} truncated: {n} lines omitted, {len(text)} chars total] ...\n` 拼接。最后，如果某一行病态地长，再硬切到 `MAX_OUTPUT_CHARS`。

1. **`bash_tool.py:31-47`** —— 在 `BashOutputResult.format_content` 内部，把 `output += self.stdout` 换成 `output += clamp_output(self.stdout, "stdout")`，stderr 分支换成 `clamp_output(self.stderr, "stderr")`。这一处同时覆盖前台（`bash_tool.py:423-429`）和 `bash_output` 的结果。
2. **`bash_tool.py:418-421`** —— `error_msg += f"\n{stderr_text.strip()}"` 改成 `error_msg += "\n" + clamp_output(stderr_text.strip(), "stderr")`。没有这一条，每条*失败*命令都会经 `agent.py:470` 绕过上限。

另外把 `truncate_text_by_tokens` 从 `file_tools.py:11-60` 原样搬走，只在 docstring 后加一句守卫：`if len(text) <= max_tokens * 2: return text`（对 cl100k 来说 2 字符/token 是安全下界），这样第 32 行的 `encoding.encode` 永远不会跑在巨型输入上。

### 注册

- `tools/__init__.py:5` → 加 `from .search_tools import GlobTool, GrepTool`；扩展第 8-17 行的 `__all__`。
- `cli.py:420-426` → `tools.extend([...])` 列表中加入 `GlobTool(workspace_dir=str(workspace_dir)), GrepTool(workspace_dir=str(workspace_dir))`。这一处改动经 `acp/__init__.py:101` 也覆盖了 ACP。
- `config/system_prompt.md:41-45`（"File Operations" 那个 bullet 列表）→ 加上："Use `glob` to find files by name/pattern and `grep` to find files by content **before** reading. Prefer them over `bash` with find/grep — bash output is truncated. `grep` defaults to returning filenames only; ask for `output_mode=\"content\"` only when you need the matching lines."

---

## Layer 2 —— repo map

### `mini_agent/context/symbols.py`

```python
@dataclass(frozen=True, slots=True)
class Tag:
    rel_path: str
    line: int          # 1-indexed
    name: str
    kind: str          # "def" | "ref"
    node_type: str     # "class" | "function" | "method" | "const" | ""

LANG_BY_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript",
               ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust",
               ".java": "java", ".rb": "ruby", ".c": "c", ".h": "c", ".cpp": "cpp"}

class TreeSitterExtractor:
    def __init__(self) -> None:                       # raises ImportError if pack missing
        from tree_sitter_language_pack import get_parser, get_language
    def extract(self, rel_path: str, source: bytes) -> list[Tag]:
```
每种语言对语法树跑两个 query：
- **defs** —— 例如 python 的 `(function_definition name:(identifier) @def.function) (class_definition name:(identifier) @def.class)`；只保留祖先链中最多含一个 `class_definition` 的节点（顶层 def 和方法，不含嵌套闭包）。
- **refs** —— 每一个*不是*定义节点 name 子节点的 `(identifier)` / `(attribute attribute:(identifier))` 节点。对照一个 60 词的停用表（`self, cls, i, x, str, int, list, dict, len, print, name, type, id, value, data, result, args, kwargs, ...`）过滤，并丢弃短于 3 个字符的名字。

```python
class RegexExtractor:
    """Fallback: line-oriented regexes for python/js/ts/go defs + \b[A-Za-z_][\w]{2,}\b refs."""
```
`get_extractor()` 能 import 就返回 TreeSitter，否则返回 Regex，在模块级缓存。两者必须产出形状完全一致的 `Tag`；测试断言在 `mini_agent/agent.py` 上，正则抽取器找到的 def 是 tree-sitter 结果的严格子集。

### `mini_agent/context/repo_map.py`

```python
REPO_MAP_SENTINEL = "<repo-map>"

@dataclass
class RepoMapConfig:
    max_tokens: int = 1024
    max_files_scanned: int = 2000
    max_file_bytes: int = 512_000
    damping: float = 0.85
    iterations: int = 30
    fit_tolerance: float = 0.10        # accept a rendering within 10% of budget
    max_definers_per_symbol: int = 5   # symbols defined in >5 files are dropped
    hysteresis: float = 0.15           # min rank churn before re-rendering

@dataclass
class FileEntry:
    rel: str
    mtime_ns: int
    size: int
    tags: list[Tag]

class RepoMap:
    def __init__(self, root: Path, cfg: RepoMapConfig = RepoMapConfig(),
                 cache_path: Path | None = None):   # default root/".agent_cache/repo_map.json"
        self._entries: dict[str, FileEntry] = {}
        self._dirty: set[str] = set()
        self._last_render: str = ""
        self._last_hash: str = ""

    # public
    def note_file_changed(self, path: str | Path) -> None      # called from Write/Edit hooks
    def get_map(self, focus_files: Sequence[str] = (),
                mentioned_symbols: Sequence[str] = ()) -> str
    def content_hash(self) -> str                               # sha1 of _last_render
```

**第 1 步 —— 扫描（`_scan`）。** 用 `walk_files(root, IgnoreRules.load(root))` 枚举候选，按 `LANG_BY_EXT` 过滤。逐个 `os.stat`。当 `(mtime_ns, size)` 匹配*且*路径不在 `self._dirty` 中时复用缓存的 `FileEntry`；否则重新解析。存在 `.git` 时按 "git 中最近被改过" 给候选排序——`git log --name-only --pretty=format: -n 400` 约 100 ms 就能给出一张频次表——否则按 mtime 排，并在 `max_files_scanned` 处截断。在 `get_map` 退出时把缓存以 JSON 持久化。

**第 2 步 —— 建图（`_build_graph`）。** 从所有 `kind == "def"` 的 tag 构造 `definers: dict[str, set[str]]`，然后丢掉任何 `len(definers[sym]) > max_definers_per_symbol` 的符号。对每个文件 A 和 A 中出现 `n` 次的被引用符号 `s`：

```python
mul = 10.0 if s in mentioned_symbols else (0.1 if s.startswith("_") else 1.0)
w   = math.sqrt(n) * mul / len(definers[s])
for B in definers[s]:
    if B != A:
        adj[A][B] += w
        edge_labels[(A, B)][s] += w
```

**第 3 步 —— personalized PageRank（`_pagerank`，约 35 LOC，不用 networkx）。**

```python
nodes = list(all_files)
p = {n: 0.0 for n in nodes}
if focus_files:
    for f in focus_files: p[f] = 1.0 / len(focus_files)
else:
    for n in nodes: p[n] = 1.0 / len(nodes)
r = dict(p)
for _ in range(cfg.iterations):
    dangling = sum(r[n] for n in nodes if not adj.get(n))
    new = {n: (1 - d) * p[n] + d * dangling * p[n] for n in nodes}
    for src, outs in adj.items():
        tot = sum(outs.values())
        if tot <= 0: continue
        share = d * r[src] / tot
        for dst, w in outs.items():
            new[dst] += share * w
    if sum(abs(new[n] - r[n]) for n in nodes) < 1e-6:
        r = new; break
    r = new
```

**第 4 步 —— 给定义排序（`_rank_tags`）。** 把每个文件的 rank 沿其出边推出去，并归到*创造这条边的那个符号*头上：

```python
for (A, B), syms in edge_labels.items():
    tot = sum(adj[A].values())
    for s, w in syms.items():
        def_score[(B, s)] += r[A] * w / tot
```
有 rank 但没有任何入向符号信用的文件，仍会以 `r[file] * 0.01` 拿到一行光秃秃的路径，这样一个高 rank 的叶子文件不会彻底消失。

**第 5 步 —— 渲染 + 预算拟合（`_render`、`_fit_budget`）。** 渲染按文件分组，符号按行号顺序：

```
mini_agent/tools/bash_tool.py
    53: class BackgroundShell
   130: class BackgroundShellManager
   217: class BashTool
mini_agent/agent.py
    18: class Agent
   153: async def _summarize_messages
```
`_fit_budget` 二分的是纳入的*已排序 (file, symbol) 对的数量*，不是渲染后的字符数：`lo, hi = 0, len(ranked)`，渲染候选，用 `tiktoken.get_encoding("cl100k_base")` 计数，接受第一个落在 `[max_tokens * (1 - tolerance), max_tokens]` 内的结果，约 12 次迭代。搜索过程中按 `mid` 缓存 token 计数。前置一行表头：`` `<repo-map>` Repository structure (top {n} files by reference-graph importance; not exhaustive — use glob/grep to find anything not listed).``

### 接线 —— 四个钩子

1. **`file_tools.py:158-165` 和 `212-221`** —— `WriteTool.__init__` / `EditTool.__init__` 增加 `on_change: Callable[[Path], None] | None = None`；在 `file_tools.py:206` 和 `file_tools.py:281` 写入成功后调用它，外面套 `try/except Exception: pass`，这样缓存的 bug 永远不会搞坏一次编辑。
2. **`cli.py:399-432`（`add_workspace_tools`）** —— 在构建工具列表之前建好 map 并返回它：
   ```python
   repo_map = RepoMap(workspace_dir) if config.tools.enable_repo_map else None
   hook = repo_map.note_file_changed if repo_map else None
   ...
   WriteTool(workspace_dir=str(workspace_dir), on_change=hook),
   EditTool(workspace_dir=str(workspace_dir), on_change=hook),
   ```
   把签名改成 `-> RepoMap | None`，并更新两个调用点：`cli.py:543` 和 `acp/__init__.py:101`。
3. **`cli.py:568-575`** —— 把 `repo_map=repo_map` 传进 `Agent(...)` 构造函数（`acp/__init__.py:102` 同样处理）。`config.py:48-63 ToolsConfig` 增加 `enable_repo_map: bool = True` 和 `repo_map_tokens: int = 1024`。
4. **`agent.py`** —— 三处外科式改动：
   - `agent.py:21-29` 构造函数增加 `repo_map: "RepoMap | None" = None`；存起来，并置 `self._map_hash = ""`。
   - `agent.py:59-61 add_user_message` 变为：
     ```python
     def add_user_message(self, content: str):
         self._refresh_repo_map()
         self.messages.append(Message(role="user", content=content))
     ```
     其中 `_refresh_repo_map`（新增，约 25 LOC）计算 `focus_files` = 历史中 `read_file`/`edit_file` 工具调用里最近出现的 5 个不同路径，调用 `get_map`，然后：首次调用时在索引 1 处插入 map 消息 `Message(role="user", content=REPO_MAP_SENTINEL + ...)`；之后的调用中若 hash 变了，就把*旧*的 map 消息内容改写成一行 `REPO_MAP_SENTINEL + " (superseded)"`，并在到来的用户轮次之前追加新的那条。
   - `agent.py:186` —— 把 map 消息排除在轮次边界之外：
     ```python
     user_indices = [i for i, m in enumerate(self.messages)
                     if m.role == "user" and i > 0
                     and not (isinstance(m.content, str) and m.content.startswith(REPO_MAP_SENTINEL))]
     ```
     并在 `agent.py:209-221` 的重建循环里，把 `execution_messages` 内发现的任何 map 消息原样带进 `new_messages`，而不是任由 `_create_summary`（`agent.py:250-259`，只格式化 `assistant`/`tool` 角色）把它悄悄丢掉。

第一次 `get_map()` 在第一条用户消息上同步执行；若 `_scan` 报告候选数超过 `max_files_scanned`，`get_map` 立即返回 `""` 并用 `asyncio.create_task` 排一个后台构建，这样第 1 轮永远不会被 monorepo 卡住。

## 边界情况

1. **两个后端在隐藏文件上的一致性。** 直觉做法：有 ripgrep 就用 `rg --files`，没有就用 `Path.rglob('*')`，假定它们一致。实际情况，在这个仓库实测：`rg --files` 返回 364，`rg --files --hidden` 返回 435，`Path('.').rglob('*')` 返回 6301——ripgrep 静默跳过 dotfile *以及* .gitignore 的路径，Python 兜底路径什么都不跳。一个被问"CI 配置在哪"的 agent，在 rg 后端上得到零命中（`.github/` 是隐藏的），在兜底路径上被 `.venv` 淹没。正确做法：在 `_ignore.py` 里钉死一套策略并强制两个后端都遵守——给 ripgrep 传 `--hidden --glob '!.git'`，在 Python 遍历里应用 `IgnoreRules` + `DENY_DIRS`——然后写一个在 `tests/fixtures/` 上跑两个后端并 diff 集合的测试来断言一致性。

2. **在多个文件里定义的符号会把图抹平。** 直觉做法：对符号 `s` 的每次引用，都向每个定义 `s` 的文件加一条边。在这个仓库里 `def name`、`def description`、`def parameters` 各自在 11 个文件里定义（每个 `Tool` 子类），`async def execute` 又是 11 个——单个 `tool.execute(...)` 调用点就发出 11 条等权重的边，于是 rank 汇聚到字母序最靠前的那个工具文件，真正有意思的那个文件只拿到 1/11 的信用。正确做法：边权除以 `len(definers[s])`，把 definer 数超过 `max_definers_per_symbol` 的符号整个丢掉，压制私有名（`_x` → 0.1 倍），并按 `sqrt(n_refs)` 而不是 `n_refs` 加权，这样一个调了 `read_file` 40 次的文件不会压过十个各调两次的文件。

3. **dangling 节点，以及"base.py 是最重要的文件"这个结果。** 直觉做法：把 PageRank 实现成 `r[dst] += d * r[src] / out_degree[src]`，迭代，收工。随之而来两个 bug。(a) 没有出边的节点（`__init__.py`、`schema.py`、纯叶子模块）吸收 rank 且永不返还；总质量每轮迭代都在缩，排序变成迭代次数的函数。(b) 用均匀 teleport 向量时，赢家永远是被 import 最多的文件——这里是 `tools/base.py` 和 `schema/schema.py`——而这恰恰是 agent 最不需要被告知的文件。正确做法：每轮迭代通过 personalization 向量重新分配 dangling 质量（`new[n] = (1-d)*p[n] + d*dangling*p[n] + ...`），并把 `p` 集中到 agent 本会话实际碰过的文件上，让 rank 流向任务，而不是流向叶子。

4. **静默截断是正确性 bug，不是外观问题。** 直觉做法：grep 上限 50 条匹配然后返回。模型随即推理"一共正好 50 个调用点，我全看过了"，然后把 300 个里的 50 个重构了。Glob 的 200 文件上限和 bash 的输出截断是同一种失败。正确做法：每一处截断都发出一段机器可读的页脚写明真实总数（`[truncated: showing 50 of 312 matches — narrow the pattern or pass a glob]`），repo map 的表头写 "not exhaustive — use glob/grep"，而且 Glob 在截断*之前*按 mtime 排序，让活下来的那个子集是有用的那个。规则是：被截断的结果必须自我描述，因为模型看不见没被展示给它的东西。

5. **通过 `bash` 做的编辑绕过失效钩子。** 直觉做法：把缓存失效挂在 `WriteTool`/`EditTool`（`file_tools.py:206`、`file_tools.py:281`）上然后信任它。但 `bash_tool.py:391` 跑的是任意 shell —— `sed -i`、`git checkout`、`black .`、一个代码生成脚本——这些都不碰钩子，于是 map 会自信地描述已经不存在的符号。正确做法：把钩子当成*优化*，把 `_scan` 里的 mtime+size stat 扫描当成*不变量*；每次 `get_map` 都重新 stat 所有候选（约 2000 次 stat ≈ 20 ms），只重解析变了的。二阶问题：`(mtime_ns, size)` 对于保留了 mtime 的等大小内容变更（`cp -p`、某些构建工具）仍会说谎，所以 `self._dirty` 里的任何路径不论 stat 结果如何都无条件重解析。每轮给每个文件算 hash 是错误的修法——那会把 20 ms 的扫描变成好几秒。

6. **如果原地刷新，刷新 map 的代价大于它带来的收益。** 直觉做法：保留一条 map 消息，文件一变就改写它的内容。那条消息位于索引 1，改写它就会让*其后所有*消息的 prompt 前缀失效——为了更新 1024 个 token，你在每次编辑时都要付一次完整的对话重读。正确做法：只在用户轮次边界刷新（`agent.py:59-61`，绝不在循环中途），只在渲染内容的 hash 真的变了时刷新，且以追加新 map 消息、把旧的打成桩的方式做——桩保证了变更点之前的前缀稳定。再加上 rank 迟滞（`hysteresis: 0.15`），这样一次只动一个符号、rank 只重排了 2% 的编辑根本不触发重渲染。

7. **一条 `role='user'` 的 map 消息会破坏现有压缩器。** `agent.py:186` 把每条 `role == 'user'` 的消息收进 `user_indices`，`agent.py:198-221` 按 `[system] + [user_k + summary_k ...]` 重建历史。于是注入的 map 消息 (a) 造出一个假的轮次边界，把一个真实轮次劈成两段被摘要的片段；(b) 如果它落在某个执行切片*内部*，会被交给 `_create_summary`，而后者在 `agent.py:250-259` 只格式化 `assistant` 和 `tool` 角色——于是 map 在第一次压缩时被静默删除，agent 恰好在上下文最长的时候瞎掉。正确做法：给内容加 sentinel 前缀，把它排除在 `user_indices` 之外，并在重建时原样带过去。

## 怎么证明它有效

两个离线 demo，不需要 API key，计算量都在一分钟以内。`scripts/bench_repo_map.py`（约 120 LOC）。

**Demo 1 —— 排序质量（真正的主张）。** 在这个仓库上手工标注 10 组（问题 → 标准答案文件）配对，以 dict 写进脚本：

```python
GOLD = {
 "where are background shell processes tracked":      "mini_agent/tools/bash_tool.py",
 "how is the system prompt loaded and templated":     "mini_agent/cli.py",
 "where does context compaction happen":              "mini_agent/agent.py",
 "exponential backoff on API failures":               "mini_agent/retry.py",
 "connecting to MCP servers":                         "mini_agent/tools/mcp_loader.py",
 "the ACP protocol bridge":                           "mini_agent/acp/__init__.py",
 "progressive disclosure of skills":                  "mini_agent/tools/skill_loader.py",
 "converting a tool to an OpenAI function schema":    "mini_agent/tools/base.py",
 "where token usage is counted":                      "mini_agent/agent.py",
 "where relative paths are resolved to the workspace":"mini_agent/tools/file_tools.py",
}
```

用 **recall@10** 和 **MRR** 给四个排序器打分：(a) 每个 query 用一个合理的起始文件作种子的 personalized PageRank，(b) 普通 PageRank（均匀 teleport），(c) mtime 倒序，(d) 文件大小。跑两遍——一遍在 Mini-Agent（364 个文件）上，一遍在 `.venv/lib/python3.11/site-packages/openai`（约 250 个文件，磁盘上已有，离线可用）上配另一套 gold 集。

```bash
uv run python scripts/bench_repo_map.py --root . --repeat 3
uv run python scripts/bench_repo_map.py --root .venv/lib/python3.11/site-packages/openai
```

输出是一张表：排序器、recall@10、MRR、构建耗时（冷缓存 / 热缓存）、渲染 token 数。当 personalized PR 在 recall@10 上大幅胜过 mtime 和 size，*并且*在 MRR 上胜过普通 PageRank 时，这个主张才算被证明——后一项才是有意思的对比，因为它把任务条件化的价值从图本身的价值里分离了出来。另外把普通 PR 下的 top-5 文件打印出来，具体展示 "everything imports `base.py`" 那个退化结果。

**Demo 2 —— token 账单。** 对三条命令，打印 `BashOutputResult.content` 在 `clamp_output` 前后的字符数，并附上 tiktoken 计数：

```bash
uv run python scripts/bench_repo_map.py --bash-demo
#   rg -n "def " .        76550 chars / ~20100 tok  ->  ~7900 chars / ~2100 tok
#   find . -name "*.py"     ...
#   ls -R .venv             ...   (exit!=0 path, proving bash_tool.py:418-421 is covered)
```
与之并排，用 `GrepTool(output_mode="files_with_matches")` 回答同样这三个问题——通常在 300 token 以下——以说明这个工具不只是一个上限，而是一种更便宜的形状。

两个 demo 各打印一行可以直接粘进 README 的结论，另加 `pytest tests/test_repo_map.py tests/test_search_tools.py` 验证不变量（后端一致性、PageRank 质量守恒到 1e-9、预算拟合落在容差内、map 能扛过一轮 `_summarize_messages`）。

## 深度追问

1. **为什么用 personalized PageRank，而不是数入向引用？** 入度是度中心性：它回答"有多少文件 import 这个？"，而在任何代码库里这个问题的头名都是工具模块——这里是 `tools/base.py` 和 `schema/schema.py`，恰恰是 agent 几乎从不需要被告知的东西。PageRank 是特征向量中心性：重要性*从*重要文件流出，所以被入口点引用一次的文件，排名高于被测试引用十次的文件。personalization 向量是任务进入的地方：把 teleport 质量集中在 agent 已经读过的文件上，rank 就会流进那个文件的邻域，于是同一个仓库对"修 retry backoff"和对"加一个工具"给出不同的 map。被否掉的方案：从入口点做原始 import-graph BFS（遇到动态 import 和多入口点的仓库就崩，而且同一跳内没有顺序）；对标识符做 TF-IDF（没有方向概念——它分不清"定义"和"使用"）；纯入度（上面那个 base.py 的失败）。damping 取 0.85 不是随手定的：它对应期望长度 1/(1-d) ≈ 6.7 跳的随机游走，大致就是值得关心的调用链深度；damping 更低就退化成 personalization 向量本身。

2. **map 注入在哪里，为什么这是最难的决定？** 三个选项，各有各的坏。放系统提示词里：前缀缓存收益最大，但它在第一条用户消息之前就构造好了，所以无法任务条件化，而且任何刷新都会让整个已缓存前缀失效。放每一轮的用户消息里：永远新鲜，但每轮付 1024 token，而且过期的重复副本在历史里越堆越多。放在索引 1 处、只在用户轮次边界刷新：这是选定的设计——工具循环期间前缀逐字节不变（90% 的请求都发生在那里），而一次刷新的缓存失效代价是每个用户轮次恰好一次，而不是每次编辑一次。让它安全的那几条不变量：绝不在循环中途改历史；只在*内容 hash* 变化时重渲染，而不是文件变化就重渲染；用"作废并追加"而不是"原地改写"，让变更点之前的前缀存活；以及把 map 排除在 `agent.py:186` 的轮次边界扫描之外，否则压缩器既会切错历史段落，又会在 `agent.py:250-259` 处把 map 删掉（那里只格式化 assistant 和 tool 角色）。这套排序理由里有一半是前缀缓存的经济学，它依赖 C1/C2（[能力矩阵](../PROVIDER_CAPABILITIES.md)，待测）；本端点若没有 prompt caching，选定的设计不变 —— 理由从"缓存失效"换成"历史稳定性与重渲染开销"，并在文档里如实写明，而不是拿缓存节省当论据。

3. **为什么二分 tag 数量，而不是截断渲染后的字符串？** 因为 map 是结构化的，而字符截断会把一个文件的符号列表从中间腰斩——模型于是看到 `bash_tool.py` 只有 6 个符号中的 2 个，并断定另外 4 个不存在，这比压根不列这个文件更糟。所以截断的单位必须是 (file, symbol) 对，渲染器作为 oracle，tiktoken 作为量具——就是 aider 的 `find_best_tree`。成本控制是要紧的：把一个 200 KB 的候选 tokenize 12 次是实打实的开销，所以按探测点缓存计数、用上一轮接受的数量给搜索播种（相邻轮次落点相差不过几对），并接受任何落在 10% 容差带内的结果，而不是去找精确最大值。这条容差带同时兼作迟滞——没有它，加一个函数就把拟合结果挪一对、触发重渲染、改变 hash，为一个模型根本感知不到的变化让 prompt cache 全部失效（最后这笔开销依赖 C1/C2，本端点待测；就算没有 prompt caching，这条容差带也值得留 —— 它此时抵的是无谓重渲染和历史抖动）。还有一点值得明说：cl100k_base 对 MiniMax 或 Claude 都是错的 tokenizer，所以这个预算是估计值——正因如此才留余量而不是精确贴边，这也和 `agent.py:103` 处理压缩阈值时的既有做法一致。

4. **是什么不变量让缓存正确，它又在哪里明知故犯地说谎？** 不变量是：真值来源是 stat 扫描，不是编辑钩子。挂在 `file_tools.py:206`/`:281` 上的钩子是一个延迟优化，让我们可以跳过一次"要不要重解析"的判断；正确性来自每次 `get_map` 都重新 stat 每个候选并比较 `(mtime_ns, size)`。这很要紧，因为 `bash_tool.py:391` 跑的是任意 shell —— `sed -i`、`git checkout`、一个 formatter ——钩子根本不会触发。已知的说谎场景是保留了 mtime 的等大小内容变更（`cp -p`、某些构建系统、网络挂载上的粗粒度文件系统时间戳）；缓解措施是本会话 `_dirty` 集合里的任何东西都不看 stat 直接重解析，而诚实的说法是：每轮对每个文件算 hash 能修好它，但不值得把 20 ms 的扫描变成好几秒。第二条不变量：解析失败必须是非致命的，而且*把失败也缓存下来*——一个编辑到一半带语法错误的文件每轮都会解析失败，每次都重试就是热扫描 20 ms 和 200 ms 的区别。

5. **grep 结果的*形状*如何改变 agent 行为？** 默认返回 `files_with_matches` 而不是内容，是一个行为选择，不是格式选择：它每条命中约 10 个 token 而不是约 40 个，并且把模型推向正确的两步走（先定位，再用 `read_file` 的 offset/limit（`file_tools.py:96-103`）去读），而不是试图从 3 行上下文里硬推。`-C` 上下文是个陷阱——成本三倍，而且通常横跨函数边界，只给模型半个签名。head_limit 必须自我声明，因为一个静默的上限把"我找到了 312 个里的 50 个"变成"一共 50 个"，这是个正确性 bug，会在三步之后以一次做了一半的重构浮出水面。同样的道理驱动了 Glob 在截断*之前*按 mtime 倒序排：如果必须丢结果，就丢最不可能是刚刚在动的那些。而 bash 的截断必须是中间挖空而不是只留头部，因为构建和测试输出把失败摘要放在*末尾*，只留头部的截断稳定地删掉唯一重要的那一行。

6. **什么时候 repo map 是错的工具，替代方案各买到什么？** 三种情形。Embedding/向量检索在词汇错配上胜出——"我们在哪儿处理限流？"而代码里写的是 `retry` 和 `backoff`（这个仓库就是字面上的例子：`mini_agent/retry.py` 里根本没出现这个说法）。它输在成本和新鲜度上：要建索引、要跑模型、每次编辑都要重新 embed，而且 chunk 边界会把一个类和它的方法切开。对一个每几秒就编辑一次代码的 agent 来说，写放大是致命伤。Agentic search（子 agent 扇出，Claude Code 的路线）在精度和规模上胜出——排序由一个上下文里真有任务的模型来做，完全不需要索引，而且在 20 万文件的 monorepo 里优雅降级（那种规模下任何静态 map 都只是舍入误差）。它输在延迟和 token 上：第一次编辑之前要好几轮往返，每个子 agent 各烧自己的窗口。repo map 的生态位窄而真实：它是一个*先验*，不是一个检索器——一笔固定的约 1k token 开销，让 agent 的*第一次* grep 有的放矢而不是 `grep -r auth .`，而且它是三者中唯一在查询时免费的。诚实的组合判断是：map 和 agentic search 是互补的（map 用于定位方向，grep/子 agent 用于精确），而 embedding 是对代码编辑 agent 应当跳过的那一个；一旦仓库超过几千个源文件，map 就退化成顶层目录摘要，该由 agentic search 接手了。

## 前置条件

1. `mini_agent/tools/file_tools.py:280` —— `content.replace(old_str, new_str)` 替换*每一处*出现，而 `file_tools.py:230-231` 的工具描述承诺匹配 "must appear uniquely in the file"。在 `file_tools.py:281` 接失效钩子之前先修：`if content.count(old_str) != 1: return ToolResult(success=False, error=...)`。三行。没有它，"map 在编辑后正确刷新"就不是一个你能诚实演示的主张，因为你根本不知道那次编辑干了什么。

2. `mini_agent/tools/bash_tool.py:418-421` —— 失败路径用完整的 `stderr_text` 构造 `error_msg`，而 `agent.py:470` 对任何失败工具都发送 `f"Error: {result.error}"`，彻底绕开 `BashOutputResult.format_content`（`bash_tool.py:31-47`）。这必须和成功路径在同一次改动里一起截断，否则 Layer 1 的上限上有个洞，每条失败命令都从那儿开过去。

## 明确不做

不做：增量/流式解析（aider 维护一个带后台刷新队列的 SQLite tag 缓存；我们做全量 stat 扫描加一个 JSON 缓存，2000 个文件吃掉约 20 ms）；文件系统监听（inotify/FSEvents）——刷新只在用户轮次边界发生；渲染代码上下文（aider 会打印每个排序符号周围的真实源码行；我们只打印 `line: kind name`，大约四分之一的 token，且足以驱动一次 `read_file`）；`LANG_BY_EXT` 里那约 10 种之外的语言；比文件到文件更细的调用图边（没有按函数的节点，所以 `Agent.run` 和 `Agent._create_summary` 共享其文件的 rank）；一个真正的 `.gitignore` 实现（不支持嵌套的每目录 ignore 文件，不支持 `[a-z]` 字符类——这个子集加上 `DENY_DIRS` 覆盖了要紧的部分）；多根/monorepo 分片（超过 `max_files_scanned` 我们返回空 map 并在后台构建，而不是优雅降级为按包的 map）；以及 JSON 缓存上除原子 rename 之外的任何并发安全，所以同一个 workspace 里的两个 agent 会互相覆盖缓存——无害，因为它只是缓存。

对面试官可以这么说："The map is a *prior*, not a retriever, so the production features I skipped are all about keeping the prior fresh at scale — incremental parsing, file watching, monorepo sharding. I built the part that decides *what is important*, because that is the part with the interesting failure modes: symbol-name collisions, dangling PageRank mass, budget fitting, and cache invalidation against a shell that can edit files behind your back. Everything I cut is engineering I know how to do; nothing I cut would have changed the ranking."

## 代码量

约 1300-1500 LOC 新代码，外加 6 个已有文件中约 40 行改动。分解：search_tools.py 约 330，_ignore.py 约 90，repo_map.py 约 380，symbols.py 约 200，bash clamp 约 45，cli.py/agent.py/file_tools.py/config.py/tools/__init__.py 的接线约 40，测试约 160，bench 脚本约 120。

## 工期

一个人 5-6 天。第 1 天：_ignore.py + GlobTool + GrepTool 两个后端加一致性测试。第 2 天（半天）：bash 截断、两个前置修复、工具注册、系统提示词改动。第 2.5-4 天：symbols.py（py/js/ts/go 的 tree-sitter query 加正则兜底）、repo_map.py 的 scan/cache/graph/pagerank。第 5 天：预算拟合、四个接线钩子（包含 agent.py:186 处与压缩器的交互）、测试。第 6 天：bench 脚本、gold 集、两个 demo、写文档。
