# ADR-0017：配置与运行时共同拒绝负重试次数

- 日期：2026-08-25
- 状态：已采纳
- 关联：`mini_agent/config.py`、`mini_agent/retry.py`、`tests/test_llm_adapters.py`、`tests/test_retry.py`、提交 `08c9f20`

## 背景

文件配置模型与运行时 `RetryConfig` 都接受负 `max_retries`；装饰器使用 `range(max_retries + 1)`，所以 `-1` 不执行被包装函数，直接落入理论上的不可达兜底（`git show 08c9f20^:mini_agent/config.py | nl -ba | sed -n '32,50p'`；`git show 08c9f20^:mini_agent/retry.py | nl -ba | sed -n '23,61p;97,138p'`）。

改动前离线探针用 `max_retries=-1` 包装一个始终失败的模型调用替身，实测输出为 `calls=0`、`error_type='Exception'`、`error='Unknown error'`。这既没有保留原始模型错误，也把“允许的附加重试次数”变成了“是否执行首次调用”的隐式开关。

## 选项

1. **只约束 YAML 配置**：CLI 能尽早失败，但两个 adapter 公开接收的运行时配置仍可程序化绕过。
2. **只约束运行时配置**：所有模型调用最终安全，但无效 YAML 会通过配置加载并延后暴露。
3. **配置入口与运行时入口各自验证**：文件加载时给出字段错误，公开运行时对象独立维持相同不变量。

## 决定

采用选项 3。Pydantic 配置字段与运行时构造函数都要求 `max_retries >= 0`（`mini_agent/config.py:32-39`；`mini_agent/retry.py:23-51`）。`0` 表示执行一次首次调用、不执行附加重试；正数仍表示首次调用之外的附加次数。

非负不变量保证重试循环至少进入一次，因此删除只为零次循环服务的 `last_exception` 状态与 `Unknown error` 尾分支（`mini_agent/retry.py:99-131`）。本轮不约束延迟或底数，不改变 `enabled` 开关所有权、回调时机、可重试异常集合、`RetryExhaustedError`、adapter 或 SDK 重试关闭策略。

## 为什么否决其他的

**否决只约束 YAML**：`RetryConfig` 是两个 adapter 的公开程序化输入，测试和未来非 CLI 宿主都能直接构造；只信任文件加载器会保留零调用路径。若所有运行时对象只能由一个私有、已验证工厂创建，配置单层约束反而足够。

**否决只约束运行时**：它能阻止零调用，却让配置文件表面加载成功，错误要等到 CLI 组装模型客户端时才出现。若重试次数只能由运行时策略动态计算、配置文件不直接表达它，运行时单层校验反而是唯一真源。

## 怎么验证它是对的

- `.venv/bin/python -m pytest -q -m 'not external' tests/test_retry.py tests/test_llm_adapters.py` 实测 `38 passed in 0.32s`。
- YAML 与运行时各自拒绝 `-1`；`0` 与 `2` 分别产生 `1` 与 `3` 次调用，耗尽错误保留相同的最后异常对象（`tests/test_llm_adapters.py:255-271`；`tests/test_retry.py:8-41`）。
- 临时移除 Pydantic 约束与运行时守卫时，对应回归分别实测 `1 failed in 0.41s` 和 `1 failed in 0.59s`。
- 显式排除 `external` 的完整集合实测 `279 passed, 9 deselected in 13.42s`；真实模型、用户 MCP 配置和网络测试本次未运行。

## 回头看

最终实现新增两行运行时守卫、收紧一个配置字段，并净删零次循环专用状态和通用错误分支。测试把配置错误位置与实际调用次数分开锁定，没有借本项改变尚未验证的退避数值或开关语义。
