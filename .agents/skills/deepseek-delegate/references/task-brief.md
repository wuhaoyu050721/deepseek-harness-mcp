# 主 AI 的编码任务书

在方案已确定、准备调用 `mode="edit"` 时使用。只填写本批工作需要的信息，不复制整段用户对话或整个仓库。

```text
目标：
  [完成后的行为；本批重复项及差异]

主 AI 已确定的方法：
  [采用的模块/现有模式，以及为什么满足本任务]
  [按顺序列出实施步骤]
  [明确接口签名、输入输出、错误约定和必要边界条件]
  [遇到缺失信息时报告具体缺口，不自行改变业务规则]

范围：
  可修改：[相对路径；允许新增文件时注明目录]
  仅参考：[必要类型、入口、现有代表性实现]
  不修改：[本任务确实需要强调的限制]

参考模式：
  [代表性现有文件/符号；必要时提供一小段关键代码]
  [其余重复项的字段/名称/路径差异清单]

验收：
  [可观察的行为、边界、兼容要求]
  [需要新增/修改的测试代码，仅在有意义时列出]
  [主 AI 将运行的具体检查；DeepSeek 不运行]

执行要求：
  在 MCP 指定的独立工作副本内实际写文件，按上述方法完成本批实现。
  不运行项目程序、测试、构建、安装或 Git；不访问额外网络。
  不读取原项目、个人目录或凭证，不改执行配置，不再次委派。
  遵守 MCP 固定报告格式；说明实际修改和未解决项，不复制整份代码到摘要。
```

主 AI 不能只写“帮我实现最佳方案”“把这个功能做好”。如果主要设计仍由 DeepSeek 决定，就没有落实本 Skill 的分工。

## 例：按现有模式增加四个客户端方法

主 AI 已检查 `src/api/client.ts` 与类型定义，并确认现有统一请求方法 `request<T>(path, options)` 满足需求。

```text
目标：在 src/api/catalog.ts 实现商品列表、详情、创建和修改四个方法。

主 AI 已确定的方法：
1. 复用 src/api/client.ts 导出的 request<T>，不创建新请求封装。
2. 导出 listProducts(query: ProductQuery): Promise<ProductPage>，GET /products；
   使用现有 query 参数编码约定。
3. 导出 getProduct(id: string): Promise<Product>，GET /products/{id}；
   对路径 id 使用 encodeURIComponent。
4. 导出 createProduct(input: ProductInput): Promise<Product>，POST /products；
   input 作为请求 body。
5. 导出 updateProduct(id: string, input: ProductInput): Promise<Product>，
   PUT /products/{id}；同样编码 id，input 作为 body。
6. 从 src/types/catalog.ts 导入既有类型；错误透传由 request 统一处理，
   不捕获后返回空结果，不另存登录令牌。

可修改：src/api/catalog.ts。
仅参考：src/api/client.ts、src/api/orders.ts、src/types/catalog.ts。
不修改：公共请求方法、类型定义和认证处理。

验收：四个签名和请求参数符合约定，复用现有导出规范；
主 AI 合入后按项目现有方式执行类型检查及相关已有测试。
在独立副本实际写文件；不要运行这些检查。
```

对应 `relative_paths` 应包含上述参考文件与修改文件；如果 `catalog.ts` 是新文件，应将其已授权父目录 `src/api` 纳入范围。MCP 没有独立的 `write_paths` 参数，“仅参考”是任务书限制，主 AI 仍需拒绝对这些文件的实际改动。

如果缺少类型或公共请求约定，主 AI 先确定它们，不能把未定的接口选择藏进“其余由你处理”。

## 局部纠错

先将失败检查压缩为一个具体问题：目标文件/符号、期望、实际现象、最短相关日志、主 AI 确定的修复方法。新任务仍需明确范围，不能假设新会话记得原任务书。

如果新任务依赖上一批代码，先在授权范围内合入正确部分，再捕获新的项目版本；不要假定上一作业的独立副本会自动成为新作业的输入。
