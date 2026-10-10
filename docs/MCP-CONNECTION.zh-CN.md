# Windows 上连接 DeepSeek Harness MCP

本教程针对 0.3.2 的 Windows `desktop` 路线和 `--shared` 多会话连接。完成后，Codex 或 Claude Code 可以提交 DeepSeek 代码审查任务；按项目开启编辑权限后，也可以让 DeepSeek 修改任务副本，再由主 AI 复核、合入和验证。下载前请确认所选发布包包含 0.3.2，共享功能不在 0.3.1 中。

这个 MCP 通过本机 **stdio** 连接，由每个客户端会话启动自己的 Python 前端。教程中的 `--shared` 使这些前端复用一个本机后台，共享任务队列、请求编号和历史。客户端仍配置程序路径和参数，无须填写 HTTP 网址。前端与后台使用经过双向认证的本机回环连接。服务使用已安装的 DeepSeek Harness 桌面运行时；模型、接口地址和凭证引用由本项目的本机 TOML 配置指定，不会自动继承桌面聊天窗口当前选择的模型。

以下统一使用 `D:/Tools/deepseek-harness-mcp`。安装到其他位置时，请同时修改服务配置和客户端配置中的路径。源码包不含 Python 环境、桌面程序、个人凭证、任务快照或历史报告，首次安装依赖需要联网。

## 1. 准备环境

需要：

- Windows 10 / Windows Server 2016 或更新版本。编辑模式目前仅支持 Windows `desktop` 后端。
- 已安装且文件完整的 DeepSeek Harness 桌面端。
- 已配置能调用所选模型的固定 API key 引用，默认是个人 Harness 凭证库中的 `refs.DEEPSEEK_API_KEY`。仅登录桌面账号得到的 grant 不能替代这个 API key。
- Git、uv，以及 Python 3.11–3.14。依赖使用仓库内的 `uv.lock`。
- 要接入的 Codex，或者 Claude Code。

如果没有 uv，可按 [uv 官方安装文档](https://docs.astral.sh/uv/getting-started/installation/) 安装。在新 PowerShell 窗口中检查：

```powershell
git --version
git --no-lazy-fetch --version
uv --version
```

第二条命令必须成功，项目快照需要 Git 支持 `--no-lazy-fetch`。若 Git 报不认识该参数，请更新 Git；本服务不会降级为可能触发仓库程序或远端获取的快照方式。

Python 版本可在安装环境时明确选择，例如下一节的 `--python 3.14`。uv 会使用或获取兼容解释器；若本机网络限制下载安装，可以改为已安装解释器的路径。

## 2. 解压或克隆项目，安装依赖

推荐从本仓库的 Releases 下载 Windows 源码 ZIP，解压到 `D:/Tools/deepseek-harness-mcp`。也可以克隆本仓库到该目录。打开 PowerShell：

```powershell
Set-Location 'D:\Tools\deepseek-harness-mcp'
uv sync --locked --python 3.14 --no-install-package deepseek-harness-runtime-bin
.\.venv\Scripts\python.exe scripts\setup_local.py
```

`--no-install-package deepseek-harness-runtime-bin` 保留锁定的 SDK 依赖，同时使用已经安装的桌面 runtime。这里不需要安装 Linux 容器。若命令失败，请先解决安装错误，再继续配置客户端。

`setup_local.py` 会创建一个独立的人工 Git 示例项目，以及以下本机文件：

| 文件 | 用途 |
| --- | --- |
| `config/codex.local.toml` | Codex 对应的模型、项目授权和数据目录 |
| `config/claude.local.toml` | Claude Code 对应的模型、项目授权和默认独立数据目录 |
| `examples/codex.local.toml` | 按本机路径生成的 Codex 配置片段 |
| `examples/claude.local.json` | 按本机路径生成的 Claude 配置片段 |

脚本不会修改个人客户端配置，不会调用模型，也不会覆盖已经存在的上述本机文件。重新解压、升级或移动安装目录后，已有文件需要人工检查路径，重新运行脚本不会替你改写它们。从 0.3.1 升级时尤其要在客户端参数中加入 `--shared`。源码 ZIP 是否带 `.git` 不影响创建和使用人工示例项目：ZIP 安装会先只授权 `sample`，不用为安装目录运行 `git init`。自己的业务项目按第 6 节单独授权。

### 检查桌面 runtime、模型和凭证引用

打开 `config/codex.local.toml`，默认主要内容是：

```toml
[model]
provider = "deepseek-official"
model = "deepseek-flash"
base_url = "https://api.deepseek.com/anthropic"
credential_source = "desktop_store"
credential_env = "DEEPSEEK_API_KEY"

[executor]
backend = "desktop"
desktop_root = ""
```

默认自动检测 `%LOCALAPPDATA%/Programs/DeepSeek Harness`。如果桌面端安装在其他地方，将 `desktop_root` 改为安装目录的绝对路径，例如 `D:/Apps/DeepSeek Harness`，该目录下应有 `DeepSeek Harness.exe` 和 `resources`。

`desktop_store` 只读取上述固定凭证引用，不复制个人会话或整个凭证库。请在自己的 Harness 中配置对应 API key；不要把密钥值写进 TOML、客户端示例、任务提示或 Git 仓库。若使用环境变量，改为 `credential_source = "env"`，并确保启动 MCP 的客户端进程可以读取 `credential_env` 指定的变量。Codex 可使用后面配置中的 `env_vars` 传递该变量。

默认 API 根地址含 `/anthropic`，runtime 再追加 `/v1/messages`。如果使用其他服务商、网关或模型，请依据其实际 Messages 兼容接口配置 `provider`、`model` 和 `base_url`；不要仅因桌面聊天能够使用某个模型就认定这里的配置已经相同。

运行前置检查：

```powershell
.\scripts\doctor.ps1 -ConfigPath .\config\codex.local.toml -RequireExecution
```

如果 PowerShell 不允许执行 `.ps1`，可以直接使用等价的 Python 入口，无须修改系统执行策略：

```powershell
.\.venv\Scripts\python.exe -m harness_mcp.doctor --config .\config\codex.local.toml --require-execution
```

检查结果需要 `execution_ready = true` 且命令退出码为 0。它只确认安装、配置和凭证引用等前置条件，不调用模型，也不验证 API key 有效性、余额或模型响应。检查 Claude 配置时将文件名替换为 `claude.local.toml`。

## 3. 连接 Codex

### 方法 A：添加本机 stdio 服务

如果安装了 Codex CLI，在 PowerShell 运行：

```powershell
codex mcp add deepseek_harness -- 'D:/Tools/deepseek-harness-mcp/.venv/Scripts/python.exe' -m harness_mcp.server --shared --config 'D:/Tools/deepseek-harness-mcp/config/codex.local.toml'
codex mcp list
```

这会新增名为 `deepseek_harness` 的 MCP 配置。若已经有同名服务，请先检查现有内容，避免重复注册或替换其他任务使用的设置。这里只启动本机程序，因此不用 `--url`。注册后检查个人配置，将 `cwd` 设为安装目录、`startup_timeout_sec` 设为 30，字段位置见方法 B。CLI 添加参数本身不代替这两个设置。

### 方法 B：合并配置片段

也可以将 `examples/codex.local.toml` 的服务段落合并到自己的 Codex `config.toml`；默认位于 `$env:USERPROFILE/.codex/config.toml`。**只合并对应 MCP 段落，保留个人原有配置。** 通用示例为：

```toml
[mcp_servers.deepseek_harness]
command = "D:/Tools/deepseek-harness-mcp/.venv/Scripts/python.exe"
args = ["-m", "harness_mcp.server", "--shared", "--config", "D:/Tools/deepseek-harness-mcp/config/codex.local.toml"]
cwd = "D:/Tools/deepseek-harness-mcp"
startup_timeout_sec = 30
tool_timeout_sec = 30
# 仅在 credential_source="env" 时需要：
# env_vars = ["DEEPSEEK_API_KEY"]
```

安装包另有可直接参考的 [通用 Codex 配置](../examples/codex.mcp.example.toml)。stdio 配置和 CLI 注册方式参见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

保存后，在 Codex 中重新连接 MCP 服务；若当前窗口仍显示旧工具参数，重启 Codex 或开启加载了新连接的聊天。共享后台首次启动需要创建私有登记目录和独立进程，`startup_timeout_sec = 30` 为这一阶段预留时间。`tool_timeout_sec` 是单次工具调用的等待时间；后台模型任务的总时限由 `timeout_seconds` 和服务配置决定，两者不同。

共享模式连接成功后应能看到七个工具：原有 `harness_submit`、`harness_status`、`harness_result`、`harness_cancel`，以及 `harness_session_open`、`harness_session_send`、`harness_session_list`。客户端可能显示额外命名空间前缀。原有四个任务工具保持兼容；编辑模式还需要检查 `harness_submit` 的真实参数中存在 `mode`，并支持 `review` 和 `edit`。

## 4. 连接 Claude Code（可选）

初始化默认使用独立的 `claude.local.toml`，使 Codex 与 Claude 各自保存任务数据；每份配置内部仍可由多个会话共享。如果希望两个客户端也共同查询、取消同一任务，将 Claude 的 `--config` 路径指向与 Codex 相同的 `codex.local.toml`，并确认两端都使用 `--shared`。不要让不同授权配置争用一个 `data_dir`。以下命令读取 setup 生成的配置，注册为当前用户的 MCP：

```powershell
Set-Location 'D:\Tools\deepseek-harness-mcp'
$claudeMcpDocument = Get-Content .\examples\claude.local.json -Raw | ConvertFrom-Json
$claudeMcpServer = $claudeMcpDocument.mcpServers.deepseek_harness | ConvertTo-Json -Depth 10 -Compress
claude mcp add-json --scope user deepseek_harness $claudeMcpServer
claude mcp list
```

需要重启或重新连接 Claude Code，并完成客户端本身要求的 MCP 授权。命令格式可用 `claude mcp add-json --help` 核对；安装包内的 [通用 Claude 配置](../examples/claude.mcp.example.json) 也可用于手工合并。该文件外层是 `mcpServers`，CLI `add-json` 接受的是其中单个服务对象，不能把整个外层文档直接传给它。连接和作用域说明参见 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp)。

## 5. 先验证工具，再选择是否调用模型

工具发现成功只证明客户端能启动和连接 MCP。不要让客户端为“检查连接”自动提交模型任务。已安装 Codex CLI 时，可选运行不调用模型的握手探针：

```powershell
.\.venv\Scripts\python.exe scripts\verify_clients.py --client codex --output .\artifacts\client-discovery.local.json
```

探针使用临时客户端配置检查连接和任务工具，不修改个人注册配置。Claude Code 使用 `--client claude`；两者都已安装时可使用 `--client all`。原有四工具探针不能单独证明共享模式的三个会话工具或并发行为已通过验收，请同时核对真实工具列表及对应版本验证记录。

准备确认真实模型调用时，可以在 Codex 中发送下面这段话。**从这一轮开始会请求模型接口，并可能产生 API 费用。** 人工示例不含业务代码或密钥。

> 请使用 DeepSeek Harness MCP，审查已授权的 sample 项目，只看 calculator.py 的空列表和负数金额边界，不运行命令、不修改文件。先保存本次提交参数和唯一 client_request_id，收到回执保存 job_id，再查询同一任务。最后读取结果并由你复核结论；如果连接失败或状态不明，请恢复原任务，不要另起一个任务重试。

对应的工具参数形状如下，`client_request_id` 应换为本次唯一编号：

```json
{
  "project_id": "sample",
  "mode": "review",
  "relative_paths": ["calculator.py"],
  "timeout_seconds": 600,
  "client_request_id": "sample-review-unique-id",
  "prompt": "审查 calculator.py 的空列表和负数金额边界，不运行命令、不修改文件。"
}
```

提交回执不是完成结果。按返回的 `poll_after_seconds` 或更长间隔调用 `harness_status(job_id)`；结束后用 `harness_result(job_id)` 读取报告。`ready=false` 表示尚无报告。需要分页时保持相同 `page_size`，按该任务返回的 `next_cursor` 继续，不要跨任务复用游标。

### 独立 DeepSeek 子会话与并行任务

共享后台和 DeepSeek 子会话是两层关系：多个 Codex/Claude 连接复用一个后台，每个连接可以建立一个或多个 `session_id`。子会话固定 `project_id`、`mode` 和 `relative_paths`，各自保留上下文和任务编号；继续发送时不能临时切换项目或扩大范围。需要另一个项目、模式或范围时，建立新的子会话。

三个会话工具的参数如下：

| 工具 | 参数及返回 |
| --- | --- |
| `harness_session_open` | 必需 `project_id`、`client_request_id`；可选 `title=""`、`mode="review"`、`relative_paths=null`；返回 `session_id` |
| `harness_session_send` | 必需 `session_id`、`prompt`、`client_request_id`；可选 `timeout_seconds`；返回 `job_id` |
| `harness_session_list` | 可选 `project_id`、`limit=20`；返回项目、标题和近期任务，便于重连后查找原会话 |

例如先登记一个只读子会话，调用 `harness_session_open`：

```json
{
  "project_id": "sample",
  "client_request_id": "sample-window-a-boundary-open-unique-id",
  "title": "金额边界审查",
  "mode": "review",
  "relative_paths": ["calculator.py"]
}
```

保存返回的 `session_id`，再调用 `harness_session_send`；这一阶段会提交模型任务，可能产生 API 费用：

```json
{
  "session_id": "替换为返回的会话编号",
  "client_request_id": "sample-window-a-boundary-round-1-unique-id",
  "timeout_seconds": 600,
  "prompt": "审查 calculator.py 的空列表和负数金额边界，不运行命令、不修改文件。"
}
```

后续仍使用 `harness_status`、`harness_result` 和 `harness_cancel` 管理返回的 `job_id`。同一子会话有未完成任务时，新的下一轮发送返回 `SESSION_BUSY`；先查询并完成或确认取消原任务，再开始下一轮。不同子会话可以并发，全局默认最多三个运行任务、五个等待任务。可信配置可以调整运行容量：

```toml
[limits]
max_parallel_jobs = 3 # 允许 1 到 8
max_queue = 5
```

容量由所有连接共同使用，不因多开窗口重复获得。修改容量也需要按第 7 节切换后台配置。

一个 Codex 聊天中，可以对主 AI 说：

> 先确定接口审查与错误处理审查的任务范围，为这两项分别建立 DeepSeek 子会话，标题和请求编号区分清楚。各自只读审查已授权文件，可并行发送；保存两个 session_id 与各轮 job_id，独立读取和复核结果，不让一个子会话复用另一个的上下文。任务不明时恢复原编号。

多个项目窗口则分别建立绑定本窗口项目的子会话，后续各自向原 `session_id` 发送。子会话不是权限隔离账号：共享同一可信配置的连接都能访问该配置授权的会话和任务。需要限制某个客户端的项目访问时，使用更窄授权及独立数据目录。

每轮执行都创建独立 runtime home 和代码副本。继续上下文从该子会话的已脱敏历史报告重建，最多最近四轮、16,000 字；它不是复用桌面 UI 的常驻聊天。前一轮编辑、其他子代理产物不会自动进入下一轮副本。主 AI 先核验差异并合入源项目，再提交依赖这些修改的新任务；并行编辑也仍须逐份核对源文件哈希，处理冲突后验证。

`client_request_id` 在共享后台全局使用，请给登记和每轮发送分别使用唯一编号，建议带项目、窗口、子会话与轮次前缀。响应不明时用原编号和完全相同参数恢复，不根据模型自述新建另一个会话或任务。

## 6. 授权自己的 Git 项目

在所用客户端对应的 `config/*.local.toml` 增加一个项目。例如先只读审查：

```toml
[projects.my_project]
path = "D:/Work/my-project"
allowed_paths = ["src", "tests", "README.md"]
exclude_patterns = []
allow_untracked = []
allow_edits = false
```

`project_id` 使用 `my_project`；`path` 必须是该机绝对路径；`allowed_paths` 是项目内的相对文件或目录前缀。工具参数不能自行切换到任意本机目录。共享后台固定启动时的配置，修改授权后请先完成或取消现有任务，关闭使用该配置的全部连接，等待后台空闲退出，再重新连接。仅关闭一个窗口不能替换其他会话正在使用的配置。

源项目需要是 Git 工作树。默认只取已保存的 Git tracked 文件；编辑器中尚未保存的内容不会复制，未跟踪文件也不会自动进入。若确实需要纳入某个未跟踪文件，在可信本机配置中逐个添加，比如 `allow_untracked = ["src/new_feature.py"]`。目录授权不能代替未跟踪文件的逐个授权。

服务会排除常见凭证、私钥、依赖和缓存路径，但规则不能识别所有硬编码秘密。请只授权任务实际需要的范围；文件会复制到本机任务数据目录，并可能通过模型接口提交给服务商。

### 允许修改副本

你确认该项目可以参与编码委派后，将这一项目的 `allow_edits` 改为 `true`。按上一段完成共享后台切换，重新连接并核对实时 schema，然后显式使用 `mode="edit"`。配置中的读取范围也是允许修改的范围，新增文件需要授权其父目录。

编辑任务只修改独立副本，不直接改原项目。主 AI 应先确定实现方法、接口、边界和验收条件，再提交明确的一批任务；DeepSeek 在副本写代码，不运行项目测试、程序、安装、构建或 Git。模型完成后，主 AI 读取真实完整差异并验证后才能合入。

可对 Codex 这样说明：

> 先理解 my_project 中这次改动的需求，确定实现方法和验收条件。确认服务支持 edit 且该项目已授权后，再将适合的同类编码任务交给 DeepSeek。不要扩展文件范围。取得成功且 edit_ready 的完整结果后，检查实际差异和源文件哈希，再合入原项目并运行必要验证；只读结果或模型自述不能当成编码完成。

合入条件包括：作业 `succeeded`、报告 `edit_ready=true`、无错误、机械差异完整、路径在授权范围、当前源文件匹配 `before_sha256`，合入后的字节匹配 `after_sha256`。服务返回 `source_applied=false`，表示原项目尚未合入。完整分页、摘要校验及兼容恢复方法见 [配套 Skill 的 MCP 契约](../.agents/skills/deepseek-delegate/references/mcp-contract.md)。

### 安装配套 Codex Skill（可选）

将仓库 `.agents/skills/deepseek-delegate` 整个目录复制到自己的 `.codex/skills`，保留其中的 `references`。以下步骤仅在目标目录不存在时复制，避免覆盖已有 Skill：

```powershell
$delegateSource = 'D:\Tools\deepseek-harness-mcp\.agents\skills\deepseek-delegate'
$delegateDestination = Join-Path $env:USERPROFILE '.codex\skills\deepseek-delegate'
if (Test-Path -LiteralPath $delegateDestination) {
    Write-Host '目标 Skill 已存在，请比较现有版本后再更新。'
} else {
    New-Item -ItemType Directory -Path (Split-Path -Parent $delegateDestination) -Force | Out-Null
    Copy-Item -LiteralPath $delegateSource -Destination $delegateDestination -Recurse
}
```

如果你设置了 `CODEX_HOME`，请将目标位置改为实际 Codex 配置目录。重新加载 Codex 后，可明确要求“使用 deepseek-delegate Skill”。Skill 规定分工和复核流程，不能替服务开启 `edit` 或扩大项目授权。

## 7. 任务恢复、取消和常见问题

每次任务都保存 `client_request_id`、完整参数和返回的 `job_id`。响应丢失时使用原编号及完全相同参数恢复；新编号会启动新的模型工作，可能重复计费。改了提示、模式、范围或时限，却继续用旧编号，会触发 `IDEMPOTENCY_CONFLICT`。共享后台统一执行配置中的容量限制，各前端不会各自创建额外执行器绕过这些限制。

多会话共享的前提是同一 Windows 用户、同一可信服务配置、同一数据目录，以及匹配的服务代码和解释器。各前端仍是独立 stdio 连接，但请求进入同一个 TaskManager 和 SQLite 历史：相同请求编号和参数返回同一任务，不会因另开窗口再执行一次；一个会话提交后，其他连接到该后台的会话可以按 `job_id` 查询、读取结果或请求取消。共享后台不会区分不同聊天的任务所有者，请仅让应当共享该授权的客户端使用同一配置。

需要停止时调用 `harness_cancel(job_id)`，再查询停止结果。`cancelling` 不是已停止；`termination_failed=true` 表示未确认停止。停止查询、关闭单次等待或断开界面都不等于取消任务。关闭一个前端只断开该会话，其他连接和后台任务继续；最后一个前端断开后，只要有等待或运行任务，后台仍继续工作，无连接且无任务时默认空闲 30 秒后安全退出。停止未确认时后台保留归属和锁并阻止新执行。取消本机进程也不能证明服务商停止计费。服务重启不会自动重放未完成任务，旧任务可能标为 `interrupted`。

升级服务代码、调整模型或项目权限后，旧后台不能直接接受新配置。先完成或取消旧任务、关闭全部旧连接，等待后台退出，再重新连接。首次由直接模式切换时也需要先关闭占用原 `data_dir` 的旧服务。不带 `--shared` 的直接模式仍实行单实例锁，只适合一个连接使用；不要删除 `service.lock`、任务数据库或归属记录，也不要改用随机数据目录绕过恢复检查。

| 现象或错误 | 处理方法 |
| --- | --- |
| 找不到 Python 或 `ModuleNotFoundError` | 核对客户端 `command` 指向安装目录的 `.venv/Scripts/python.exe`，确认 `uv sync` 成功；不要填系统 Python 的模糊命令。 |
| 握手时 `connection closed: initialize response` | 查看该 MCP 启动时的安全错误输出。doctor 只检查前置条件，不能证明初始化握手成功；按下面的具体错误处理，不先认定是 API key。 |
| `INSTANCE_LOCKED` | 直接模式的另一个服务占用了同一数据目录。确认当前注册包含 `--shared`；切换前先完成或取消旧任务并关闭旧服务。保留锁文件、数据库和归属记录。 |
| `SHARED_BACKEND_UNAVAILABLE` | 数据目录仍由旧直接实例占用，或后台归属尚未确认。先核对并关闭旧连接、确认任务安全停止，不删除登记记录或换目录绕过。 |
| `SHARED_CONFIG_MISMATCH` | 已运行后台的配置、授权范围或代码版本不同。完成或取消旧任务，关闭全部旧连接，等待后台退出，再用一致的配置重新连接。 |
| `SHARED_LAUNCH_FAILED` / `SHARED_START_TIMEOUT` | 共享后台未能独立启动，或未在启动期限内就绪。核对 Windows WMI/CIM 服务、Python 绝对路径及私有目录权限，并设置 Codex `startup_timeout_sec = 30`；不要反复启动新任务。 |
| `SHARED_STORAGE_UNSAFE` / `SHARED_AUTH_FAILED` | 私有登记目录或本机认证未通过检查。确认目录属于当前用户且没有链接、重解析或权限漂移；保留现有任务和归属，不放宽认证检查。 |
| `SHARED_CREDENTIAL_MISMATCH` | 使用环境变量凭证的连接携带了不同 API key。共享后台不会在活动任务中切换凭证；先统一客户端环境，完成后台切换后重连，不发送密钥值。 |
| `SHARED_CONNECTION_LOST` / `SHARED_CONNECTION_CLOSED` | 当前前端连接已丢失或关闭，后台任务未因此取消。重新连接后用原请求编号及完全相同参数恢复，或用已保存的 `job_id` 查询；不改编号重复提交。 |
| `SHARED_PROTOCOL_INVALID` | 本机连接收到不符合约定的数据。核对各客户端使用相同安装版本，完成后台切换后重连；不回显原始协议包或凭证。 |
| `SESSION_BUSY` | 同一子会话仍有未完成任务。按已保存的 `job_id` 查询、读取结果或确认取消后再发送下一轮；需要独立并发工作时另建具有明确范围的子会话。 |
| `DESKTOP_RUNTIME_UNAVAILABLE` | 确认桌面端安装完整，非默认位置填写 `desktop_root`；不能用一个仅存在的空目录代替安装。 |
| `MODEL_CREDENTIAL_UNAVAILABLE` / `CREDENTIAL_UNAVAILABLE` | 检查可信配置指定的固定 API key 引用或环境变量。账号已登录、凭证文件存在，都不等于该引用可用；不要将密钥贴进任务或报告。 |
| doctor 成功，模型仍失败 | doctor 是前置检查；另核对 API key 有效性、余额、API 地址、模型名及接口兼容性。查看安全错误，不先换新编号重复提交。 |
| `PROJECT_NOT_ALLOWED` / `EDIT_NOT_ALLOWED` | 检查所用客户端 TOML 的项目编号和 `allow_edits`，保存后重新连接。Skill 不能绕过服务授权。 |
| 工具仍没有 `mode` 或编辑参数 | 当前连接可能缓存旧版本。重启 MCP 服务或客户端，再核对实时 schema；不要用 `review` 执行写入。 |
| `EDIT_SECRET_DETECTED` | 副本、修改前文本或差异命中凭证规则，宿主拒绝回传可合并补丁。检查本批范围及测试夹具，保留原作业，不关闭凭证检查来强行导出。 |
| 文件未进入快照 | 检查是否保存、是否 Git tracked、是否显式列入 `allow_untracked`，以及是否被授权范围或排除规则过滤。 |
| 快照因旧 Git、链接或文件漂移失败 | 更新支持固定只读命令的 Git，使用普通文件，停止同时修改本批源文件；不要放宽链接和一致性检查。 |
| `EXECUTION_BLOCKED` 或 `termination_failed=true` | 上次执行或恢复尚未确认安全停止。先处理同一任务的停止/恢复问题，保留归属记录；不要删记录或改数据目录来掩盖状态。 |
| 注册成功却发现不到工具 | 检查绝对路径、TOML 语法和客户端授权，重新连接。直接启动 stdio 服务后一直等待输入是正常状态，不代表需要浏览器访问某个网址。 |

Windows `desktop` 路线使用独立副本和受监管进程，但没有操作系统级文件、网络沙箱；仅用于你明确授权的本机项目。不要把整台电脑或个人 home 作为项目范围。共享后台登记目录位于 `data_dir/shared-service`，采用当前用户的私有访问权限和双向认证；环境变量型模型凭证在认证后只经内存传递，不写入后台启动参数或登记文件。本机配置、登记目录、凭证、任务快照和日志都不应上传到 GitHub。

## 命令核对范围

本教程的服务参数和工具契约面向 0.3.2。既有 CLI 注册格式在 0.3.1 编写时以 `codex mcp add --help`、`codex mcp list --help`、`claude mcp add-json --help` 和 `claude mcp list --help` 核对，使用的客户端版本为 Codex CLI `0.162.0-alpha.17.2`、Claude Code `2.1.273`；该历史检查不代替 0.3.2 多会话验收。客户端升级后如提示参数变化，以该机 `--help` 和官方文档为准。安装依赖、doctor、工具发现、多会话生命周期和真实模型调用应分别确认，验收结果以相应版本的验证记录为准。
