# DeepSeek Harness MCP 契约

本文件依据配套 MCP 的源代码编写。调用时以客户端真实暴露的 schema 和可信本机配置为准；不能为了兼容而绕过服务端约束。

## 工具及参数

| 工具 | 参数 | 用途 |
|---|---|---|
| `harness_submit` | 必填 `project_id`、`prompt`、`client_request_id`；可选 `relative_paths`、`timeout_seconds`、`mode` | 返回任务回执，不代表完成 |
| `harness_status` | `job_id` | 查询状态、阶段、耗时、错误、停止失败情况 |
| `harness_result` | `job_id`，可选 `cursor`、`page_size` | 分页读取已保存报告；`ready=false` 表示尚无报告 |
| `harness_cancel` | `job_id` | 请求取消；确认执行器停止后才进入停止终态 |
| `harness_watch` | `job_ids` | 开启自动续跑时，将已接纳/已完成作业绑定原主对话，不重放模型 |
| `harness_completion_list` | 可选 `state=null`、`limit=20` | 开启自动续跑时，只列当前原对话的完成事件 |
| `harness_completion_ack` | `event_ids` | 开启自动续跑时，完整处理结果后确认事件 |

客户端可能加 `mcp__deepseek_harness__` 等命名空间，使用实际名称。

`mode` 的真实 schema 默认值取可信根字段 `default_mode`。0.3.3 新 desktop 配置为 edit，旧配置缺省仍为 review；编码可显式指定 edit，只读调查显式指定 review。只有 `review/edit` 两种模式；没有 `write_paths`、`patch_pages`、自动合入或让执行器运行项目测试的参数。Docker 不接受默认编辑，编辑默认本身不代替项目 allow_edits 授权。

提交示例，值应替换为已确认的授权项目和本次任务书，不要把示例原样作为真实任务：

```json
{
  "project_id": "authorized-project",
  "mode": "edit",
  "client_request_id": "catalog-implementation-unique-id",
  "relative_paths": ["src/api", "src/types/catalog.ts"],
  "timeout_seconds": 600,
  "prompt": "主 AI 已确定的方法、修改范围、参考模式和验收条件……"
}
```

## 授权与输入范围

编辑要求可信项目配置 `[projects.<id>] allow_edits=true`，当前只支持 `desktop`。`EDIT_NOT_ALLOWED` / `EDIT_BACKEND_UNSUPPORTED` / `PROJECT_NOT_ALLOWED` 需要处理真实能力缺口；不要自动改配置或转用只读任务执行写入。

`relative_paths` 同时确定复制的读取范围和允许修改的范围，不能硬性区分只读依赖与写入文件。任务书另列两者，主 AI 以实际差异检查。“仅参考”文件若被修改，应拒绝该项改动。

路径是项目内相对文件/目录前缀；拒绝绝对路径、`..`、UNC、盘符、ADS 和越界链接。省略参数使用配置范围，显式空数组拒绝。新增文件应授权其父目录。文件来自已保存的 Git tracked 文件及可信 `allow_untracked` 清单；编辑器中未保存内容不会进入快照，普通未跟踪文件也不会自动进入。

默认提示上限 32000 字符，`relative_paths` 至多 256 项，默认超时 600 秒/配置默认上限 1800 秒；实际值可由可信配置改变。快照在准备阶段捕获，不是在回执时捕获。避免在执行期间同时修改本批目标文件。

## 生命周期与重试

1. 保存提交参数与 `client_request_id`，收到回执保存 `job_id`。
2. 回执 `auto_continue=true` 表示机制绑定，可继续独立工作；实际收到完成事件后才进入结果处理，不把 true 当作通知已被接收。没有绑定或渠道不可用时查询同一 job，按 `poll_after_seconds` 或更长间隔等待。直接模式保持一个执行位置；共享模式默认三个（可信配置1–8）和最多五个等待位置。同一会话按轮次顺序执行，互不依赖的子代理会话才可并行。
3. `succeeded/failed/cancelled/timed_out/interrupted` 是终态；`cancelling` 尚未确认停止，`termination_failed=true` 不能当作取消成功。
4. 响应丢失时，用原 ID、原参数恢复。新 ID 会启动新模型工作，可能重复计费。修改提示、模式、范围或超时而复用原 ID 会出现 `IDEMPOTENCY_CONFLICT`。
5. 服务重启不会重放旧未完成任务，可能记为 `interrupted`；明确需要重新执行时，使用新的 ID。
6. 用户要求停止时调用 `harness_cancel`，再查询停止结果。取消本地执行器不能证明上游停止计费。

改变可信默认模式后，旧 review 请求的恢复参数必须显式包含原 `mode="review"`，或直接读取已有 job_id；不要让新的默认 edit 改变旧请求指纹。

## 共享连接与子代理会话

0.3.2 及更新版本的 `--shared` 保留原四个任务工具，并增加以下三工具；只有实时schema确认存在才使用：

- `harness_session_open(project_id, client_request_id, title="", mode=可信default_mode, relative_paths=None)`：创建固定项目/模式/范围的会话，返回 `session_id`，不调用模型；此处的默认值以实际 schema 为准。
- `harness_session_send(session_id, prompt, client_request_id, timeout_seconds=None)`：初次任务或继续追问，返回 `job_id`，之后复用原状态/结果/取消工具。
- `harness_session_list(project_id=None, limit=20)`：找回会话和近期任务编号，不自动重放。

一个 Codex 对话可以保存多个 `session_id`；不同项目窗口各自保存自己的会话。后台共享同一幂等数据库、全局并发上限与等待队列，会话对话历史与作业副本独立。`SESSION_BUSY` 表示上一轮未结束；`SESSION_CONFIG_CHANGED` 表示绑定授权发生变化，应创建新会话。请求编号不能跨创建会话/提交任务重复使用。网络回执丢失先用原编号和参数恢复，禁止自动换编号重复执行。

共享后台在无连接但仍有未完成任务时继续运行；无连接且无任务30秒后退出。前端退出不触发取消。旧直接实例占用同一 `data_dir` 时，先结束旧连接后重连；不能删锁、清空历史或换随机目录绕过归属检查。变更可信权限/路由/实现会拒绝静默复用旧后台，等待原任务完成并空闲退出后重连。

会话上下文由本机最近四轮脱敏报告重建，最多16000字符；不保留模型reasoning。每轮从当前授权源码生成新的副本，上一轮未合入的编辑不会自动传给下一轮。主 AI 须按实际差异合入并验证，再提交依赖该修改的任务。

## 完成事件与自动续跑

0.3.3 在可信 `[completion] auto_continue=true` 时发现 watch/list/ack 三个完成事件工具，共享模式合计十工具；未开启时仍七工具，旧配置缺省 false。`auto_continue=true` 仅表示当前客户端完成机制已绑定，不证明宿主接收或主 AI 已处理。Codex thread/turn 元数据及动态本机 app-tools 通道须通过宿主能力/归属只读预检；失败返回 false 与 auto_continue_reason，普通模型任务仍接纳。CODEX_APP_TOOLS_PIPE_PATH 按客户端 env_vars 白名单透传，不硬编码路径。

普通 Claude 使用全部已启用审查、编辑、子代理工具，不因缺少通知能力退化为只读。实验 channel 另需服务 `--claude-channel`、客户端 `MCP_PROTOCOL_NEGOTIATION=legacy` 和交互启动 `--dangerously-load-development-channels server:deepseek_harness`。服务声明实验 claude/channel capability，使用官方通知格式；宿主不反馈渠道许可，绑定 true 不等于渠道已登记。[Claude Channels 参考](https://code.claude.com/docs/en/channels-reference)、[协议说明](https://code.claude.com/docs/en/mcp#push-messages-with-channels)

- `harness_watch(job_ids)`：为已接纳或已完成作业绑定可信当前来源；不调用模型、不改变既有目标。Codex 同源可刷新动态通道；Claude 新进程不是原私有 session，旧绑定不能改投。
- `harness_completion_list(state=None, limit=20)`：只列实际当前原对话的事件；筛选值为 `pending/claimed/delivered/uncertain/acknowledged`。
- `harness_completion_ack(event_ids)`：结果完整读取、复核和必要后续工作处理结束后确认事件；同源确认幂等，不能替另一主对话确认。

接纳、来源绑定、终态与完成事件使用事务保存；没有授权绑定的旧作业不会因升级被批量唤醒。每个已绑定作业只有一个完成事件。后台向原主对话发送固定作业、事件、模式和终态标识，让主 AI 通过 `harness_result` 读取报告；不把用户提示、模型报告或代码插入唤醒指令，不接受任意 URL、命令或目标聊天参数。

delivered 须结合 delivery_status：Codex accepted 表示宿主接受，Claude channel_written 只表示 stdio 写出，不能证明 Claude 已接收；均不能证明主 AI 已处理或合入。acknowledged 才记录结果处理确认。收到事件后核对原任务，读取完整必要分页并按下文检查。失败、取消、超时或中断只核对状态与错误，不因完成通知自动重跑模型或应用部分修改，处理结束后仍可确认事件。

确定未写出的临时错误允许退避重试；写后断连或结果不明时记为 uncertain，不盲目重发。原来源仍可访问时通过事件列表读取并处理后 ack。Claude 前端使用私有 connection/session 标识路由窗口，服务 session 不是 Claude 真实会话编号；进程重启生成新身份，当前没有安全跨重启证明，旧绑定不能通过 watch 改投。原 job_id 仍可读结果继续，不声称显式 resume 必然恢复通知。/clear 或交互 resume 可能沿用 MCP 进程，当前仅保证窗口/连接路由。不得为修复通知重新提交模型、转移目标或覆盖用户最新限制。Codex 原生空闲唤醒及自动读取、Claude 实际传输隔离已经验收；Codex 首次确认使用本机存储，随后原对话通过真实来源捕获、watch 和幂等 ack 工具验证了完成入口。初始绑定来自可信验收脚本，没有重新提交模型任务；Claude 应用内续跑仍未验收，历史模型和七工具发现不代替这些检查。

## 编辑结果与分页

以下是本地完整编辑报告的字段；MCP 分页会拆分其中的差异和文件文本，不能把分页元数据当作完整 changes：

| 字段 | 意义 |
|---|---|
| `mode="edit"`、`edit_ready=true` | 宿主已收集并验证完整机械差异 |
| `source_applied=false`、`requires_main_agent_review=true` | 尚未合入原项目，需主 AI 复核 |
| `snapshot_id`、`change_set_id` | 基线及变更集合的标识 |
| `changed_files`、`changes` | 实际改动路径和逐项信息 |
| `changes[].change_type` | `add/modify/delete` |
| `changes[].before_sha256/after_sha256` | 修改前/后文件字节 SHA256；不存在的一端为 null |
| `changes[].diff/after_text` | 实际 unified diff 和修改后完整 UTF-8 文本；删除的 after_text 为 null |
| `patch_text` | 按清单顺序拼接的全部 diff |
| `tests="not_run"` | DeepSeek 没有运行项目测试 |

获取结果需固定 `page_size`（1–100），使用同一任务返回的 `next_cursor`；不要跨 job 或改变 page_size 复用游标。当前报告文本每页最多 8000 字符。只读摘要/问题可按需读取；合入的差异必须完整，不能把被截断的报告文本当完整补丁。

### 当前 edit 分页的组装

若实际接口返回下列字段，用编排代码/本地缓存累积各页，不把每页所有重复内容都注入主 AI 上下文。首先核对各页的 `job_id/report_id/snapshot_id/change_set_id` 一致，然后按偏移组装。所有文本 offset 和 `*_chars` 均按 Python Unicode 码点计数，不是字节数或 JavaScript UTF-16 长度；建议用 Python 组装，JavaScript 则用 `Array.from(text)` 计数和切片，避免 emoji 等非 BMP 字符导致错误。文件 SHA256 始终按完整 UTF-8 字节计算。

- `changes` 是当前页的变更元数据，不含完整 `diff/after_text`；按 `changes_offset` 累积至 `total_changes`。`changed_files` 也仅覆盖当前页，不能当作全部文件清单。
- `patch_text` 是最多 8000 字符的补丁片段；按 `patch_text_offset` 连续累积至 `total_patch_chars`。元数据的 `diff_offset/diff_chars` 标识各文件在完整补丁内的位置。
- `change_contents` 是文件内容片段，含 `path/field/offset/text/total_chars/sha256`。按每个路径的 offset 连续组装 `after_text`，核对总字符数及完整 UTF-8 字节的 SHA256。不把某页空片段当作该文件为空。
- 元数据的 `after_text_is_null=true` 表示删除；非 null 且 `after_text_chars=0` 表示真正的空文件，后者不会有内容片段。
- 检查偏移无缺口/重叠、总数一致，并取得完整的变更集合。重建完整 changes 时，每项仅保留 `path/change_type/before_sha256/after_sha256/before_size_bytes/after_size_bytes/diff/after_text` 八个原始字段；剔除分页新增的 offset、字符数及 null 标记。将 `{"snapshot_id": snapshot_id, "changes": changes}` 按 Python `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")` 计算 SHA256，核对 `change_set_id`，不能对分页元数据或已脱敏/被重写的文本算这个摘要。

按 `next_cursor` 继续读取到所需机械数据完整。若游标为空但数据仍缺失，停止合入。完整缓存可作为应用产物，主 AI 阅读实际差异；不必再将全部 after_text 和报告文本打印一次。

### 本地完整报告兼容路径

旧版服务或尚未重载的客户端可能仍只返回审查字段，甚至固定 `changed_files=[]`，没有编辑就绪字段。此时不能从 summary 推断编辑成功，也不能发明新的分页字段。

仅当调用端能在同一可信主机访问 MCP 数据目录时，可以从本地完整报告恢复：

1. 先用工具核对 job `status="succeeded"`、`ready=true`、`error=null`，取得 `report_id`。只信任服务返回的 job ID；本实现为 32 位小写十六进制 UUID hex。
2. 从实际启动的可信 TOML 配置定位 `data_dir`；相对目录按该配置文件的父目录解析，构造 `data_dir/jobs/<job_id>/report.json`。不使用模型摘要给出的绝对路径，不读取凭证或个人会话。
3. 对读取的 **report.json 原始字节** 计算 SHA256，并与工具返回的 `report_id` 对齐；此实现保存与分页使用相同 canonical JSON 字节。校验不一致时停止使用该报告。不要重新序列化后用不同规则算哈希。
4. 从校验通过的报告读取编辑字段，核对 `job_id`、`project_id`、`snapshot_id` 及下列合入条件。只读取需要的差异，不输出整份原文和重复的完整 after_text。

不能访问可信本地数据，或分页完整度/摘要不匹配时，报告结果接口缺口。保留 job 供恢复，不重复提交同一编码任务，不据摘要合入，也不绕过 MCP 直接启动另一个 DSH 进程。

## 合入条件

- 工具查询的状态为 `succeeded`，真实报告 `mode="edit"`、`edit_ready=true`、`finish_reason="completed"`、`error=null`。
- `source_applied=false`，完整 `changes` 和 `change_set_id` 可用且与本次范围一致。空清单是“没有实际改动”，不能只凭摘要称功能已实现。
- 主 AI 核对真实差异符合任务书；修改/删除的当前源文件字节哈希匹配 `before_sha256`；新增路径不存在；拒绝路径越界或符号链接/reparse point 越界。
- 合入时保持 UTF-8 文本的原始字节与换行；避免 PowerShell 默认编码或重写换行造成补丁内容变化。原样合入后检查 `after_sha256`。
- 模型返回文本、引用机械校验和 `succeeded` 都不能代替行为验证；主 AI 在原项目运行适当检查并记录真实结果。

完整报告及差异受输出/持久化上限约束（默认模型输出 1 MiB，报告保存固定 2 MiB）。输出过大时拆成更小的独立批次，不提高权限、不接受截断补丁，也不为了修报告格式再次执行同一大任务。
