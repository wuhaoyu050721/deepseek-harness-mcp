# 0.3.4 版本说明

这次针对 MCP 实测的凭证误报、报告质量、重连提示、失败诊断和重复传输进行改进。

- 源码凭证检查使用有界词法识别，PHP `=> null`、变量、环境读取、动态认证头和认证正则不再套用自然文本的凭证替换规则。真实固定凭证、已知密钥、provider token、私钥和凭证 URL 仍拒绝导出。`harness_preflight` 在提交前返回脱敏规则、相对路径和行号；正式准备阶段再次检查实际字节。
- `harness_result` 新增 summary/patch/files 视图，full 保持兼容；执行状态、引用质量计数和机械补丁可供复核的状态分别显示。结果不会宣称测试通过、原项目已修改或当前基线已验证。
- `harness_service_info` 显示当前与磁盘配置指纹、是否需要重连，以及活动连接、排队与活动任务数量。doctor 只读诊断现有后台；旧后台未实现信息接口时计数为未知，不伪造为0。
- 脱敏错误携带阶段、诊断编号和受控异常类型；首次诊断贯穿准备进程、执行器、共享 RPC 和 MCP。未知异常文本和绝对路径不向客户端输出。
- 轻视图只在首页返回自然元数据，分页绑定视图，文件内容与补丁保持字节和哈希；未完成任务的建议查询间隔退避到2/5/10/20秒，继续沿用原对话完成通知。

兼容 Codex 与 Claude Code 普通 stdio/MCP 工具调用。共享模式九个基础工具，completion 开启后十二个；直接模式六个或九个。Claude channel 的应用内自动续跑仍须单独验收。具体测试证据与发布包校验见 VALIDATION.md 及本版本公开验证记录。

# 0.3.3 版本说明

本次增加任务完成后通知原 Codex 主对话、可选 Claude Code 实验 channel 的续跑链路，以及可信默认编辑模式。完整回归761项测试和57个子测试、全新源码包安装、三前端通道传输与隔离均通过。既有报告的真实 Codex 空闲唤醒和自动读取已通过；首次本机确认后，原对话又通过实际 MCP 来源捕获、watch 和幂等 ack 验证了完成工具入口。没有提交新模型任务；Claude 应用内续跑仍未验收。详细范围见 [验证记录](VALIDATION.md)。

可信 `[completion] auto_continue=true` 开启完成机制。Codex 需有效桌面 thread/turn 宿主元数据和正规本机通道，并先通过能力及归属的只读预检。宿主缺少能力时返回 auto_continue=false 与原因，普通模型任务仍正常接纳；客户端通过 env_vars 透传 CODEX_APP_TOOLS_PIPE_PATH，不硬编码动态句柄。旧配置及没有上下文的连接继续查询状态与结果。

Claude 普通连接支持审查、编辑及子代理七/十工具；实验推送另需 `--claude-channel`、legacy 协议和交互启动的开发渠道登记。新增 examples/claude.channel.local.json，普通 claude.local.json 不带 channel 参数。auto_continue=true 仅表示机制绑定，Claude 不反馈渠道许可；channel_written 只证明 stdio 写出，ack 才记录完整处理。每前端私有身份区分窗口，进程重启产生新身份，旧绑定不能 watch 改投新会话；原 job_id 可继续读取，不承诺 resume 自动恢复通知。

作业终态与唯一完成事件在同一数据库事务保存。后台发送固定作业/事件标识和状态，让主 AI 读取 `harness_result` 再复核、合入及验证；模型报告不直接组成续跑指令，失败或取消不自动重新执行或应用部分修改。来源目标不可改投，重连原对话可更新本机通道。

启用功能时新增 `harness_watch(job_ids)`、`harness_completion_list(state=None, limit=20)` 和 `harness_completion_ack(event_ids)`；共享模式共十工具，未启用仍七工具。watch 可绑定尚无来源的旧作业或恢复相同来源，不重放模型、不变更已绑定目标。delivered 须结合 delivery_status 判断 Codex 接受或 Claude 写出，完整处理另由 acknowledged 记录；写后断连或结果不明记为 uncertain，不盲重发。主 AI 完整处理结果后再 ack。

可信根字段 `default_mode` 决定 submit/session_open 真实 schema 的默认模式。新 Windows desktop 初始化配置为 edit，登记项目 allow_edits=true，并开启完成通知；旧配置缺省仍为 review、自动续跑 false。显式 `mode="review"` 可只读审查，无须反复关闭编辑权限或重载。edit 始终只修改独立副本，原项目仍由主 AI 复核和验证后合入。Docker 不接受默认编辑。

升级不覆盖已有本机配置。按现有生命周期完成或取消旧任务、关闭旧连接并切换后台后加载新工具；保留原数据目录和历史。改变默认模式后，原 review 请求使用显式原模式恢复，或直接按原 job_id 读取，避免以新的默认模式重用旧请求编号。历史任务不会批量自动唤醒，需在原对话明确 watch。

# 0.3.2 版本说明

0.3.2 增加 `--shared` 本机共享连接。客户端仍通过 stdio 启动前端；多个连接复用同一个持锁的后台 TaskManager、SQLite、请求编号和历史，避免第二个客户端因相同数据目录被占用而在 MCP 初始化前退出。

共享模式新增 `harness_session_open`、`harness_session_send`、`harness_session_list`，原四个任务工具保持兼容。一个 Codex 聊天可建立多个独立 DeepSeek 子会话，多个项目窗口也可各自建立绑定本项目的会话。每会话固定项目、模式和文件范围，同会话下一轮不能与未完成工作重叠；不同会话默认最多三个任务并发，可信配置 `limits.max_parallel_jobs` 可设为 1 到 8，等待容量仍由全局 `max_queue` 限制。

继续上下文从该会话本机保存的已脱敏历史报告有限重建，最多最近四轮、16,000 字。每轮 runtime home 与代码副本保持独立，前一轮和其他子代理的修改不自动合并；编辑结果仍由主 AI 检查真实差异、核对源文件哈希后合入并验证。

共享后台使用私有登记目录和回环连接的双向认证，核对可信配置、授权范围、服务代码及解释器身份。Windows 以 WMI/CIM 创建隐藏独立后台。环境变量型模型凭证只在后台认证成功后经内存传递，不进入启动参数或登记文件。

单个前端关闭不取消任务，也不影响其他连接。最后一个前端断开后，后台继续处理等待或运行任务；无连接且无任务时默认空闲 30 秒后安全退出。停止未确认时保留归属、锁和执行阻断。断线后仍须恢复原请求或任务，不自动重发可能已受理的调用。

连接教程和通用 Codex/Claude 示例默认使用 `--shared`。已有本机片段不会被初始化脚本覆盖，升级需要检查参数；Codex 配置还应设置安装目录 `cwd` 和 `startup_timeout_sec = 30`。从旧直接模式迁移前先完成或取消旧任务并关闭旧连接。修改授权或更新代码后，关闭使用旧配置的全部连接、等待后台退出，再重新连接；不要删除锁、数据库或归属记录，也不要换数据目录绕过恢复。

0.3.2 已通过566项测试和27个子测试、两个真实Codex控制连接的七工具发现、WMI后台独立生命周期、两个真实DeepSeek任务并行及同会话追问。完整范围与探针恢复说明见 [验证记录](VALIDATION.md) 和 [机器摘要](../artifacts/public/validation-v0.3.2.json)。以下0.3.1检查作为历史保留。

# 0.3.1 发布说明

发布日期：2026-10-10。默认路线为 Windows desktop，复用本机已安装的 DeepSeek Harness runtime。MCP 服务提供审查与授权副本修改四工具。

## 安全修复

- 快照只使用固定 Git plumbing 和原始文件字节，禁用 lazy fetch、仓库 filter、hook、fsmonitor 与 replace object 路线；旧 Git 不支持必需能力时拒绝继续。
- 报告与诊断共用认证字段规则，覆盖 JSON 引号键、转义字段、截断值；敏感元数据项在限长之前剔除。
- Docker 输出采用严格结果字段与错误结构校验，异常内容不直接回显。
- Windows 进程通过创建时的 Job 属性原子归属，继承管道句柄采用白名单；无法确认停止时阻止新执行。
- 已确认停止的运行记录原子归档，保留审计内容，正常历史累计不再耗尽活跃恢复扫描额度。
- 源文件拒绝硬链接、重解析路径与身份变化；编辑结果从已经核验的前后字节重新生成补丁并匹配。

## 编辑与发布使用

`mode="review"` 仍是默认模式。`mode="edit"` 仅支持可信配置已启用 `allow_edits=true` 的 Windows desktop 项目。只修改任务副本，宿主采集真实变化，不自动合入原项目。

本次发布新增中文 MCP 连接教程、通用 Codex/Claude 配置示例、可复现的 Windows 源码 ZIP 与独立 Skill 包。源码 ZIP 无 `.git` 时初始化只授权人工 `sample` Git 项目；从 Git 克隆的根目录保留 `mcp` 自审查授权。已有本机配置和客户端片段保持不覆盖。

升级后应重新连接 MCP 服务或重启客户端，并核对提交工具实际包含 `mode`。磁盘文件更新不会替换已经运行的进程。已有模型引用、项目授权与客户端注册可以沿用，但应确认指向实际安装位置。

## 验证与边界

发布验证范围见 [VALIDATION.md](VALIDATION.md)，机器可读摘要见 [validation-v0.3.1.json](../artifacts/public/validation-v0.3.1.json)。Windows 桌面执行没有操作系统级文件或网络沙箱；Docker 实际构建/隔离、Linux/macOS runtime、Claude 完整模型流程和活动模型计费取消均未验收。

发布包不包含个人配置、凭证、任务数据和原始联调报告；只发布经过清单检查的源码、通用示例与验证摘要。
