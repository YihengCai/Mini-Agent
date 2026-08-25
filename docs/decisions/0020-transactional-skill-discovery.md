# ADR-0020：Skill 发现以完整快照发布并拒绝重名

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/tools/skill_loader.py`、`tests/test_skill_loader.py`、提交 `9c15477`

## 背景

`SkillLoader` 长期持有以名称索引的 `loaded_skills`，`GetSkillTool` 与系统提示词元数据都从该注册表读取（`mini_agent/tools/skill_loader.py:47-58,225-265`；`mini_agent/tools/skill_tool.py:13-57`）。旧 `discover_skills()` 却在递归扫描期间直接向该注册表赋值，既不清除上次结果，也不检查同名来源（`git show 9c15477^:mini_agent/tools/skill_loader.py | nl -ba | sed -n '194,214p'`）。

改动前离线探针实测：两个都声明 `collision` 的文件会返回 2 个发现结果，但注册表只剩后扫描内容；删除两份文件后再次发现返回 0，`list_skills()` 仍返回 `['collision']`。因此返回列表、模型可见元数据和实际可调用内容可以描述三种不同状态。

## 选项

1. **扫描前清空原注册表，再逐项填入**：能删除陈旧条目，但中途失败会发布空或部分状态。
2. **排序后选择第一个或最后一个重名来源**：结果可复现，却暗中建立了目录优先级，调用者仍看不到配置冲突。
3. **在局部注册表完成稳定扫描，拒绝重名，成功后一次替换**：发现结果与已发布注册表一一对应，失败不会破坏上一完整快照。

## 决定

采用选项 3。`discover_skills()` 按路径排序全部 `SKILL.md`，把成功解析的条目加入局部列表和名称索引；同名时抛出包含名称和两个来源路径的 `ValueError`。只有完整扫描没有冲突后才替换 `loaded_skills`（`mini_agent/tools/skill_loader.py:194-223`）。

目录不存在或当前没有有效文件表示一次成功的空发现，因此会发布空注册表；现有 `load_skill()` 对单个无效文件打印诊断并返回 `None` 的行为不变（`mini_agent/tools/skill_loader.py:60-117,204-223`）。本轮不改变 YAML frontmatter（文件头元数据）的现有最小校验、内容路径改写、`allowed-tools` 语义、CLI 组装、信任级别或权限。

## 为什么否决其他的

**否决先清空再填入**：注册表是提示词和工具查询共享的能力快照，半次扫描不应成为可观察状态。若安全模型要求任何刷新错误都立即撤销全部旧能力，而且调用者会显式处理空状态，失败时清空反而应成为 contract。

**否决固定目录优先级**：当前只有一个递归目录，没有“项目级覆盖用户级”等来源层次；文件名排序不应偷偷获得覆盖权。若未来引入多个明确命名的来源层，并定义可见的优先级和覆盖诊断，选择确定来源才可能是正确方案。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_skill_loader.py tests/test_skill_tool.py tests/test_markdown_links.py` 实测 `16 passed in 0.30s`。
- 回归覆盖文件删除后的重扫替换、重名诊断中的稳定路径顺序，以及失败扫描保留上一完整注册表（`tests/test_skill_loader.py:115-157`）。
- 临时把快照替换退化为 `dict.update()` 时删除回归 1 项转红；临时移除重名守卫时冲突回归 1 项转红，分别耗时 `0.33s` 和 `0.32s`。
- 显式排除 `external` 的完整集合实测 `309 passed, 9 deselected in 13.21s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

实现只重排发现阶段的状态所有权，没有改写单项 Skill 内容。确定排序用于稳定结果和诊断，不构成覆盖优先级；注册表现在代表最近一次成功的完整扫描，但仍不是动态文件监视器或安全撤权机制。
