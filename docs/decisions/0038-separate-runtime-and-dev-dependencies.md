# ADR-0038：分开直接运行依赖与开发依赖

- 日期：2026-08-26
- 状态：已采纳
- 关联：`pyproject.toml`、`uv.lock`、`README.md`

## 背景

改动前 `pyproject.toml:11-23` 把 `pytest`、`requests`、`pip`、`pipx` 和 `httpx` 声明为生产依赖。执行 `rg -n '(^|[[:space:]])(import|from) (httpx|requests|pip|pipx|pytest)' mini_agent tests --glob '*.py' --glob '!mini_agent/skills/**'`，只有测试文件导入 `pytest`；生产代码不直接导入另外四项，`httpx` 由 anthropic、openai 和 mcp 的锁图传递提供。

开发依赖又同时存在于 `project.optional-dependencies.dev` 与 `dependency-groups.dev`，`pytest-asyncio` 有两个下限；`pytest-cov`、`pytest-xdist` 和 pylint 配置没有仓库命令、配置或代码消费者。`uv tree --locked --no-dev --depth 1` 显示这些声明让生产根节点直接带上测试器、包管理器和通用 HTTP 客户端。

## 选项

1. **保留全部预备依赖**：环境继续包含可能有用的工具，但安装面与代码实际需求不一致。
2. **只把 pytest 移到开发区**：修正最明显的分类，仍保留四个零导入生产依赖和两套开发入口。
3. **运行与开发各自单一来源**：生产区只列代码直接导入的库，`dependency-groups.dev` 只列当前默认测试所需的两项。
4. **保留 `dev` extra，删除 dependency group**：继续支持 `pip install .[dev]`，但 README 的 `uv sync` 不再有本地开发组。

## 决定

采用选项 3。生产依赖只保留 pydantic、PyYAML、mcp、prompt-toolkit、anthropic 和 openai；开发组只保留 pytest 与 pytest-asyncio。删除直接 `httpx`、`requests`、`pip`、`pipx`、`project.optional-dependencies.dev`、pytest-cov、pytest-xdist 及无执行入口的 pylint 配置。

`httpx` 仍作为现有 SDK/MCP 的传递依赖留在锁图，但本项目不以直接依赖约束其版本。`uv sync` 继续默认安装 `dev` 开发组；普通发布依赖不再附带测试和包管理工具。本轮不增加、升级或替换任何依赖，也不改变 Python 实现、CLI 或 agent 工具。

## 为什么否决其他的

**否决保留预备依赖**：依赖声明应说明当前代码为何需要某个包，而不是充当可能用到的工具清单；尤其 pip/pipx 会把环境管理器错误包装成 agent 运行能力。若实现真正直接导入 `httpx` 或 `requests`，或安装流程明确从 Python 调用包管理 API，应随该行为和测试恢复直接依赖。

**否决只移动 pytest**：它不能解决两套开发入口和其余零消费者声明，锁图仍被无关工具扩大。若这些包即将由同一项已授权实现使用，且该实现必须与元数据同一提交落地，暂时保留才有意义；当前没有这种工作。

**否决保留 `dev` extra**：仓库安装说明只有 `uv sync`，没有 `pip install .[dev]` 消费者；双入口已经产生 pytest-asyncio 下限分叉。若项目发布后需要同时正式支持 pip contributor workflow，并有自动化验证两种安装路径，extra 应恢复并与 dependency group 由单一生成来源同步。

**不保留 coverage/xdist**：仓库没有覆盖率门槛、并行测试命令或配置，安装插件不能代替评测设计。若 CI 增加可执行的覆盖率目标或测试时长证明确需并行，它们应作为该验证入口的直接开发依赖恢复。

## 怎么验证它是对的

- `uv lock --offline --dry-run` 使用本机既有缓存实测只计划删除 12 个包，没有新增或升级。
- `uv lock --check` 实测 `Resolved 47 packages`；生产树只有 6 个直接节点，`--only-dev` 树只有 pytest 与 pytest-asyncio。
- 用 Python 标准库解析改动前提交与当前 `uv.lock`，实测 `packages 59 -> 47; retained versions unchanged; removed 12`。
- `.venv/bin/python -m pytest -q` 实测 `257 passed, 5 deselected in 13.62s`；外部 MCP 与网络测试本次未运行。

## 回头看

首次用当前 `uv 0.11` 直接生成锁文件时，工具还把原清华镜像源改成 PyPI、把 `revision` 从 1 改成 3，造成与依赖删除无关的大面积文本变化。该结果没有进入实现：恢复原锁格式后，只修改根包依赖并删除 dry-run 已确认的 12 个独占包块，`uv lock --check` 仍接受。最终 `pyproject.toml` 净减 19 行，`uv.lock` 净减 330 行；所有保留版本、镜像源和 `revision` 不变。
