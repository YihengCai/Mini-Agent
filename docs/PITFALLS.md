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
