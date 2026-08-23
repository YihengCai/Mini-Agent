# 主流开源 coding agent 的文件工具边界

调查日期：2026-08-24。这里只记录源码事实和本轮借鉴，不把外部项目的实现等同于本项目结论。

| 项目 | 源码事实 | 本轮取舍 |
|---|---|---|
| [Pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/tools/truncate.ts) | 工具输出共用 2000 行与 50 KiB 上限，截断保留完整行；[`read_file`](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/tools/read.ts) 在截断结果中告诉模型怎样继续读取。 | 借用“行数 + UTF-8 字节数 + 完整行 + 续读提示”的 contract；没有把 50 KiB 解释成精确 token 数。链接指向调查日的 `main`。 |
| [Gemini CLI read](https://github.com/google-gemini/gemini-cli/blob/5411f113cafae26161b4969b0237b8e1e024e2c2/packages/core/src/tools/read-file.ts#L49-L67) / [replace](https://github.com/google-gemini/gemini-cli/blob/5411f113cafae26161b4969b0237b8e1e024e2c2/packages/core/src/tools/edit.ts#L304-L387) | 读取范围是 1-based；替换默认拒绝多处匹配，但实现还会尝试空白和模糊匹配，并提供 `allow_multiple`；[写入路径](https://github.com/google-gemini/gemini-cli/blob/5411f113cafae26161b4969b0237b8e1e024e2c2/packages/core/src/tools/edit.ts#L889-L945)会保留已有 CRLF 约定。 | 借用 1-based 范围、歧义拒绝和 CRLF 处理；没有借用模糊匹配或批量替换，因为当前没有预览、恢复与错误修改指标。 |
| [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk/blob/c20709fb587f71d38d4af62c4813ff4d2681fa02/openhands-tools/openhands/tools/file_editor/editor.py#L178-L255) | 替换前检查目标是否唯一；写入使用目标目录中的临时文件再 `os.replace()`，并保留已有权限位（[写入实现](https://github.com/OpenHands/software-agent-sdk/blob/c20709fb587f71d38d4af62c4813ff4d2681fa02/openhands-tools/openhands/tools/file_editor/editor.py#L468-L531)）。 | 借用严格唯一和同文件系统原子替换；提交后不再执行可能让成功变失败的清理步骤。 |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/tools/edit_anthropic/bin/str_replace_editor#L516-L551) | `str_replace` 同样要求唯一文本，但随后直接写回文件（[写入实现](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/tools/edit_anthropic/bin/str_replace_editor#L645-L672)）。 | 借用唯一性，不借用直接覆写。 |
| [Codex](https://github.com/openai/codex/blob/aec653daa9873bf44517a623fd033722737817a8e/codex-rs/apply-patch/src/seek_sequence.rs#L39-L114) | `apply_patch` 逐级放宽上下文匹配；路径能否写入由独立的沙箱/可写根策略处理（[策略实现](https://github.com/openai/codex/blob/aec653daa9873bf44517a623fd033722737817a8e/codex-rs/core/src/safety.rs#L26-L98)）。 | 没有复制模糊补丁；确认“编辑语义”和“访问强制”是两层。本轮只改前者，不宣称 `workspace_dir` 是安全边界。 |

## 本轮得到的共同点

主流实现并不存在一个统一的文件工具答案，但反复出现三条边界：模型读取必须有显式预算和继续方式；小范围替换至少要在写入前处理歧义；路径权限与编辑算法应由不同层负责。本项目只落地了前两条和单文件原子可见替换，权限、并发版本和跨文件恢复仍留在 `docs/BUILD_LIST.md`。
