# DeepSeek Harness MCP

让 **Codex / Claude Code 调用本机 DeepSeek Harness**，完成代码审查或在独立副本中修改代码。主 AI 负责理解需求、设计方案、复核差异和合入验证，DeepSeek 按明确任务书执行。

当前版本 **0.3.1**，默认使用 Windows 桌面后端，直接复用已安装的 Harness runtime，无须 Linux 或 Docker。MCP 使用本机 stdio，由客户端启动服务；每次任务创建独立会话和代码快照，不接管已打开的桌面聊天。

[中文连接教程](docs/MCP-CONNECTION.zh-CN.md) · [下载发布包](https://github.com/wuhaoyu050721/deepseek-harness-mcp/releases/tag/v0.3.1) · [版本说明](docs/RELEASE-NOTES.md) · [验证范围](docs/VALIDATION.md)

## 可以做什么

- **代码审查**：读取本机配置明确授权的 Git 文件，返回带路径和引用的报告。
- **副本编码**：项目开启 `allow_edits=true` 后使用 `mode="edit"`，返回实际文件差异、完整修改后内容和前后 SHA256。原项目由主 AI 复核后合入。
- **异步任务**：提交立即返回编号，支持查询、分页读取、取消、相同请求编号恢复，每实例一个执行位置。
- **固定权限与模型**：项目、文件范围、模型、接口地址和凭证引用由本机 TOML 决定，工具参数不能扩大授权。
- **配套 Skill**：[deepseek-delegate](.agents/skills/deepseek-delegate/SKILL.md) 定义任务书、幂等恢复、差异核验与合入流程，可独立安装到 Codex。

## 快速安装

准备 Windows、已安装并配置固定 API key 的 DeepSeek Harness 桌面端、Git、uv、Python 3.11–3.14，以及 Codex 或 Claude Code。Git 必须支持 `--no-lazy-fetch`。首次依赖安装需要联网。

下载 Releases 的 `deepseek-harness-mcp-0.3.1-windows-source.zip`，将其中目录放到 `D:/Tools/deepseek-harness-mcp`，或者克隆仓库到该位置。在 PowerShell 中执行：

```powershell
Set-Location 'D:\Tools\deepseek-harness-mcp'
uv sync --locked --python 3.14 --no-install-package deepseek-harness-runtime-bin
.\.venv\Scripts\python.exe scripts\setup_local.py
.\.venv\Scripts\python.exe -m harness_mcp.doctor --config config\codex.local.toml --require-execution
```

初始化会创建人工 `sample` Git 项目、本机模型与授权配置、Codex/Claude 配置片段；已有文件不覆盖。ZIP 安装先仅授权 `sample`。桌面安装位置不同、模型或接口不同时，请先修改 `config/codex.local.toml`。默认读取 Harness 的 `refs.DEEPSEEK_API_KEY`，也支持环境变量引用；不要将密钥写进仓库。

检查前置条件后连接 Codex：

```powershell
codex mcp add deepseek_harness -- 'D:/Tools/deepseek-harness-mcp/.venv/Scripts/python.exe' -m harness_mcp.server --config 'D:/Tools/deepseek-harness-mcp/config/codex.local.toml'
codex mcp list
```

也可把生成的 `examples/codex.local.toml` 合并到个人 Codex 配置。保存后重新连接 MCP 或重启客户端，确认四个工具已加载。详细配置、Claude Code 接入、项目授权与故障处理见 [完整连接教程](docs/MCP-CONNECTION.zh-CN.md)。

## 四个 MCP 工具

| 工具 | 用途 |
| --- | --- |
| `harness_submit` | 提交任务：`project_id`、`prompt`、`client_request_id`；可选范围、时限和 `mode` |
| `harness_status` | 查询状态、阶段、耗时和安全错误 |
| `harness_result` | 读取报告与修改内容，支持游标分页 |
| `harness_cancel` | 请求取消，随后查询是否确认停止 |

新任务使用唯一 `client_request_id`；响应不明时用原编号和完全相同参数恢复。收到提交回执后按建议间隔查询同一 `job_id`。停止查询不等于取消，`cancelling` 不等于已停止。

首次审查可以对 Codex 说：

> 使用 DeepSeek Harness MCP，只读审查 sample 项目的 calculator.py，关注空列表和负数金额边界。保存本次请求编号和任务编号，不运行命令、不修改文件；完成后读取报告并复核结论，状态不明时恢复原任务。

这会调用 DeepSeek 模型接口，可能产生 API 费用。doctor 和工具发现本身不调用模型。

## 授权自己的项目与编码

在 `config/codex.local.toml` 中增加本机 Git 项目，再重新连接服务：

```toml
[projects.my_project]
path = "D:/Work/my-project"
allowed_paths = ["src", "tests", "README.md"]
exclude_patterns = []
allow_untracked = []
allow_edits = true
```

默认只取已保存的 Git tracked 文件。未跟踪文件需要在 `allow_untracked` 中逐文件授权；读取范围也限制可修改范围。只审查时使用 `allow_edits=false`。

编辑必须明确使用 `mode="edit"`。成功结果返回 `edit_ready=true`、`source_applied=false`、实际差异与内容摘要。主 AI 读取完整分页、检查路径和行为、核对当前源文件匹配 `before_sha256` 后才能合入，再运行必要验证。模型自述的改动或测试不能代替真实检查。

Windows 桌面路线使用独立任务目录与受监管进程，**没有操作系统级文件或网络沙箱**。只授权任务需要的文件；代码可能发往所配置的模型服务商。固定提示和目录隔离不能限制当前 Windows 用户权限。可选 Docker 审查后端保留，但未完成实际容器验收，也不支持编辑模式。

服务使用 TOML 中指定的模型、地址和凭证引用，不自动继承桌面聊天的当前模型选择。每任务独立会话，不复制个人 home/profile 或聊天。凭证、任务数据、个人配置和原始联调报告不包含在发布包中。

## 发布文件

| 文件 | 用途 |
| --- | --- |
| `deepseek-harness-mcp-0.3.1-windows-source.zip` | 推荐安装包：源码、锁文件、脚本、教程、测试与 Skill |
| `deepseek_harness_mcp-0.3.1-py3-none-any.whl` | Python 包；依赖、配置与桌面 runtime 需另行准备 |
| `deepseek-delegate-0.3.1.zip` | 独立 Codex Skill，包含引用文档 |
| `SHA256SUMS` / `manifest-v0.3.1.json` | 文件摘要、来源成员清单与完整性核对 |

发布包不内置 Python、虚拟环境或 DeepSeek 桌面程序，不是独立运行的 EXE。可使用 `Get-FileHash -Algorithm SHA256` 核对下载文件。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_mvp.py --output artifacts\mvp-protocol.local.json
uv build --wheel --out-dir artifacts/dist
.\.venv\Scripts\python.exe scripts\package_release.py
```

前两项验证本地契约和协议，不调用模型。真实模型验收需要单独授权的人工项目与本机配置。0.3.1 已完成 Windows 回归、Codex/Claude 工具发现、Codex 到桌面 runtime 的真实副本编辑验收；该真实样例只核验 AST、差异和摘要，没有执行或合入生成代码。详细结果与尚未验证的平台/流程见 [验证说明](docs/VALIDATION.md) 和 [机器可读摘要](artifacts/public/validation-v0.3.1.json)。

实现结构见 [IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。尚未指定开源许可证；第三方依赖和 DeepSeek 桌面程序遵循各自的授权条款。
