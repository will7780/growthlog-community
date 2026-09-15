# 工程指南

## 本地与容器

Python 3.13、Node 20+、MySQL 8.0。正式运行使用 Docker CPU-only 依赖。
数据库/JWT 从环境读取，无内置真实凭据。frontend 的构建输出为 backend/static。
开发前运行 frontend/scripts/community_assets.mjs 生成可再分发图标，然后 npm run dev。
运行前须准备自己的 E5 和 API 配置；单元测试使用显式隔离配置。

## Schema

community_init init 拒绝非空库；创建所有 ORM 模型表、流式 claim 表及必要额外索引。
MySQL DDL 不是完整事务：中断可能留下不完整空库，应检查并重新创建专用测试库，
不能对已有用户数据反复运行初始化或使用自动 DROP。
community_init create-admin 只用于无用户库，使用隐藏密码输入。
无匿名注册或默认密码；后续用户由管理员创建。

历史 migration 是结构参考，不是从 001 按文件名全部执行的安装脚本。
空库初始化不读取外部 dump 或真实用户数据。升级必须按 schema、备份和针对性迁移单独设计。

## 公共接口与不变量

沿用现有 /api/auth、entries、todos、ai、notifications 和 integrations/notion。
本社区版本仅整理发布环境、资源和文档；任何安全修复在 CHANGELOG 单列。
根任务深度 0，最大子任务深度 3；同父节点排序，不改变父链。
完成父任务前须完成后代；创建或重开子任务会重开祖先。
统计和通知仅计算根任务。AI 引用区域只读。
引用必须验证当前用户、会话、token purpose 和当前来源资格。
Notion 原链接不是访问授权，Notion 仍校验访问者账户权限。

## 测试边界

Compose 冒烟测试仅作用于本次临时数据库，创建 community_test_ 前缀数据并精确清理。
公共 CI 无生产 Secrets，无线上部署任务。PR 来自外部时不给写权限。
AI 合约使用合成内容和 fake provider；单独真实 provider 验证必须明确声明，
不能把合约通过等同于外部服务已实测。禁止将个人语料用于公开截图或 CI。
公开运行应另配置 HTTPS、登录速率限制、容量配额、日志保留与数据备份。
