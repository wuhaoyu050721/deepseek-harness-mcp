# 实现与权限边界

本文描述 0.3.1 的服务结构。安装与使用见 [连接教程](MCP-CONNECTION.zh-CN.md)，实际验证范围见 [VALIDATION.md](VALIDATION.md)。

## 数据流

```text
MCP stdio 四工具
  -> TaskManager：单任务调度、总期限与取消
  -> JobStore：SQLite 幂等、容量、冻结配置与终态
  -> snapshot_worker / workspace：授权文件快照与 manifest
  -> edit_workspace：按需创建独立可写副本
  -> DesktopRunner：受监管进程 -> 已安装 Harness runtime -> SDK
     或 DockerRunner：容器 Worker 与租约监管（可选审查）
  -> edit_collect_worker：执行停止后比较实际文件
  -> report：引用校验、差异校验、脱敏、持久化摘要与分页
```

主项目不作为 runtime 工作目录。review 使用任务快照，edit 使用另外复制的工作区。stdout 仅输出 MCP 协议；子进程原文不混入协议。适配器只采集根会话最后 assistant 文本，reasoning/tool-call 不作为报告。

## 配置与文件

可信 TOML 决定 provider、model、base_url、凭证来源、项目范围与资源上限。工具只选择已有项目，不能传入任意本机目录或扩展权限。数据库冻结非秘密执行配置，凭证只存引用，执行时读取并在内存中登记实际凭证值供结果检查。

文件在 preparing 阶段捕获，默认取保存到工作树的 Git tracked 文件，未跟踪文件按可信配置逐个授权。路径校验拒绝绝对路径、上级、盘符、UNC、ADS、设备名、控制字符和歧义尾部；祖先与文件句柄身份核对 symlink、reparse 和多硬链接。凭证、私钥、依赖、缓存、常见二进制和备份排除规则不能识别全部硬编码秘密。

Git 元数据只使用固定只读 plumbing，禁用 lazy fetch、fsmonitor、hooks、replace objects 和可选锁。不会为检测 dirty 执行 status、diff 或仓库 filter。缺对象或 Git 缺必需能力时失败关闭。复制前后复读文件哈希，并核对 HEAD/index/tree 元数据变化；普通复制不构成操作系统原子快照。

## 任务与进程

同一数据目录只允许一个服务持锁。SQLite 事务先查原请求编号，再查执行阻断和容量；相同参数返回原任务，参数不同冲突。每实例一个执行位置，默认最多五个等待位置，FIFO。总期限包含准备、模型执行与差异采集；服务重启不自动重放任务。

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
