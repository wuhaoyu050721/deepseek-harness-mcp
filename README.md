# DeepSeek Harness MCP

让 **Codex / Claude Code 调用本机 DeepSeek Harness**，完成代码审查或在独立副本中修改代码。主 AI 负责理解需求、设计方案、复核差异和合入验证，DeepSeek 按明确任务书执行。

当前版本 **0.3.3**，默认使用 Windows 桌面后端，直接复用已安装的 Harness runtime，无须 Linux 或 Docker。MCP 使用本机 stdio；教程中的 `--shared` 让多个客户端会话连接同一个本机后台。一个 Codex 或 Claude Code 聊天可以建立多个独立 DeepSeek 子会话并行工作，多个项目窗口也可以分别建立自己的子会话。每轮任务创建独立 Harness home 和代码副本，不接管已打开的桌面聊天。

[中文连接教程](docs/MCP-CONNECTION.zh-CN.md) · [下载发布包](https://github.com/wuhaoyu050721/deepseek-harness-mcp/releases) · [版本说明](docs/RELEASE-NOTES.md) · [验证范围](docs/VALIDATION.md)

## 可以做什么

- **代码审查**：读取本机配置明确授权的 Git 文件，返回带路径和引用的报告。
- **副本编码**：项目开启 `allow_edits=true` 后使用 `mode="edit"`，返回实际文件差异、完整修改后内容和前后 SHA256。原项目由主 AI 复核后合入。
- **异步任务**：提交立即返回编号，支持查询、分页读取、取消和相同请求编号恢复。
- **完成后继续处理**：启用可信 `completion.auto_continue` 后，可使用 Codex 桌面原对话通知，或显式启用 Claude Code 的实验 channel。主 AI 收到固定完成事件后读取结果、复核和继续工作。机制绑定、通知写出和处理确认分别记录，不把模型报告作为唤醒指令。
- **多会话连接**：使用同一可信配置和数据目录的共享连接共用后台调度、等待队列和历史；任一已授权会话都能查询或取消同一任务。关闭一个会话不会关闭其他连接，也不会取消后台任务。
- **独立子会话**：每个 `session_id` 固定项目、模式和文件范围，保存各自上下文。不同子会话默认最多三个任务并发，同一子会话每次只允许一轮未完成工作。
- **固定权限与模型**：项目、文件范围、模型、接口地址和凭证引用由本机 TOML 决定，工具参数不能扩大授权。
- **配套 Skill**：[deepseek-delegate](.agents/skills/deepseek-delegate/SKILL.md) 定义任务书、幂等恢复、差异核验与合入流程，可独立安装到 Codex。

## 快速安装

准备 Windows、已安装并配置固定 API key 的 DeepSeek Harness 桌面端、Git、uv、Python 3.11–3.14，以及 Codex 或 Claude Code。Git 必须支持 `--no-lazy-fetch`。首次依赖安装需要联网。

从 Releases 下载所需版本的 Windows 源码 ZIP，将其中目录放到 `D:/Tools/deepseek-harness-mcp`，或者克隆仓库到该位置。多会话需要 0.3.2 或更新版本；完成通知和可信默认编辑模式需要 0.3.3。Codex 原生空闲唤醒、自动结果读取和完成工具入口，Claude 通道传输已经验收；具体范围及尚未验证的 Claude 应用内续跑见 [验证范围](docs/VALIDATION.md)。在 PowerShell 中执行：

```powershell
Set-Location 'D:\Tools\deepseek-harness-mcp'
uv sync --locked --python 3.14 --no-install-package deepseek-harness-runtime-bin
.\.venv\Scripts\python.exe scripts\setup_local.py
.\.venv\Scripts\python.exe -m harness_mcp.doctor --config config\codex.local.toml --require-execution
```

初始化会创建人工 `sample` Git 项目、本机模型与授权配置、Codex/Claude 配置片段；已有文件不覆盖。ZIP 安装先仅授权 `sample`；新 desktop 配置默认 `default_mode="edit"`、授权项目的 `allow_edits=true` 和 `completion.auto_continue=true`。旧配置未设置这些字段时保持默认审查、关闭自动续跑，升级需明确调整。桌面安装位置不同、模型或接口不同时，请先修改 `config/codex.local.toml`。默认读取 Harness 的 `refs.DEEPSEEK_API_KEY`，也支持环境变量引用；不要将密钥写进仓库。

检查前置条件后连接 Codex：

```powershell
codex mcp add deepseek_harness -- 'D:/Tools/deepseek-harness-mcp/.venv/Scripts/python.exe' -m harness_mcp.server --shared --config 'D:/Tools/deepseek-harness-mcp/config/codex.local.toml'
codex mcp list
```

也可把生成的 `examples/codex.local.toml` 合并到个人 Codex 配置，确认参数含 `--shared`，`cwd` 指向安装目录，设置 `startup_timeout_sec = 30` 和 `env_vars=["CODEX_APP_TOOLS_PIPE_PATH"]`。此白名单透传宿主动态句柄，不要硬编码 pipe 路径。初始化不会改写已经存在的本机片段；从旧版本升级需要检查这些字段。保存后重新连接 MCP 或重启客户端：共享模式原有七工具，启用自动续跑后再增加三个完成事件工具。工具可见不代表当前客户端提供了自动唤醒能力，须核对回执中的 `auto_continue`。详细配置、Claude Code 接入、项目授权与故障处理见 [完整连接教程](docs/MCP-CONNECTION.zh-CN.md)。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `harness_submit` | 提交任务：`project_id`、`prompt`、`client_request_id`；可选范围、时限和 `mode` |
| `harness_status` | 查询状态、阶段、耗时和安全错误 |
| `harness_result` | 读取报告与修改内容，支持游标分页 |
| `harness_cancel` | 请求取消，随后查询是否确认停止 |
| `harness_session_open` | 共享模式：登记固定项目、模式和范围的独立子会话，返回 `session_id` |
| `harness_session_send` | 共享模式：向已有子会话提交下一轮任务，返回 `job_id` |
| `harness_session_list` | 共享模式：按项目查看子会话、标题及近期任务 |
| `harness_watch` | 自动续跑开启时：为已接纳或已完成的 `job_ids` 绑定原对话通知，不重新执行模型 |
| `harness_completion_list` | 自动续跑开启时：查看当前主对话的完成事件，可按状态筛选 |
| `harness_completion_ack` | 自动续跑开启时：完整读取结果并完成后续处理后，确认已处理的 `event_ids` |

新任务使用唯一 `client_request_id`；响应不明时用原编号和完全相同参数恢复。共享连接中的编号属于同一任务历史，跨会话使用相同编号和参数会返回原任务。回执 `auto_continue=true` 表示作业已绑定当前客户端的完成机制，不表示宿主已接收通知或主 AI 已处理结果。没有绑定时按建议间隔查询，并查看 `auto_continue_reason`；Codex 宿主缺少通知能力时的只读预检不会阻止普通模型任务。停止查询不等于取消，`cancelling` 不等于已停止。

Codex 通知目标来自宿主 thread/turn 元数据和本机正规 app-tools 通道；只读预检确认能力和原对话归属后绑定。任务提示不能指定聊天、网址或命令。通知仅含固定作业与事件标识，模型报告仍通过 `harness_result` 读取。事件 `delivered` 要结合 `delivery_status` 判断：Codex 的 `accepted` 表示宿主接受，Claude 的 `channel_written` 仅表示前端写出了消息。处理完结果后才调用 `harness_completion_ack`。若写后连接中断、结果不明，事件记为 `uncertain`，不会盲目再次发送。失败、取消或超时不能自动重跑模型或合入部分修改。

普通 Claude 连接支持审查、编辑与子代理工具，工具数量取决于 completion 开关（共享模式七或十个），完成机制缺少上下文时回执为 false。Claude 实验推送需服务启动参数 `--claude-channel`、客户端 legacy 协议以及交互启动 `--dangerously-load-development-channels server:deepseek_harness`；详细步骤见 [Claude channel 接入](docs/MCP-CONNECTION.zh-CN.md)。每个 stdio 前端使用私有连接标识区分窗口，进程重启会产生新身份，已有绑定不能用 watch 改投新会话；保留原 job_id 仍可读取结果继续处理。

`--shared` 保持一个后台持有数据目录的实例锁。最后一个前端断开后，有等待或运行任务时后台继续工作；无连接且无任务时，默认等待 30 秒后安全退出。不带 `--shared` 的直接模式仍只允许一个服务使用同一数据目录。升级、修改授权或切换到共享模式时，请先完成或取消任务、关闭旧连接，再重新连接；不要删除锁或数据库，也不要更换数据目录绕过未确认停止的执行。

需要多个 DeepSeek 子代理时，分别用 `harness_session_open` 建立会话，例如同一项目的接口审查和错误处理审查各用一个 `session_id`，再用 `harness_session_send` 提交各自任务。多个项目窗口则各建一个绑定本项目的会话；窗口重连后通过 `harness_session_list` 恢复原会话。请求编号在共享后台全局唯一，建议加项目、窗口和子会话前缀。

子会话的继续上下文从该会话的已脱敏历史报告重建，最多最近四轮、16,000 字；每轮 runtime 和工作副本仍独立。它不是桌面 UI 中常驻的聊天，前一轮修改也不会自动进入下一轮或其他子代理副本。编辑产物仍由主 AI 核验、合入，再发送需要基于新源码的任务。调度和示例见 [连接教程](docs/MCP-CONNECTION.zh-CN.md#独立-deepseek-子会话与并行任务)。

首次审查可以对 Codex 说：

> 使用 DeepSeek Harness MCP，只读审查 sample 项目的 calculator.py，关注空列表和负数金额边界。保存本次请求编号和任务编号，不运行命令、不修改文件；完成后读取报告并复核结论，状态不明时恢复原任务。

这会调用 DeepSeek 模型接口，可能产生 API 费用。doctor 和工具发现本身不调用模型。

## 授权自己的项目与编码

在 `config/codex.local.toml` 中增加本机 Git 项目，再让使用该配置的连接完成后台切换：

```toml
[projects.my_project]
path = "D:/Work/my-project"
allowed_paths = ["src", "tests", "README.md"]
exclude_patterns = []
allow_untracked = []
allow_edits = true
```

默认只取已保存的 Git tracked 文件。未跟踪文件需要在 `allow_untracked` 中逐文件授权；读取范围也限制可修改范围。项目允许副本编辑后，单次只审查可以指定 `mode="review"`，无需反复更改权限或重载；长期禁止某项目编辑才使用 `allow_edits=false`。共享后台固定启动时的配置，配置或服务代码变化后拒绝复用旧后台。请先完成或取消旧任务，关闭使用旧配置的全部连接，等待后台空闲退出，再重新连接。

模式的真实 schema 默认值取自可信根配置 `default_mode`。新 desktop 配置为 `edit`，旧配置仍为 `review`；编码也可显式指定 `mode="edit"`，只读则指定 `mode="review"`。Docker 当前仅支持审查，不能设置编辑默认。成功结果返回 `edit_ready=true`、`source_applied=false`、实际差异与内容摘要。主 AI 读取完整分页、检查路径和行为、核对当前源文件匹配 `before_sha256` 后才能合入，再运行必要验证。模型自述的改动或测试不能代替真实检查。

Windows 桌面路线使用独立任务目录与受监管进程，**没有操作系统级文件或网络沙箱**。只授权任务需要的文件；代码可能发往所配置的模型服务商。固定提示和目录隔离不能限制当前 Windows 用户权限。可选 Docker 审查后端保留，但未完成实际容器验收，也不支持编辑模式。

服务使用 TOML 中指定的模型、地址和凭证引用，不自动继承桌面聊天的当前模型选择。每任务独立会话，不复制个人 home/profile 或聊天。凭证、任务数据、个人配置和原始联调报告不包含在发布包中。

## 发布文件

| 文件 | 用途 |
| --- | --- |
| `deepseek-harness-mcp-<版本>-windows-source.zip` | 推荐安装包：源码、锁文件、脚本、教程、测试与 Skill |
| `deepseek_harness_mcp-<版本>-py3-none-any.whl` | Python 包；依赖、配置与桌面 runtime 需另行准备 |
| `deepseek-delegate-<版本>.zip` | 独立 Codex Skill，包含引用文档 |
| `SHA256SUMS` / `manifest-v<版本>.json` | 文件摘要、来源成员清单与完整性核对 |

发布包不内置 Python、虚拟环境或 DeepSeek 桌面程序，不是独立运行的 EXE。可使用 `Get-FileHash -Algorithm SHA256` 核对下载文件。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_mvp.py --output artifacts\mvp-protocol.local.json
uv build --wheel --out-dir artifacts/dist
.\.venv\Scripts\python.exe scripts\package_release.py
```

前两项验证本地契约和协议，不调用模型；`verify_mvp.py` 原有直接模式检查不能替代多会话验收。0.3.3 已通过761项测试和57个子测试、源码包全新安装、三前端 Claude 通道传输隔离，以及既有报告的 Codex 原生空闲唤醒和实际自动读取。首次本机确认后，又通过原对话实际 MCP 来源捕获、watch 和幂等 ack 验证完成工具入口，没有重新提交模型。历史 0.3.2 的两个真实 DeepSeek 审查任务并行及同会话追问、0.3.1 的真实副本编辑另保留原范围。完整验证范围和未验证平台见 [验证说明](docs/VALIDATION.md) 和 [0.3.3机器摘要](artifacts/public/validation-v0.3.3.json)。

实现结构见 [IMPLEMENTATION.md](docs/IMPLEMENTATION.md)。尚未指定开源许可证；第三方依赖和 DeepSeek 桌面程序遵循各自的授权条款。
