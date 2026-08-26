# ADR-0007：外部测试必须显式允许收集

- 日期：2026-08-25
- 状态：已采纳
- 关联：`conftest.py:11-41`、`pyproject.toml:50-58`、`tests/test_pytest_entrypoint.py:11-112`、`tests/test_agent.py:15`、`tests/test_integration.py:18`、`tests/test_mcp.py:325-493`、[P-008](../PITFALLS.md)、提交 `8616739`

## 背景

原 pytest 配置只声明测试路径、缓存目录和 asyncio 模式，没有区分离线与外部测试。改动前运行 `.venv/bin/python -m pytest --collect-only -q tests/test_agent.py tests/test_integration.py tests/test_mcp.py` 实测收集 33 项：两个模型测试模块共 4 项会读取本地 `config.yaml` 并调用真实端点；MCP 混合模块另有 5 项会读取本地 `mcp.json`、启动或调用已配置的 server，或主动访问网络（`tests/test_agent.py:21-147`、`tests/test_integration.py:30-234`、`tests/test_mcp.py:325-493`）。

AGENTS 与 README 当时只用人工白名单避开前两个模块，还遗漏了 MCP 模块里的外部路径。新增的纯收集回归首次运行即因默认结果仍包含上述 9 个节点而失败；它没有执行任何测试体或访问端点。

## 选项

1. **继续维护离线文件白名单**：文档列出允许执行的测试文件，调用者每次复制完整命令。
2. **只用 pytest 原生 marker 表达式**：在 `addopts` 写 `-m 'not external'`，用命令行 `-m external` 显式覆盖。
3. **能力标记加双层收集门**：以 `external` 表示真实模型、用户 MCP 配置或网络能力；配置默认排除，根级收集 hook 还要求显式 `--run-external`，并严格校验 marker 拼写。
4. **把外部测试全部移到独立目录**：依赖目录选择隔离，不在默认测试路径内收集。

## 决定

采用选项 3。两个完全依赖真实模型的模块使用模块级 `external` marker；`tests/test_mcp.py` 只标记 5 项外部测试，其余 24 项纯配置与初始化测试继续进入默认集合。`pyproject.toml` 同时启用 `--strict-markers` 和默认 `not external`；`conftest.py` 在没有 `--run-external` 时再次移除所有 `external` 项，所以普通 `-m asyncio` 也不能覆盖安全门。

显式外部入口需要同时给出 `--run-external -m external`。本决定只保护 pytest 的收集与执行，不改写这些上游测试的断言，不保证直接运行 `python tests/test_agent.py` 安全，也不把固定、离线的本地 shell 子进程测试归为外部测试。

## 为什么否决其他的

- **继续维护离线文件白名单**：新增测试默认不在白名单，混合文件又迫使调用者在“放弃 24 项离线覆盖”和“连同 5 项外部路径一起运行”之间选择。如果测试集合很小、不可扩展，且唯一执行入口由生成脚本完全控制，白名单反而可能最直观；当前仓库已有混合模块，不满足这些条件。
- **只用 pytest 原生 marker 表达式**：命令行另一个 `-m` 会覆盖配置中的表达式；例如只想筛选 asyncio 测试时，真实模型和外部 MCP 测试也会重新进入集合。如果 CI 和本地命令都由一个不可绕过的包装器生成，并禁止额外 marker 过滤，单层原生表达式会更简单；当前没有这样的入口所有权。
- **把外部测试全部移到独立目录**：目录隔离能在导入前阻止整个模块，适合测试模块导入本身已有副作用，或外部测试已形成独立大型套件的情况。当前模块导入没有外部副作用，而 `tests/test_mcp.py` 同时拥有有价值的离线测试；为隔离 5 项而拆文件会扩大无行为变化的搬运。

## 怎么验证它是对的

```bash
.venv/bin/python -m pytest -q tests/test_pytest_entrypoint.py
.venv/bin/python -m pytest -q tests/test_mcp.py
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest --collect-only -q --run-external -m external \
  tests/test_agent.py tests/test_integration.py tests/test_mcp.py
```

前三条依次实测为 `2 passed in 3.83s`、`24 passed, 5 deselected in 0.50s`、`183 passed, 9 deselected in 13.82s`。最后一条只收集、不执行，实测为 `9/33 tests collected (24 deselected) in 0.65s`。入口回归还验证：不加载根 `conftest.py` 时配置层仍默认排除；普通 `-m asyncio` 不能放行；拼错 marker 会在收集阶段失败。

## 回头看

实现没有移动或重写原上游测试；默认集合反而新增了 MCP 模块的 24 项离线覆盖。复审后增加了配置层第二道排除和拼错 marker 回归：临时删除 `--strict-markers` 时，定向测试实测 `1 failed in 0.30s`，恢复后重新通过。与改动前认知的偏差是外部边界不只真实模型测试，读取用户 MCP 配置和“预期超时”的网络测试也必须按副作用分类。

2026-08-26：三项真实模型演示因吞异常、返回布尔值或使用模型措辞作为判定标准而删除；CLI 已提供明确的真实端点手动入口，agent loop 由脚本化 LLM 离线验证。收集门继续保护 5 项 MCP/网络探测，入口回归相应只以 `tests/test_mcp.py` 为目标，实测 `2 passed in 3.67s`；显式外部收集为 `5/32 tests collected (27 deselected) in 0.44s`。这缩小了当前外部集合，没有推翻按能力标记和双层门控的决定。
