# 运行时工作区的单一所有权

> 状态：已实现。配置字段与迁移边界位于 `mini_agent/config.py:53-57,128-156`，CLI 选择与传递位于 `mini_agent/cli.py:651-714,968-980`，离线回归位于 `tests/test_llm_adapters.py:157-160,231-243`；取舍见 [ADR-0024](../decisions/0024-cli-owns-runtime-workspace.md)。

## 问题证据

上游从 YAML 读取 `workspace_dir` 并存入 `AgentConfig`，但 CLI 另行从 `--workspace` 或当前目录取得运行时路径，工具与 agent loop 只消费后者（`git show 953b943:mini_agent/config.py | nl -ba | sed -n '31,38p;131,136p'`；`git show 953b943:mini_agent/cli.py | nl -ba | sed -n '822,834p'`）。修改配置字段会成功解析，却不能改变运行时工作区。

## 本轮不变量

1. CLI 的显式 `--workspace` 是最高优先级；未提供时使用当前目录。
2. CLI 选出的同一路径传给工作区工具和 `AgentSession`，配置对象不持有第二份候选值。
3. `AgentConfig` 程序化构造拒绝 `workspace_dir`。
4. YAML 中的旧字段在通用未知字段检查前失败，并明确指向 `--workspace`。
5. 示例配置只列出真实生效的 agent 字段。

## 不在范围

不改变 `~` 展开、绝对路径转换、目录创建、默认当前目录、Session 工作区事实块、文件工具越界能力、权限或沙箱；不让 `skills_dir` 或配置伴随文件参与运行时工作区选择。

## 离线验证

- 缺省与全显式配置的 `AgentConfig.model_dump()` 都不含工作区字段；
- 程序化构造旧字段失败，旧 YAML 失败文本同时包含字段名和 `--workspace`；
- 删除定向迁移错误时，对应 YAML 回归转红；
- 配置来源、agent loop 与完整离线集合保持通过。
