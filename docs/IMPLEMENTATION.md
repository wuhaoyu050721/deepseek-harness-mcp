# 实现与权限边界

本文描述 0.3.4 的服务结构，包括可选 `--shared` 连接、可信默认编辑模式、Codex 桌面完成通知和 Claude Code 实验 channel。Codex 原对话空闲唤醒、实际自动读取及完成工具入口，Claude 通道传输与隔离已验收；Claude 应用内续跑仍未验证。安装与使用见 [连接教程](MCP-CONNECTION.zh-CN.md)，具体范围见 [验证记录](VALIDATION.md)；历史协议、多会话和模型执行记录不代替自动续跑验收。

## 数据流

```text
MCP stdio 任务工具
  -> 直接模式：本进程 TaskManager
     或 --shared：各前端 SharedClient -> 本机共享后台
  -> TaskManager：统一调度、总期限与取消
  -> JobStore：SQLite 幂等、容量、冻结配置与终态
  -> session：固定项目/模式/范围、每会话串行、历史上下文重建
  -> snapshot_worker / workspace：授权文件快照与 manifest
  -> edit_workspace：按需创建独立可写副本
  -> DesktopRunner：受监管进程 -> 已安装 Harness runtime -> SDK
     或 DockerRunner：容器 Worker 与租约监管（可选审查）
  -> edit_collect_worker：执行停止后比较实际文件
  -> report：引用校验、差异校验、脱敏、持久化摘要与分页
  -> completion_events：已绑定作业的终态事件，与任务终态同事务保存
  -> CompletionDispatcher：固定完成消息 -> 原 Codex 主对话或私有 Claude channel
  -> 主 AI：读取完整结果、复核、必要合入与验证 -> completion_ack
```

主项目不作为 runtime 工作目录。review 使用任务快照，edit 使用另外复制的工作区。stdout 仅输出 MCP 协议；子进程原文不混入协议。适配器只采集根会话最后 assistant 文本，reasoning/tool-call 不作为报告。

## 共享连接与后台归属

`python -m harness_mcp.server --shared --config ...` 保留对外 stdio 协议和任务工具契约。每个客户端会话有自己的前端进程；它们通过私有登记目录发现本机后台，由一个 TaskManager 持有原 `data_dir` 的实例锁，并共同使用原 SQLite、队列、幂等请求和历史。原直接模式不带 `--shared`，仍只允许一个实例使用同一数据目录。

共享连接复用必须匹配可信配置、项目权限、服务版本、代码和解释器身份；匹配失败拒绝接入，不临时替换运行任务的授权。配置或代码更新后，完成或取消旧任务，关闭旧连接，等待后台空闲退出，再重新连接。旧直接实例、未知归属或未确认停止的执行占用数据目录时，共享启动也失败关闭，不删除实例锁、数据库或执行记录，不生成随机数据目录规避恢复。

后台登记和启动文件位于 `data_dir/shared-service`。目录拒绝链接、重解析、硬链接和身份漂移；Windows 权限限制为当前用户及 SYSTEM，POSIX 使用私有目录和文件权限。前后台以回环 TCP 传递有界 JSON，用新挑战、客户端随机值、配置摘要及不同角色标签进行 HMAC 双向认证。客户端先验证后台证明，再在需要时通过内存传递环境变量型模型凭证；凭证不写入启动参数、登记文件或日志。不同凭证的连接不能替换现有后台凭证。这里的本机认证不能隔离当前 Windows 用户下的其他程序。

Windows 后台通过 WMI/CIM 创建隐藏的独立进程，避免继承单个 stdio 前端的退出生命周期；启动失败不降级为依赖该前端的普通子进程。其他平台采用独立进程会话，平台运行效果仍需相应验收。关闭前端只关闭该连接，不调用 TaskManager.close，也不取消任务。无连接但有等待或运行任务时后台保留；无连接且无工作时默认空闲 30 秒后安全关闭。无法确认执行停止时继续保留后台、实例锁和阻断状态。

共享 RPC 仅接受明确的工具方法和校验后的 JSON，错误经过统一脱敏后返回。连接丢失可能发生在提交或取消已经受理之后，因此前端不自动重发这些调用。恢复使用原 `client_request_id`、完整参数或已保存的 `job_id`；重新连接不自动重放任务。

## 子会话、上下文与并行调度

共享模式在原四个任务工具之外提供 `harness_session_open`、`harness_session_send`、`harness_session_list`。open 固定项目、mode、relative_paths，登记标题和全局幂等编号；send 在该会话授权内创建一轮普通作业，返回 job_id，继续由原 status/result/cancel 管理；list 按项目返回会话与近期任务，重连不要求建立重复会话。旧 `harness_submit` 仍可直接提交独立任务。

不同 session 独立调度和重建上下文，可并行执行；同一 session 有未完成任务时，新一轮 send 返回 `SESSION_BUSY`。默认 `limits.max_parallel_jobs=3`，可信配置允许 1 到 8；`max_queue` 默认 5，运行和等待容量由整个后台共享。子会话不引入账号权限边界，连接同一配置的客户端可访问该配置授权的会话和任务。

每轮 worker 创建独立 home 和工作副本，不保留一个持续运行的桌面 UI 会话。继续上下文只由本机保存的该会话脱敏历史报告有限重建，最多最近四轮、16,000 字；不同 session 的历史不会互相混入。编辑后的工作副本不会自动成为下一轮或其他会话的源码。主 AI 核验并合入本轮产物后，下一轮才从新的原项目创建快照；并行编辑仍需检查原文件哈希与冲突。

## 完成通知与原对话续跑

可信 `[completion] auto_continue=true` 开启功能；未设置时为 false。Codex 入口从实际请求宿主元数据读取 thread/turn，从受信启动环境读取动态 app-tools 通道；客户端通过 env_vars 白名单透传 CODEX_APP_TOOLS_PIPE_PATH，不硬编码通信路径。绑定前只读预检确认宿主能力与原对话归属；失败时回执 auto_continue=false 与原因，普通任务执行、状态和结果接口仍可使用。工具参数和模型输出不能指定通知目标、URL、命令或转发内容。

接纳任务与来源绑定在 SQLite 同一事务完成，幂等重试保持原作业和目标。已有或已完成作业可以通过 `harness_watch(job_ids)` 明确绑定，不能重放模型；已绑定的目标不可变更。Codex 同一可信原对话可更新动态通道；Claude 前端的私有 session 身份只在该进程生命周期内稳定，重启后产生新身份，不能改投既有绑定。历史任务不会因升级而被批量自动唤醒。

进入 `succeeded/failed/cancelled/timed_out/interrupted` 终态时，同事务为已绑定作业写入唯一完成事件。投递器由事件唤醒，发送固定续跑消息以及作业、事件、模式和终态标识；不插入用户提示、模型报告或代码。主 AI 被宿主启动后，重新读取原 `job_id` 的完整结果，遵守当前用户授权并独立复核。模型完成通知不能扩大权限，也不能使失败或部分编辑满足合入条件。

事件独立记录 `pending/claimed/delivered/uncertain/acknowledged`、投递租约、delivery_status 与稳定错误码。delivered 的 Codex accepted 表示宿主接受，Claude channel_written 仅表示前端写出，不证明宿主已接收；两者均不能证明主 AI 已处理。完整结果及后续工作结束后调用 `harness_completion_ack(event_ids)` 才进入 acknowledged。`harness_completion_list(state=None, limit=20)` 和 ack 只作用于原 Codex 对话或原 Claude 前端身份；共享配置的普通作业读取权限不因此改变。

确定未写出的临时错误可退避重试；写后断连或投递租约失效时进入 uncertain，不盲目再次发送可能已受理的消息。原对话通过事件列表读取结果并确认即可恢复。关闭前端不等于取消模型，通知绑定及事件持久化供恢复使用。失败、取消、超时、中断仍只提示读取状态与错误，不自动重新提交或应用部分修改。

启用功能时增加 `harness_watch`、`harness_completion_list`、`harness_completion_ack`，共享模式合计十二工具；关闭时九个基础工具（含预检及服务信息）。普通 Claude 仍支持所有已开启的审查、编辑和子代理工具，没有推送上下文则返回 auto_continue=false。Codex 通知需要已通过预检的宿主，Claude 推送另须显式服务参数和客户端渠道登记；工具发现不证明自动唤醒。通知桥、真实模型完成与主对话续跑需分别验收。

Claude 适配只在显式 `--claude-channel` 时声明实验 claude/channel capability，并按官方 notifications/claude/channel 格式发送固定事件。客户端需使用 legacy 握手协议并在交互启动时允许该开发渠道。服务没有渠道许可反馈，机制绑定 true 不代表 Claude 已登记或接收；channel_written 与 ack 分开。每个 stdio 前端独有的 connection/session 标识确保路由窗口隔离，session 标识不是 Claude 的真实会话编号。重启身份变化后旧 job_id 仍可读取，但不能用 watch 伪装原身份；/clear 或交互 resume 可能保留 MCP 进程，当前不宣称识别所有对话切换或安全跨重启恢复。配置与官方参考见连接教程。

## 配置与文件

可信 TOML 决定 provider、model、base_url、凭证来源、项目范围与资源上限。工具只选择已有项目，不能传入任意本机目录或扩展权限。数据库冻结非秘密执行配置，凭证只存引用，执行时读取并在内存中登记实际凭证值供结果检查。

根字段 `default_mode` 未设置时为 review，新 desktop 初始化配置为 edit；MCP schema 的默认值与可信配置一致。省略模式走可信默认，显式 review 保留只读任务；会话保存解析后的固定模式。edit 仍要求项目 allow_edits=true 和 desktop 后端，默认值本身不授予缺失的项目权限。Docker 不接受默认编辑。改变默认模式后恢复旧请求需传原模式或直接读取原 job_id，避免改变旧请求指纹。

文件在 preparing 阶段捕获，默认取保存到工作树的 Git tracked 文件，未跟踪文件按可信配置逐个授权。路径校验拒绝绝对路径、上级、盘符、UNC、ADS、设备名、控制字符和歧义尾部；祖先与文件句柄身份核对 symlink、reparse 和多硬链接。凭证、私钥、依赖、缓存、常见二进制和备份排除规则不能识别全部硬编码秘密。

Git 元数据只使用固定只读 plumbing，禁用 lazy fetch、fsmonitor、hooks、replace objects 和可选锁。不会为检测 dirty 执行 status、diff 或仓库 filter。缺对象或 Git 缺必需能力时失败关闭。复制前后复读文件哈希，并核对 HEAD/index/tree 元数据变化；普通复制不构成操作系统原子快照。

## 任务与进程

同一数据目录只允许一个 TaskManager 持锁，共享模式的多个前端不各自持有或放宽这把锁。SQLite 事务先查原请求编号，再查执行阻断、会话忙状态和全局容量；相同参数跨连接返回原任务，参数不同冲突。总期限包含准备、模型执行与差异采集；服务重启不自动重放任务。0.3.1 的单执行位置是历史调度契约，不代表 0.3.2 的全局并发上限，也不代表多个前端各自获得一份容量。

Windows 原生进程通过 STARTUPINFOEX 的 Job 属性在创建时归属任务，恢复返回的主线程句柄，标准管道采用继承白名单。没有普通创建后再 Assign 的兼容退路。确认整树停止后才发布停止终态；停止未确认时保留归属记录、实例锁和阻断状态。

准备/采集与 desktop 的 execution.json 在确认停止后原子归档到对应 run 的 stopped 目录，保留记录字节。desktop 另须确认专用 home 清理。异常归属、清理/归档失败继续阻断恢复；正常历史不会占用活跃恢复扫描额度。

Desktop runtime 复用已安装程序，每任务独立 home/session 与固定 profile。官方默认凭证引用是 `refs.DEEPSEEK_API_KEY`，不使用账号 grant，不复制个人聊天。模型和接口由本机配置显式指定；Messages 默认 API 根地址含 `/anthropic`，runtime 追加 `/v1/messages`。

JobObject 管理生命周期，不提供文件或网络沙箱。desktop 使用当前 Windows 用户权限，独立副本不能阻止读取目录外文件。Docker 路线设计了只读源挂载、可写 home/output、非 root 用户和租约 watchdog，未完成实际容器验证；桥接网络没有经过验证的出站白名单。

## 报告与编辑

finish_reason 与 report_format 分开。JSON 无效时保留有界原文，不重新调用模型修复格式。模型报告的测试声明不采信，固定为 `tests=not_run`；当前 `usage=null`。引用按快照 manifest 校验文件、行号、文本和哈希，机械校验不证明业务判断成立。

报告和诊断共用完整认证字段检查，覆盖 JSON 引号和 wire 转义，敏感字段在截断前检查并删除。Docker 结果字段与错误结构严格校验，异常输出不回显。report.json 临时写入、fsync、原子替换，数据库关联原始字节 SHA256；读取篡改拒绝，分页游标绑定任务、摘要与页大小。

edit 只允许可信配置 `allow_edits=true` 且 backend=desktop。保持不可变 baseline，另建可写副本；停止执行后由宿主比较实际字节。修改范围取项目授权、快照和本次请求范围的交集；新增/删除也受约束。

差异采集拒绝链接、大小写别名、敏感路径、非 UTF-8、二进制、超限与实际凭证命中。从核验后的前后字节生成统一补丁；报告再次核对内容与摘要，自洽的伪造补丁也拒绝。只有成功执行且无错误时 `edit_ready=true`，始终 `source_applied=false`。

主 AI 重建完整分页，核对原文件 `before_sha256` 后应用真实内容，并独立运行必要验证。没有自动写回原项目的 MCP 工具。固定提示禁止 Harness 运行项目测试、程序、构建、安装和 Git，但没有保存完整工具事件来证明所有命令从未执行。

## 发布与复现

`scripts/package_release.py` 按公开清单收集源码与通用文档，拒绝源链接、大小写冲突和常见凭证格式；固定 ZIP 时间戳并回读逐字节比对，生成 SHA256SUMS 与成员 manifest。实际本机凭证检查由发布者在内存中另行执行，不由公开脚本读取个人凭证库。

本机配置、任务目录和原始联调报告不纳入源码包或仓库提交。公开验证摘要保留检查类型与通过范围，删除本机路径、任务编号、个人配置摘要及模型原文。重跑本地/协议测试不代表重跑真实模型验收；依赖外部程序的检查须先满足各自前置条件。
