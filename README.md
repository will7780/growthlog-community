# GrowthLog Community

可自托管的成长工作台：记录、小要事、Notion 与可追溯 AI 助手。

**Self-hosted personal knowledge workspace with traceable AI assistants, hybrid retrieval,
versioned evidence, Notion integration and ordered subtasks.** Chinese-first interface.
Licensed under AGPL-3.0-only.

## 功能

- 检索鼠：问题理解 → 文档发现或正文检索 → RAG 准入 → 一次语义选择、归纳和引用。
- 讲解鼠：确认来源范围后持续追问，显式补充来源才创建新版本。
- 来源统一支持记录、附件、三级任务树和 Notion；引用可预览，Notion 可打开原页。
- 记录族、多附件 OCR/PDF/PPTX、个性设置、自动会话压缩和 PWA Web Push。
- 主任务优先级和今日计划；子任务按人工顺序展示。
- 整理鼠处于维护态，暂不提供新建整理操作。

源码基线 11.13.2；社区首版 11.13.2-community.1。无真实用户数据、生产配置或默认账号。

## 快速启动（Docker Compose）

需要 Docker Engine / Desktop、Compose v2、Python 3；建议至少 8 GB 可用内存和 15 GB 可用磁盘。
CPU-only 构建首次需要下载 Python 依赖与 E5，速度取决于网络。数据库和模型分别持久化。

```sh
git clone https://github.com/will7780/growthlog-community.git
cd growthlog-community
python scripts/setup_env.py
docker compose build app
docker compose up -d db
docker compose run --rm app python -m scripts.community_init init
docker compose run --rm app python -m scripts.community_init create-admin
docker compose run --rm prepare-model
docker compose up -d app worker notification-worker
```

打开 http://localhost:8000，以刚创建的管理员登录。创建更多账户使用产品管理员页面。
首次初始化只接受空库；重复执行会明确拒绝，不会删除或覆盖已有数据。
MySQL 不发布主机端口，应用默认仅监听 127.0.0.1。不要用此命令序列升级已有实例。

编辑本地 .env 设置自己的 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL，
然后 `docker compose up -d --force-recreate app worker notification-worker`。
未设置模型 API 时仍可管理记录与任务，AI 生成不能完成。
密钥由 setup_env 自动生成到忽略的 .env，不会打印。勿提交或分享该文件。

E5 使用固定 revision、768 维、CPU 验证；模型目录只读挂载。准备阶段可联网，
业务运行阶段禁止模型自动下载。更换模型涉及向量兼容性，不能直接覆盖已有索引。

## Notion 与通知（可选）

- 自建 Notion OAuth connection，配置自己的 client ID、secret、公开 HTTPS callback
  `https://你的域名/api/integrations/notion/oauth/callback` 和独立 32 字节加密密钥。
- 设置 NOTION_INTEGRATION_ENABLED=true；用户从个人中心授权自己的页面。
- 配置 webhook 时验证签名；不给任何未授权页面读权限。同步状态和引用权限由服务端检查。
- Web Push 需要公开 HTTPS、VAPID 密钥和订阅加密密钥。iPhone 需要添加到主屏幕后使用。
- 公开部署自行配置反向代理 HTTPS、登录/API 限流、备份与监控；示例不附带托管服务凭据。

## 设计与验证

- [AI 与系统架构](docs/ARCHITECTURE.md)
- [工程和数据初始化](docs/ENGINEERING.md)
- [第三方许可](THIRD_PARTY_NOTICES.md)
- [安全报告](SECURITY.md)

CI 从源码构建 CPU-only 镜像，在临时 Compose 中验证空库、真实 HTTP、任务树、
来源权限及 AI 合约。合约中的 fake provider 只使用合成数据，不表示真实 Notion OAuth 已验证。
测试覆盖范围与实际运行结果见 GitHub Actions；没有通过的检查不会标记完成。

## 开源与自托管

Copyright (C) 2026 GrowthLog contributors. 本项目采用 AGPL-3.0-only，见 [LICENSE](LICENSE)。
软件许可不授予任何托管实例的访问权限，也不包含用户数据或密钥。
修改后对外提供网络服务时，应遵守 AGPL 对相应源码提供等要求。
默认界面包含社区源码入口；部署修改版时必须指向该实例实际运行版本的相应源码。
第三方依赖和模型遵守各自许可证。托管运营所需备份由部署者负责。
