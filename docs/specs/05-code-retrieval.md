# Glob/Grep + repo map

> 生成的实现规格草案，随实现验证；其中每个具体数字都要自己重测。
> 取舍与优先级见 [../BUILD_LIST_CN.md](../BUILD_LIST_CN.md)。

`Repo-aware code search: Glob/Grep tools + a tree-sitter repo map (PageRank-ranked symbol index)`


## 一句话

Give the agent two cheap structural retrieval primitives it currently lacks — a gitignore-aware Glob/Grep pair with hard result caps (plus bash output truncation so a stray `grep -r` cannot eat the window) — and on top of that a tree-sitter repo map that extracts per-file symbol definitions and references, ranks files by personalized PageRank over the reference graph, and renders the top symbols into a fixed token budget injected once per session and refreshed on edit.

## 为什么这是难点

A coding agent's first turn in an unfamiliar repo is a blind guess. The model has a task ("fix the retry backoff") and a filesystem it cannot see. Everything downstream — which file it reads, whether the edit lands in the right layer, whether it burns 30 steps — is determined by that first retrieval decision. This is the *entry point* to every other agent capability: context management only matters if the right things got into the context in the first place.

It is hard because the two obvious solutions both fail. Dumping structure is unaffordable: this repo's naive `Path('.').rglob('*')` returns 6301 files against ripgrep's 364 (sampled 2026-08-23; re-measured 7072 / 391 on 08-24 — absolute counts move with the working tree, **the ~18x ratio is the point**; re-measure before citing), and a single `rg -n "def " .` produces 76550 characters (~20k tokens) that Mini-Agent today hands to the model verbatim (bash_tool.py:412-429 decodes and returns with no cap). Letting the model grope around blindly is slow and lossy: without priors it greps for the wrong word, gets 400 hits, and truncation silently lies to it.

The interesting middle is a *ranking* problem, not a search problem: which 25 of 364 files should the model know exist before it asks anything? That is a graph question — importance flows from a file to the files whose symbols it references — and answering it well requires handling symbol-name collisions, dangling nodes, task conditioning, a token budget, and cache invalidation under concurrent edits. Those five together are exactly what separates reading aider's blog post from having built it.

## 仓库现状

**There is no search capability at all.** `mini_agent/tools/__init__.py:3-6` exports exactly `ReadTool, WriteTool, EditTool, BashTool, SessionNoteTool, RecallNoteTool`. `cli.py:418-427` registers only Read/Write/Edit. There is no Glob, no Grep, no directory listing. The system prompt (`mini_agent/config/system_prompt.md:41-51`) tells the model to "Verify file existence before reading" and "Don't guess - use tools to discover missing information" while giving it no tool that can discover anything except raw `bash`.

**So all discovery falls through to bash, which is uncapped.** `bash_tool.py:391-396` runs the command via `create_subprocess_shell`, `bash_tool.py:411-413` decodes stdout/stderr in full, and `bash_tool.py:423-429` returns them. `BashOutputResult.format_content` (`bash_tool.py:31-47`) concatenates stdout + stderr + exit code into `content` with no length check. `agent.py:470` then puts `result.content` straight into a `tool` message. A `grep -rn` at repo root is ~20k tokens against a `token_limit` of 80000 (`agent.py:28`) — one command consumes a quarter of the budget, and two of them trigger the prose compactor at `agent.py:153-233`, which is lossy.

**The failure path is worse than the success path.** `bash_tool.py:418-421` builds `error_msg = f"Command failed with exit code {rc}" + "\n" + stderr_text.strip()` with the *entire* stderr, and `agent.py:470` sends `f"Error: {result.error}"` for any unsuccessful tool. So `find / -name '*.py'` (exit 1, megabytes on stderr from permission errors) bypasses even `content` formatting entirely.

**The one truncation that exists is in the wrong place and is itself a hazard.** `file_tools.py:11-60` `truncate_text_by_tokens` is applied only in `ReadTool.execute` (`file_tools.py:147-148`, 32k token cap). Its first act is `encoding.encode(text)` on the *whole* string (`file_tools.py:32-33`) before deciding whether truncation is needed — O(n) tiktoken work on input that may be tens of MB, with no cheap character-count pre-check.

**No structural index of any kind.** Nothing in `mini_agent/` parses source. The system prompt's only orientation is `agent.py:41-44`, which appends one sentence naming the workspace path. `tiktoken` is already a dependency and already used for budgeting (`agent.py:96-131`), so the measuring instrument exists; the thing to measure does not.

**Two seams that constrain the design.** (1) `add_workspace_tools` (`cli.py:399-432`) is shared by the CLI (`cli.py:543`) and the ACP server (`acp/__init__.py:101`), so a single hook there covers both loops. (2) The compactor's `user_indices` scan at `agent.py:186` treats *every* `role == "user"` message as a turn boundary, which an injected repo-map message would corrupt.

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

New dependency (one, justified): `tree-sitter-language-pack>=0.7` — prebuilt wheels for ~160 grammars, no compiler needed. Import is wrapped in try/except; `RegexExtractor` covers py/js/ts/go when the import fails, so the repo never hard-depends on it.

---

## Layer 1 — Glob, Grep, and bash truncation

### `mini_agent/tools/_ignore.py`

```python
DENY_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".tox", ".next", "target", ".idea"}

class IgnoreRules:
    """Minimal .gitignore matcher: literal, *, **, dir/, leading /, negation."""
    def __init__(self, root: Path): ...
    @classmethod
    def load(cls, root: Path) -> "IgnoreRules":   # root/.gitignore + root/.git/info/exclude
    def ignored(self, rel: str, is_dir: bool) -> bool: ...

def walk_files(root: Path, rules: IgnoreRules, include_hidden: bool = False,
               max_files: int = 20_000) -> Iterator[Path]:
    """os.walk with followlinks=False, prunes dirnames in place, tracks visited
    (st_dev, st_ino) to break symlink loops, stops at max_files."""
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
Backend A (`rg --files --hidden --glob '!.git' -g <pattern>`), backend B (`walk_files` + `fnmatch` on the relative path, `**` expanded). Results are **sorted by mtime descending** before the cap — this is the single most important line in the tool: with a 200-file cap on a 5000-file match, recency ordering is what makes the cap harmless. Output is newline-joined relative paths plus, when capped, a literal `[showing 200 of 4193 matches, newest first]` footer.

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
Backend A shells out to `rg --no-heading --line-number --color never --hidden --glob '!.git'` (+ `-l` / `-c` / `-C n` / `-i` / `-g <glob>`), reading stdout with a byte budget so a pathological pattern cannot fill memory. Backend B compiles the pattern with `re`, walks via `walk_files`, opens files in binary, skips any file whose first 8192 bytes contain `b"\x00"` or that exceeds 2 MB, decodes with `errors="replace"`, and breaks out of the whole walk once `head_limit` results are collected.

Rendering (token economy is the point):
- `files_with_matches`: bare relative paths, one per line.
- `count`: `path:N`, sorted descending.
- `content`: grouped per file, `path:LINE: text`, each line hard-capped at 200 chars with `…`.
- Always append `[truncated: showing N of M matches — narrow the pattern or pass a glob]` when capped. Never truncate silently.

Both tools validate that the resolved path stays under `workspace_dir` (same `Path.is_absolute()` pattern as `file_tools.py:112-114`) and return `ToolResult(success=False, ...)` on a bad regex rather than raising.

### Bash truncation — two edits, both in `bash_tool.py`

Add near the top:

```python
MAX_OUTPUT_LINES = 200      # 100 head + 100 tail
MAX_LINE_CHARS  = 2000
MAX_OUTPUT_CHARS = 60_000   # absolute backstop

def clamp_output(text: str, label: str = "output") -> str:
    """Line-wise middle-out clamp. Char-count guard FIRST, no tokenizer."""
```
Algorithm: return unchanged if `len(text) <= 8000` (fast path, no split). Otherwise `splitlines()`, clamp each line to `MAX_LINE_CHARS`, and if `len(lines) > MAX_OUTPUT_LINES` keep the first 100 and last 100 joined by `\n... [{label} truncated: {n} lines omitted, {len(text)} chars total] ...\n`. Finally hard-slice to `MAX_OUTPUT_CHARS` if a single line was pathological.

1. **`bash_tool.py:31-47`** — inside `BashOutputResult.format_content`, replace `output += self.stdout` with `output += clamp_output(self.stdout, "stdout")` and the stderr branch with `clamp_output(self.stderr, "stderr")`. This covers foreground (`bash_tool.py:423-429`) and `bash_output` results in one place.
2. **`bash_tool.py:418-421`** — `error_msg += f"\n{stderr_text.strip()}"` becomes `error_msg += "\n" + clamp_output(stderr_text.strip(), "stderr")`. Without this, every *failing* command bypasses the cap via `agent.py:470`.

Also move `truncate_text_by_tokens` out of `file_tools.py:11-60` unchanged except for one added guard after the docstring: `if len(text) <= max_tokens * 2: return text` (2 chars/token is a safe floor for cl100k) so the `encoding.encode` at line 32 never runs on huge input.

### Registration

- `tools/__init__.py:5` → add `from .search_tools import GlobTool, GrepTool`; extend `__all__` at lines 8-17.
- `cli.py:420-426` → the `tools.extend([...])` list gains `GlobTool(workspace_dir=str(workspace_dir)), GrepTool(workspace_dir=str(workspace_dir))`. This one edit also covers ACP, via `acp/__init__.py:101`.
- `config/system_prompt.md:41-45` ("File Operations" bullet list) → add: "Use `glob` to find files by name/pattern and `grep` to find files by content **before** reading. Prefer them over `bash` with find/grep — bash output is truncated. `grep` defaults to returning filenames only; ask for `output_mode=\"content\"` only when you need the matching lines."

---

## Layer 2 — the repo map

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
Per language, run two queries against the parse tree:
- **defs** — e.g. python `(function_definition name:(identifier) @def.function) (class_definition name:(identifier) @def.class)`; only nodes whose ancestor chain contains at most one `class_definition` (top-level defs and methods, not nested closures).
- **refs** — every `(identifier)` / `(attribute attribute:(identifier))` node that is *not* the name child of a definition. Filter against a 60-word stoplist (`self, cls, i, x, str, int, list, dict, len, print, name, type, id, value, data, result, args, kwargs, ...`) and drop names shorter than 3 chars.

```python
class RegexExtractor:
    """Fallback: line-oriented regexes for python/js/ts/go defs + \b[A-Za-z_][\w]{2,}\b refs."""
```
`get_extractor()` returns TreeSitter if importable else Regex, cached at module level. Both must produce identical `Tag` shapes; the tests assert the regex extractor finds a strict subset of the tree-sitter defs on `mini_agent/agent.py`.

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

**Step 1 — scan (`_scan`).** Enumerate candidates with `walk_files(root, IgnoreRules.load(root))` filtered to `LANG_BY_EXT`. `os.stat` each. Reuse the cached `FileEntry` when `(mtime_ns, size)` match *and* the path is not in `self._dirty`; otherwise re-parse. Sort candidates by "recently touched in git" when `.git` exists — `git log --name-only --pretty=format: -n 400` gives a frequency table in ~100 ms — else by mtime, and cut at `max_files_scanned`. Persist the cache as JSON on exit of `get_map`.

**Step 2 — graph (`_build_graph`).** Build `definers: dict[str, set[str]]` from all `kind == "def"` tags, then drop any symbol with `len(definers[sym]) > max_definers_per_symbol`. For each file A and each referenced symbol `s` with `n` occurrences in A:

```python
mul = 10.0 if s in mentioned_symbols else (0.1 if s.startswith("_") else 1.0)
w   = math.sqrt(n) * mul / len(definers[s])
for B in definers[s]:
    if B != A:
        adj[A][B] += w
        edge_labels[(A, B)][s] += w
```

**Step 3 — personalized PageRank (`_pagerank`, ~35 LOC, no networkx).**

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

**Step 4 — rank definitions (`_rank_tags`).** Push each file's rank out along its edges and attribute it to the *symbol* that created the edge:

```python
for (A, B), syms in edge_labels.items():
    tot = sum(adj[A].values())
    for s, w in syms.items():
        def_score[(B, s)] += r[A] * w / tot
```
Files with rank but no incoming symbol credit still get a bare path line at `r[file] * 0.01` so a high-rank leaf never vanishes entirely.

**Step 5 — render + budget fit (`_render`, `_fit_budget`).** Render is grouped by file, symbols in line order:

```
mini_agent/tools/bash_tool.py
    53: class BackgroundShell
   130: class BackgroundShellManager
   217: class BashTool
mini_agent/agent.py
    18: class Agent
   153: async def _summarize_messages
```
`_fit_budget` binary-searches the *number of ranked (file, symbol) pairs* included, not the rendered characters: `lo, hi = 0, len(ranked)`, render the candidate, count with `tiktoken.get_encoding("cl100k_base")`, accept the first result within `[max_tokens * (1 - tolerance), max_tokens]`, ~12 iterations. Cache token counts per `mid` inside the search. Prepend one header line: `` `<repo-map>` Repository structure (top {n} files by reference-graph importance; not exhaustive — use glob/grep to find anything not listed).``

### Wiring — the four hooks

1. **`file_tools.py:158-165` and `212-221`** — `WriteTool.__init__` / `EditTool.__init__` gain `on_change: Callable[[Path], None] | None = None`; call it after the successful writes at `file_tools.py:206` and `file_tools.py:281`, inside a `try/except Exception: pass` so a cache bug can never break an edit.
2. **`cli.py:399-432` (`add_workspace_tools`)** — build the map before the tool list and return it:
   ```python
   repo_map = RepoMap(workspace_dir) if config.tools.enable_repo_map else None
   hook = repo_map.note_file_changed if repo_map else None
   ...
   WriteTool(workspace_dir=str(workspace_dir), on_change=hook),
   EditTool(workspace_dir=str(workspace_dir), on_change=hook),
   ```
   Change the signature to `-> RepoMap | None` and update both call sites: `cli.py:543` and `acp/__init__.py:101`.
3. **`cli.py:568-575`** — pass `repo_map=repo_map` into the `Agent(...)` constructor (and the same at `acp/__init__.py:102`). `config.py:48-63 ToolsConfig` gains `enable_repo_map: bool = True` and `repo_map_tokens: int = 1024`.
4. **`agent.py`** — three surgical changes:
   - `agent.py:21-29` constructor gains `repo_map: "RepoMap | None" = None`; store it and `self._map_hash = ""`.
   - `agent.py:59-61 add_user_message` becomes:
     ```python
     def add_user_message(self, content: str):
         self._refresh_repo_map()
         self.messages.append(Message(role="user", content=content))
     ```
     where `_refresh_repo_map` (new, ~25 LOC) computes `focus_files` = the last 5 distinct paths seen in `read_file`/`edit_file` tool calls in history, calls `get_map`, and: inserts the map `Message(role="user", content=REPO_MAP_SENTINEL + ...)` at index 1 on the first call; on later calls, if the hash changed, rewrites the *old* map message's content to the single line `REPO_MAP_SENTINEL + " (superseded)"` and appends the fresh one before the incoming user turn.
   - `agent.py:186` — exclude map messages from turn boundaries:
     ```python
     user_indices = [i for i, m in enumerate(self.messages)
                     if m.role == "user" and i > 0
                     and not (isinstance(m.content, str) and m.content.startswith(REPO_MAP_SENTINEL))]
     ```
     and in the rebuild loop at `agent.py:209-221`, carry any map message found inside `execution_messages` through to `new_messages` verbatim instead of letting `_create_summary` (`agent.py:250-259`, which only formats `assistant`/`tool` roles) silently drop it.

The first `get_map()` runs synchronously on the first user message; if `_scan` reports more than `max_files_scanned` candidates, `get_map` returns `""` immediately and schedules the build with `asyncio.create_task`, so turn 1 is never blocked on a monorepo.

## 边界情况

1. **Backend parity on hidden files.** Obvious: use `rg --files` when ripgrep exists and `Path.rglob('*')` otherwise, assume they agree. Reality, measured in this repo: `rg --files` returns 364, `rg --files --hidden` returns 435, and `Path('.').rglob('*')` returns 6301 — ripgrep silently skips dotfiles *and* .gitignore'd paths, the Python fallback skips nothing. An agent asked "where is CI configured?" gets zero hits from the rg backend (`.github/` is hidden) and drowns in `.venv` on the fallback. Right: pin one policy in `_ignore.py` and force both backends to it — pass `--hidden --glob '!.git'` to ripgrep, and apply `IgnoreRules` + `DENY_DIRS` in the Python walk — then assert parity in a test that runs both backends over `tests/fixtures/` and diffs the sets.

2. **Symbols defined in many files smear the graph.** Obvious: every reference to symbol `s` adds an edge to every file defining `s`. In this repo `def name`, `def description`, and `def parameters` are each defined in 11 files (every `Tool` subclass) and `async def execute` in 11 more — a single `tool.execute(...)` call site emits 11 edges of equal weight, so rank pools in whichever tool file happens to be alphabetically first and the interesting file gets 1/11 of the credit. Right: divide edge weight by `len(definers[s])`, drop symbols with more than `max_definers_per_symbol` definers entirely, damp private names (`_x` → 0.1x), and weight by `sqrt(n_refs)` rather than `n_refs` so one file calling `read_file` 40 times does not outrank ten files calling it twice.

3. **Dangling nodes and the 'base.py is the most important file' result.** Obvious: implement PageRank as `r[dst] += d * r[src] / out_degree[src]`, iterate, done. Two bugs follow. (a) Nodes with no outgoing edges (`__init__.py`, `schema.py`, pure-leaf modules) absorb rank and never return it; total mass shrinks every iteration and the ranking becomes a function of the iteration count. (b) With a uniform teleport vector the winner is always the most-imported file — here `tools/base.py` and `schema/schema.py` — which is exactly the file the agent least needs told to it. Right: redistribute dangling mass through the personalization vector each iteration (`new[n] = (1-d)*p[n] + d*dangling*p[n] + ...`), and concentrate `p` on the files the agent has actually touched this session so rank flows toward the task, not toward the leaves.

4. **Truncating silently is a correctness bug, not a cosmetic one.** Obvious: cap grep at 50 matches and return them. The model then reasons "there are exactly 50 call sites, I have seen them all" and refactors 50 of 300. Same failure for Glob's 200-file cap and for bash's output clamp. Right: every cap emits a machine-readable footer naming the real total (`[truncated: showing 50 of 312 matches — narrow the pattern or pass a glob]`), the repo map's header says "not exhaustive — use glob/grep", and Glob sorts by mtime *before* capping so the surviving subset is the useful one. The rule: a truncated result must be self-describing, because the model cannot see what it was not shown.

5. **Edits made through `bash` bypass the invalidation hook.** Obvious: hang cache invalidation off `WriteTool`/`EditTool` (`file_tools.py:206`, `file_tools.py:281`) and trust it. But `bash_tool.py:391` runs an arbitrary shell — `sed -i`, `git checkout`, `black .`, a codegen script — and none of that touches the hook, so the map confidently describes symbols that no longer exist. Right: treat the hook as an *optimization* and the mtime+size stat scan in `_scan` as the *invariant*; every `get_map` re-stats all candidates (~2000 stats ≈ 20 ms) and reparses only what changed. Second-order: `(mtime_ns, size)` still lies for a same-size content change with a preserved mtime (`cp -p`, some build tools), so any path in `self._dirty` is reparsed unconditionally regardless of stat. Hashing every file every turn is the wrong fix — it turns a 20 ms scan into seconds.

6. **Refreshing the map costs more than it saves if you refresh in place.** Obvious: keep one map message and rewrite its content whenever a file changes. That message sits at index 1, so rewriting it invalidates the prompt prefix for *every* subsequent message — you pay a full re-read of the conversation to update 1024 tokens, on every edit. Right: refresh only at user-turn boundaries (`agent.py:59-61`, never mid-loop), only when the rendered content hash actually changed, and by appending a new map message while stubbing the old one — the stub keeps the prefix stable up to the point of change. Add rank hysteresis (`hysteresis: 0.15`) so a one-symbol edit that reshuffles ranks by 2% does not re-render at all.

7. **A `role='user'` map message corrupts the existing compactor.** `agent.py:186` collects `user_indices` as every message with `role == 'user'`, and `agent.py:198-221` rebuilds history as `[system] + [user_k + summary_k ...]`. An injected map message therefore (a) creates a bogus turn boundary, splitting one real turn into two summarized segments, and (b) if it lands *inside* an execution slice, gets handed to `_create_summary`, which at `agent.py:250-259` only formats `assistant` and `tool` roles — so the map is silently deleted at the first compaction and the agent goes blind exactly when the context is longest. Right: sentinel-prefix the content, exclude it from `user_indices`, and carry it through the rebuild verbatim.

## 怎么证明它有效

Two offline demos, no API key, both under a minute of compute. `scripts/bench_repo_map.py` (~120 LOC).

**Demo 1 — ranking quality (the real claim).** Hand-label 10 (question → ground-truth file) pairs over this repo, written into the script as a dict:

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

Score four rankers on **recall@10** and **MRR**: (a) personalized PageRank seeded with one plausible starting file per query, (b) plain PageRank (uniform teleport), (c) mtime-descending, (d) file size. Run it twice — once on Mini-Agent (364 files) and once on `.venv/lib/python3.11/site-packages/openai` (~250 files, already on disk, offline) with a second gold set.

```bash
uv run python scripts/bench_repo_map.py --root . --repeat 3
uv run python scripts/bench_repo_map.py --root .venv/lib/python3.11/site-packages/openai
```

Output is one table: ranker, recall@10, MRR, build time (cold / warm cache), rendered tokens. The claim is proven if personalized PR beats mtime and size by a wide margin on recall@10 *and* beats plain PageRank on MRR — the latter is the interesting comparison, because it isolates the value of task conditioning from the value of the graph. Also print the top-5 files under plain PR to show the "everything imports `base.py`" degenerate result concretely.

**Demo 2 — the token bill.** Print, for three commands, the character count of `BashOutputResult.content` before and after `clamp_output`, with tiktoken counts:

```bash
uv run python scripts/bench_repo_map.py --bash-demo
#   rg -n "def " .        76550 chars / ~20100 tok  ->  ~7900 chars / ~2100 tok
#   find . -name "*.py"     ...
#   ls -R .venv             ...   (exit!=0 path, proving bash_tool.py:418-421 is covered)
```
Alongside it, the same three questions answered via `GrepTool(output_mode="files_with_matches")` — typically under 300 tokens — to show the tool is not just a cap but a cheaper shape.

Both demos print a single line each that can be pasted into a README, plus `pytest tests/test_repo_map.py tests/test_search_tools.py` for the invariants (backend parity, PageRank mass conservation to 1e-9, budget fit within tolerance, map survives one round of `_summarize_messages`).

## 深度追问

1. **Why personalized PageRank instead of counting inbound references?** In-degree is degree centrality: it answers "how many files import this?", whose top answer in any codebase is the utility module — here `tools/base.py` and `schema/schema.py`, which the agent almost never needs to be told about. PageRank is eigenvector centrality: importance flows *from* important files, so a file referenced once by the entry point outranks one referenced ten times by tests. The personalization vector is where the task enters: teleport mass concentrated on the files the agent has already read makes rank flow into that file's neighborhood, so the same repo produces a different map for "fix retry backoff" than for "add a tool". Rejected: raw import-graph BFS from the entry point (breaks on dynamic imports and on repos with many entry points, and gives no ordering within a hop); TF-IDF over identifiers (no notion of direction — it cannot distinguish 'defines' from 'uses'); plain in-degree (the base.py failure above). Damping at 0.85 is not arbitrary: it is the expected 1/(1-d) ≈ 6.7-hop random walk, roughly the depth of a call chain worth caring about; lower damping degenerates toward the personalization vector itself.

2. **Where do you inject the map, and why is that the hardest decision?** Three options, all bad in different ways. In the system prompt: maximally prefix-cacheable, but it is built before the first user message so it cannot be task-conditioned, and any refresh invalidates the entire cached prefix. In every turn's user message: always fresh, but you pay 1024 tokens per turn and duplicate stale copies pile up in history. Once at index 1, refreshed at user-turn boundaries only: the chosen design — the prefix stays byte-identical during the tool loop (which is where 90% of the requests happen), and a refresh costs cache invalidation exactly once per user turn, not once per edit. The invariants that make it safe: never mutate history mid-loop; only re-render when the *content hash* changed, not when a file changed; supersede-and-append rather than rewrite-in-place so the prefix before the change survives; and exclude the map from `agent.py:186`'s turn-boundary scan or the compactor both mis-segments the history and deletes the map at `agent.py:250-259`, which only formats assistant and tool roles. Half of this ranking argument is prefix-cache economics, which depends on C1/C2 ([capability matrix](../PROVIDER_CAPABILITIES.md), untested); if this endpoint has no prompt caching, the chosen design does not change — the argument for it becomes history stability and re-render cost rather than cache invalidation, and the spec says so instead of quoting a cache saving.

3. **Why binary-search the tag count instead of truncating the rendered string?** Because the map is structured, and character truncation cuts a file's symbol list in half — the model then sees `bash_tool.py` with 2 of its 6 symbols and concludes the other 4 do not exist, which is worse than not listing the file at all. So the unit of truncation must be the (file, symbol) pair, with the renderer as the oracle and tiktoken as the measure — aider's `find_best_tree`. Cost control matters: tokenizing a 200 KB candidate 12 times is real work, so you cache counts per probe, seed the search from the previous turn's accepted count (adjacent turns land within a few pairs), and accept anything inside a 10% tolerance band rather than searching for the exact maximum. The tolerance band doubles as hysteresis — without it a single added function flips the fit by one pair, re-renders, changes the hash, and invalidates the prompt cache for a change the model cannot even perceive (that last cost depends on C1/C2, untested here; with no prompt caching the tolerance band still earns its keep by suppressing pointless re-renders and history churn). Also worth saying out loud: cl100k_base is the wrong tokenizer for MiniMax or Claude, so the budget is an estimate — which is why you leave headroom rather than fitting exactly, and it is consistent with what `agent.py:103` already does for the compaction threshold.

4. **What is the invariant that makes the cache correct, and where does it knowingly lie?** The invariant is: the stat scan, not the edit hook, is the source of truth. The hook off `file_tools.py:206`/`:281` is a latency optimization that lets us skip a reparse decision; correctness comes from re-statting every candidate on each `get_map` and comparing `(mtime_ns, size)`. That matters because `bash_tool.py:391` runs an arbitrary shell — `sed -i`, `git checkout`, a formatter — and the hook never fires. The known lie is a same-size content change with a preserved mtime (`cp -p`, some build systems, coarse filesystem timestamps on network mounts); the mitigation is that anything in the session's `_dirty` set is reparsed regardless of stat, and the honest statement is that hashing every file every turn would fix it and is not worth turning a 20 ms scan into seconds. Second invariant: parse failures must be non-fatal and *cached as failures* — a file with a syntax error mid-edit will fail to parse every turn, and re-attempting it each time is the difference between a warm scan of 20 ms and one of 200 ms.

5. **How does the *shape* of a grep result change agent behavior?** Defaulting to `files_with_matches` rather than content is a behavioral choice, not a formatting one: it costs ~10 tokens per hit instead of ~40, and it pushes the model into the correct two-step (locate, then read with `read_file`'s offset/limit at `file_tools.py:96-103`) instead of trying to reason from 3 lines of context. `-C` context is a trap — it triples cost and usually straddles a function boundary, giving the model half a signature. The head_limit must announce itself, because a silent cap converts "I found 50 of 312" into "there are 50", and that is a correctness bug that surfaces three steps later as a half-finished refactor. Same reasoning drives Glob's mtime-descending sort *before* the cap: if you must drop results, drop the ones least likely to be the ones just worked on. And the bash clamp must be middle-out rather than head-only, because build and test output puts the failure summary at the *end*, so a head-only truncation reliably deletes the only line that mattered.

6. **When is a repo map the wrong tool, and what do the alternatives buy?** Three regimes. Embedding/vector search wins on vocabulary mismatch — "where do we handle rate limiting?" when the code says `retry` and `backoff` (which is literally this repo: `mini_agent/retry.py` contains no occurrence of the phrase). It loses on cost and freshness: an index to build, a model to run, re-embedding on every edit, and chunk boundaries that split a class from its methods. For an agent that edits code every few seconds, the write amplification is the killer. Agentic search (subagent fan-out, Claude Code's approach) wins on precision and on scale — the ranking is done by a model that has the actual task in context, it needs no index at all, and it degrades gracefully in a 200k-file monorepo where any static map is a rounding error. It loses on latency and tokens: several round trips before the first edit, each subagent burning its own window. The repo map's niche is narrow and real: it is a *prior*, not a retriever — a fixed ~1k-token cost that makes the agent's *first* grep targeted instead of `grep -r auth .`, and it is the only one of the three that is free at query time. The honest portfolio statement is that map and agentic search are complements (map for orientation, grep/subagents for precision) and embeddings are the one to skip for a code-editing agent; the moment the repo exceeds a few thousand source files, the map degrades to top-level-directory summaries and agentic search should take over.

## 前置条件

1. `mini_agent/tools/file_tools.py:280` — `content.replace(old_str, new_str)` replaces *every* occurrence while the tool description at `file_tools.py:230-231` promises the match "must appear uniquely in the file". Fix before wiring the invalidation hook at `file_tools.py:281`: `if content.count(old_str) != 1: return ToolResult(success=False, error=...)`. Three lines. Without it, 'the map refreshes correctly after an edit' is not a claim you can honestly demo, because you do not know what the edit did.

2. `mini_agent/tools/bash_tool.py:418-421` — the failure path builds `error_msg` from the full `stderr_text`, and `agent.py:470` sends `f"Error: {result.error}"` for any unsuccessful tool, bypassing `BashOutputResult.format_content` (`bash_tool.py:31-47`) entirely. This must be clamped in the same change as the success path or Layer 1's cap has a hole that every failing command drives through.

## 明确不做

Not building: incremental/streaming parsing (aider keeps a SQLite tag cache with a background refresh queue; we do a full stat scan plus a JSON cache and eat ~20 ms for 2000 files); filesystem watching (inotify/FSEvents) — refresh happens at user-turn boundaries only; rendered code context (aider prints the actual source lines around each ranked symbol; we print `line: kind name`, roughly a quarter of the tokens and enough to drive a `read_file`); languages beyond the ~10 in `LANG_BY_EXT`; call-graph edges finer than file-to-file (no per-function nodes, so `Agent.run` and `Agent._create_summary` share their file's rank); a real `.gitignore` implementation (no nested per-directory ignore files, no `[a-z]` character classes — the subset plus `DENY_DIRS` covers what matters); multi-root/monorepo sharding (over `max_files_scanned` we return an empty map and build in the background rather than degrading gracefully to per-package maps); and any concurrency safety on the JSON cache beyond an atomic rename, so two agents in one workspace will clobber each other's cache — harmless, since it is a cache.

To an interviewer: "The map is a *prior*, not a retriever, so the production features I skipped are all about keeping the prior fresh at scale — incremental parsing, file watching, monorepo sharding. I built the part that decides *what is important*, because that is the part with the interesting failure modes: symbol-name collisions, dangling PageRank mass, budget fitting, and cache invalidation against a shell that can edit files behind your back. Everything I cut is engineering I know how to do; nothing I cut would have changed the ranking."

## 代码量

~1300-1500 LOC of new code, plus ~40 lines changed across 6 existing files. Breakdown: search_tools.py ~330, _ignore.py ~90, repo_map.py ~380, symbols.py ~200, bash clamp ~45, wiring in cli.py/agent.py/file_tools.py/config.py/tools/__init__.py ~40, tests ~160, bench script ~120.

## 工期

5-6 days for one person. Day 1: _ignore.py + GlobTool + GrepTool with both backends and the parity test. Day 2 (half): bash clamp, the two prerequisite fixes, tool registration, system-prompt edits. Days 2.5-4: symbols.py (tree-sitter queries for py/js/ts/go plus the regex fallback), repo_map.py scan/cache/graph/pagerank. Day 5: budget fit, the four wiring hooks including the compactor interaction at agent.py:186, tests. Day 6: bench script, gold sets, the two demos, writeup.
