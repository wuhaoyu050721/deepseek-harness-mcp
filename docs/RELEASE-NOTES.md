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
