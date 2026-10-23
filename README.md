# Arclight

**面向 AI 漫剧与视觉叙事的多智能体创作项目。**

让故事创作成为可审核、可追踪、可恢复的生产流程，而不是一组彼此独立的模型调用。

Arclight 面向个人创作者、小型漫剧工作室和 AI 内容制作团队，目标是连接故事、剧本、分镜、视觉素材与最终成片。创作者负责方向与关键决策，专业 Agent 承担具体创作任务，服务端负责状态、版本、审核和执行。

> **当前阶段：可用的文本创作工作台。**
> 已实现可爱风画布前端、账户与项目协作，以及故事 → 剧本 → 分镜的双人工审核流程。图片、配音和成片生产仍在开发中。

## 为什么做 Arclight

AI 内容创作的难点不只在于生成一段文字或一张图片，还在于如何把不同环节组织成可持续修改、协作和交付的过程：

- **创作结果需要成为正式产物。** 剧本、分镜和镜头信息不能只存在于聊天记录中，需要保存、查询并关联具体版本。
- **关键决定需要由人确认。** 剧本和分镜是否通过，应当是明确的审核动作，而不是模型自行跳过的步骤。
- **中断不应丢失全部进度。** 工作流需要记录执行位置，支持等待审核、重启恢复和失败后的显式重试。
- **扩展模型不应改写业务流程。** 生成能力与业务逻辑分离，避免工作流绑定某个供应商的 SDK。

Arclight 先把文本创作与审核链路做实，再逐步向完整的漫剧生产流程扩展。

## 当前可以做什么

### 在贴纸画布上整理故事

React + TypeScript 工作台统一使用 **shadcn/ui（Radix）组件与 Tailwind CSS v4**，采用纸粉、淡紫与薄荷色的「贴纸创作本」风格。左侧管理项目与剧集，中间画布展示剧集、剧本、分镜和镜头，右侧阅读或编辑具体内容。

节点可以拖动，布局与视角保存到服务端；依赖连线来自真实版本关系，不能手工连线伪造审核结果。也可切换版本列表，不依赖画布操作；小屏幕保留列表与编辑入口。

注册登录后即可创建项目、添加剧集、输入故事。任务中心展示状态与事件，提供人工审核、取消和失败重试。项目拥有者可添加已注册伙伴，分别授予编辑或只读权限；设置页支持项目配置与归档恢复。

### 从故事生成剧本与分镜

以项目和剧集组织创作内容。输入故事后，由编剧 Agent 生成结构化剧本；剧本通过人工审核后，再由导演 Agent 生成结构化分镜和镜头信息。

生成结果通过 Pydantic 合同校验并保存到数据库，分镜关联具体剧本版本，便于后续查询和核对。

也可以选择“自己写剧本”，从空白表单保存人工版本并审核；“自己写分镜”允许选择精确的已批准剧本手工起稿。人工保存成功后自动打开回执对应的新版本并显示待审核状态，不会误选并发新稿；读取刷新失败可单独重试，无需重复保存。对任意已批准的剧本，点击“从此版本生成分镜”即可单独启动导演任务：只使用选定版本，不重新调用编剧；分镜生成后仍需人工审核。

### 在关键节点保留创作控制权

剧本与分镜各有独立的人工审核节点。审核绑定具体内容版本和运行 revision，避免将过期审核结果应用到另一份内容。

审核通过后继续执行，驳回则终止当前运行。支持保存人工新版本并由项目拥有者审核；工作流生成版本仍必须通过对应任务审核，不可绕过版本绑定。

### 查看与恢复工作流

每次创作任务都有独立的运行记录、状态和业务事件。LangGraph checkpoint 保存编排进度，同一任务使用稳定的 thread ID，在人工审核后恢复原有流程。

后台任务支持幂等请求、重复消息去重、排队／待审核运行取消，以及失败后的显式重试。外部模型调用不承诺 exactly-once，重试可能产生额外调用费用。

### 在无模型密钥的环境中演示

默认 Mock Provider 可以演示完整的文本审核流程。仓库也提供 OpenAI-compatible 文本适配器，使用结构化输出协议接入模型服务；真实供应商仍需配置和联调。

## 创作流程

```text
创建项目与剧集
      ↓
   输入故事
      ↓
编剧 Agent → 保存剧本版本
      ↓
  人工审核剧本 ── 驳回 → 结束
      ↓ 通过
导演 Agent → 保存分镜与镜头
      ↓
  人工审核分镜 ── 驳回 → 结束
      ↓ 通过
  本次创作任务完成
```

这里的“完成”指剧本／分镜任务完成，不代表图片、配音或视频已经生成。

## 设计思路

| 原则 | 在当前工程中的体现 |
| --- | --- |
| 内容优先，而非聊天优先 | 剧本、分镜和镜头独立持久化，不依赖对话历史保存业务事实 |
| 模型负责创作，代码负责执行 | Agent 输出经过结构校验，状态迁移、审核和版本一致性由服务端控制 |
| 人工审核是流程的一部分 | 使用 LangGraph `interrupt` 暂停，通过 `Command` 恢复原任务 |
| 数据库记录业务事实 | PostgreSQL 保存运行、内容和审核，Redis 仅承载后台任务队列 |
| 生成能力可以替换 | Provider 适配层与 API、工作流及持久化逻辑分离 |
| 先完成明确链路 | 采用模块化单体 + 独立 Worker，不提前拆分空微服务 |

## 技术架构

```text
React 画布工作台 / OpenAPI
       │
       ▼
    FastAPI ──► PostgreSQL（业务数据 / 内容版本 / 审核 / Outbox）
                       │
                Celery Beat 扫描 Outbox
                       │
                       ▼
                     Redis
                       │
                       ▼
                 Celery Worker
                       │
                       ▼
                   LangGraph ──► Text Provider
                       │
                       ▼
                PostgreSQL Checkpoint
```

API 负责接收请求和查询结果，耗时生成在独立 Worker 中执行。业务表通过 **Alembic** 管理版本；LangGraph checkpoint 内部表通过官方持久化组件单独初始化。

| 层次 | 技术 |
| --- | --- |
| 前端 | React、TypeScript、Vite、shadcn/ui（Radix）、Tailwind CSS v4、React Flow |
| 语言与依赖 | Python 3.12、uv |
| API 与数据校验 | FastAPI、Pydantic v2 |
| Agent 编排 | LangGraph、持久化 checkpoint |
| 业务存储与迁移 | PostgreSQL、SQLAlchemy 2、Alembic |
| 后台任务 | Celery、Redis |
| 本地运行与检查 | Docker Compose、pytest、Ruff、mypy、GitHub Actions 配置 |

SQLite 用于单进程开发和测试，不作为生产或并发 Worker 的数据库方案。

## 当前范围

后端现已提供账号注册登录、可撤销 JWT 会话、项目 Owner/Editor/Viewer 权限、人工内容版本与审核，以及画布数据和布局 API。具体权限与接口见 [账户与内容说明](docs/accounts-and-content.md)。

图片／视频／TTS 生成、连续性审查、对象存储、时间轴及 FFmpeg 成片尚未实现。资产相关表仅有基础结构，不代表资产管理服务已经完成；预算扣费与完整可观测性也尚未接入。

当前适合本地演示、架构验证和后续开发，**不应按完整生产系统直接部署到公网**。详细约束见 [架构说明](docs/architecture.md) 与 [开发指南](docs/development.md#当前边界)。

## 快速体验

安装 Docker Compose 后，在仓库根目录执行。若已有 `.env`，请保留现有文件并按示例调整配置。

```powershell
Copy-Item ".env.example" ".env"
docker compose up --build -d
```

- 创作工作台：`http://localhost:8080`，首次访问请创建账号。
- Swagger：`http://localhost:8000/docs`
- 默认使用 Mock，无需模型密钥；不会生成真实图片或视频。
- API 调试可使用开发 Service Token：`local-development-token-change-me`，浏览器工作台使用自己的账户会话。
- 首次启动会执行业务表迁移和 checkpoint 初始化，默认凭据仅适用于本机开发。

本地 Python 开发、双审核接口演示、真实模型配置和测试命令见 [开发与运行指南](docs/development.md)。

## 项目结构

```text
Arclight/
├── backend/                 # FastAPI、LangGraph 与后台任务
│   ├── src/arclight/        # API、业务服务、Agent、工作流与持久化
│   ├── migrations/         # Alembic 历史迁移
│   ├── tests/              # 后端单元测试与集成测试
│   ├── scripts/            # 浏览器测试服务与隔离 Compose 验收
│   ├── alembic.ini
│   ├── pyproject.toml
│   ├── uv.lock
│   └── Dockerfile
├── fronted/                 # React + TypeScript 前端（目录名为 fronted）
│   ├── src/
│   ├── tests/              # Playwright 浏览器测试
│   ├── package.json
│   ├── package-lock.json
│   ├── nginx.conf
│   └── Dockerfile
├── docs/                    # 设计、架构与开发文档
├── .github/workflows/       # 前后端 CI
├── .env.example             # 本地配置示例
└── compose.yaml             # 前后端与数据库等依赖的统一编排
```

Python 依赖安装、后端启动和 Alembic 命令在 `backend/` 执行；前端 npm 命令在 `fronted/` 执行；Docker Compose 在仓库根目录执行。数据库和 Redis 仍由 Compose 提供，目录调整不会改变服务名、端口或数据卷。

## 项目文档

- [账户与内容](docs/accounts-and-content.md)：权限、不可变版本、审核与接口。
- [界面约定](docs/ui-design.md)：可爱画布的布局、颜色与交互边界。

- [技术设计 v1.2](docs/technical-design-v1.2.md)：产品目标、完整生产流程与数据模型设计；不代表所有能力已实现。
- [架构与约束](docs/architecture.md)：分层、持久化、恢复机制及安全边界。
- [开发与运行指南](docs/development.md)：环境启动、API 演示、Alembic 迁移、模型配置及质量检查。

## 账号模型服务配置

顶栏“模型服务配置”可分别保存文字、图片、视频和语音的地址与模型预设。文字服务默认继承服务器配置，只有用户显式启用自定义文字服务后，新任务才使用该配置。图片、视频、语音当前仅保存预设，不支持生成；真实收费 Provider 联调仍待授权验收。

- 部署新版前按既有流程备份并执行 `alembic upgrade head`（新增迁移 `0003`）；开发代理需保持 `/api/v1` 同源。
- 保存 API Key 前须配置独立 `ARCLIGHT_SERVICE_ENCRYPTION_KEY`：以 `cryptography.fernet.Fernet.generate_key()` 生成，API 与 Worker 使用相同值。密钥不与密文同库存储，不写入 Git，必须安全备份；直接更换会使已保存服务和未结束任务的配置快照无法解密。未配置时仍可保存无密钥预设。
- `ARCLIGHT_MODEL_SERVICE_ALLOWED_HOSTS` 是管理员信任的精确公网 HTTPS 域名 JSON 列表，默认仅 `api.openai.com`。不要加入不受信任或解析到内网的域名；禁止 IP、HTTP、URL 凭证、查询参数、非443端口。HTTP 客户端不跟随重定向；执行任务时再次检查允许列表，管理员撤销域名后，未结束任务可能被阻止。
- 配置按账号隔离、revision 防止陈旧覆盖。读取接口只有 `has_key`，不返回密钥或密文；浏览器不持久化 API Key。工作流启动时捕获加密配置快照，后续修改不会改变等待审核及重试任务；凭证不进入提示词、checkpoint、公开任务详情或审计事件。
- 保存不测试连接、不产生模型费用。启用自定义文字服务后，主动创建的任务将发送故事素材并可能收费，请先确认数据与预算。

## 许可证

本项目采用 [MIT License](LICENSE)。
