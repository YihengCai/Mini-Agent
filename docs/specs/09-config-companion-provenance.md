# 配置伴随文件的来源边界

> 状态：已实现。路径解析位于 `mini_agent/config.py:14-23`，CLI 来源传递与消费位于 `mini_agent/cli.py:351-444,548-690`，离线回归位于 `tests/test_config_provenance.py:1-158`；取舍见 [ADR-0015](../decisions/0015-bind-config-companions-to-selected-source.md)。

## 问题证据

原 CLI 只用已选路径读取 `config.yaml`，然后丢弃来源；`system_prompt.md` 与 `mcp.json` 各自重新按开发、用户、包目录搜索。临时目录探针证明三者可以来自不同父目录，而模板声明它们属于同一配置目录。

## 本轮不变量

1. 主配置路径由现有优先级选择一次，并显式传入同一次 CLI runtime。
2. 相对 `system_prompt_path` 与 `mcp_config_path` 只相对于该主配置的词法父目录解释。
3. 显式绝对路径原样保留，不转换成其他来源的同名文件。
4. 相对文件缺失时不跨目录回退：系统提示词使用内置值，MCP 保持未加载。
5. 路径来源不进入 Pydantic 配置值、序列化或相等语义。

## 不在范围

不改变主配置搜索优先级、`skills_dir`、`workspace_dir`、`../`、符号链接、`~` 展开或权限边界；不把缺失伴随文件升级为启动失败，也不改变已存在但不可读或内容无效时的错误处理。

## 离线验证

- 同一测试同时放置主配置目录文件和全局搜索诱饵，只允许前者进入 Session 与 MCP manager；
- 主配置目录缺失两份文件、诱饵仍存在时，必须使用内置提示词且不调用 MCP manager；
- 两个显式绝对路径必须逐字保持；
- 恢复旧的全局搜索会使三个参数场景全部转红。
