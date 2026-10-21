# Arclight

面向 AI 漫剧生产的模块化后端，依据 `docs/technical-design-v1.2.md` 搭建。
当前交付是 **后端基础工程 + 剧本/分镜审核链路**，不是设计文档中全部功能的完成版。

## 已实现

- Python 3.12、uv 依赖锁定、FastAPI/OpenAPI、Pydantic v2。
- SQLAlchemy 2 + PostgreSQL，业务表通过 **Alembic** 迁移，启动时不调用 `create_all()`。
- 项目/剧集创建与分页查询，项目归属校验，统一错误响应和 request ID。
- LangGraph 编剧 → 剧本审核 → 导演 → 分镜审核，审核使用 `interrupt` / `Command`。
- PostgreSQL 持久化 checkpoint，同一 run 使用稳定 thread ID；SQLite 供单进程开发和测试。
- 剧本、分镜版本及镜头持久化；审核必须绑定具体版本和运行 revision。
- 事务 Outbox、Celery/Redis 独立 Worker、重复消息消除、运行事件、取消和显式失败重试。
- Mock 文本 Provider，以及输出经 Pydantic 校验的 OpenAI-compatible Provider。
- Docker Compose、格式/静态检查、SQLite 与 PostgreSQL 自动化测试、GitHub Actions 配置。

## 架构

```text
Client → FastAPI → Application → PostgreSQL（业务表 / Outbox）
                       │
Celery Beat → Outbox Dispatcher → Redis → Celery Worker
                                           │
                                   LangGraph + Text Provider
                                           │
                         PostgreSQL（checkpoint / 版本 / 事件）
```

业务状态以 PostgreSQL 为准。Redis 不承载正式资产或审核状态；API 不在请求内执行模型长任务。

## 快速启动：Docker Compose

需要 Docker Compose。首次启动会下载镜像并构建 Python 环境。
默认凭据和端口绑定只适用于本机开发，**不要直接部署到公网**。

```powershell
Copy-Item ".env.example" ".env"
uv sync --frozen
# 启动新开发环境时，这条命令会执行 Alembic 与 checkpoint 初始化。
docker compose up --build -d
```

- Swagger：`http://localhost:8000/docs`
- 存活探针：`http://localhost:8000/health/live`
- 数据库连接探针：`http://localhost:8000/health/ready`
- API 使用 Bearer Token；默认开发值为 `local-development-token-change-me`。
- Compose 的 `migrate` 是一次性服务；API/Worker 等迁移成功后启动。

查看日志与正常停止（保留数据卷）：

```powershell
docker compose logs -f api worker beat
docker compose down
```

不要对有用数据执行 `docker compose down -v`。迁移到已有数据库前先备份并审阅迁移。

## 本地 Python 开发

先启动依赖服务，再显式迁移：

```powershell
uv sync --frozen
Copy-Item ".env.example" ".env"
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python -m arclight.workflows.checkpoints
uv run uvicorn arclight.main:app --reload
```

其他终端在相同项目目录分别启动：

```powershell
# Windows 本机使用 solo；Linux/Docker 可使用默认 prefork。
uv run celery -A arclight.workers.celery_app:celery_app worker --pool=solo --loglevel=info
uv run celery -A arclight.workers.celery_app:celery_app beat --loglevel=info --schedule=celerybeat-schedule
```

也可完全不运行 PostgreSQL/Redis：编辑 `.env` 中以下两项，保持 `ARCLIGHT_PROVIDER=mock`：

```dotenv
ARCLIGHT_DATABASE_URL=sqlite:///./arclight.db
ARCLIGHT_CHECKPOINT_URL=sqlite:///./checkpoints.db
```

然后执行迁移、checkpoint 初始化和 API 启动命令。每次创建或恢复 run 后，用以下命令手动推进一次：

```powershell
uv run python -m arclight.workers.local "替换为实际-run-UUID"
```

SQLite 仅支持此处的单进程演示；生产及并发 Worker 必须使用 PostgreSQL。

## 演示链路

1. `POST /api/v1/projects` 创建项目。
2. `POST /api/v1/projects/{project_id}/episodes` 创建剧集。
3. `POST /api/v1/episodes/{episode_id}/script-runs`，携带 `Idempotency-Key`，正文为 `{"story":"雨夜，主角收到一封匿名来信。"}`，返回 202 和 run ID。
4. Worker 执行后，`GET /api/v1/runs/{run_id}` 返回 `WAITING_REVIEW` 和 `pending_review`。
5. 查询 `/api/v1/episodes/{episode_id}/scripts` 阅读对应版本。
6. `POST /api/v1/runs/{run_id}/resume` 提交审核；不能省略版本 ID 和 revision：

```json
{
  "resource_type": "script",
  "version_id": "复制 pending_review.version_id",
  "decision": "APPROVE",
  "comment": "剧本通过",
  "expected_revision": 1
}
```

7. 再次推进后暂停在分镜审核。查询 `/api/v1/episodes/{episode_id}/storyboards`，以及 `/api/v1/episodes/{episode_id}/shots?storyboard_id={version_id}`。
8. 再调用 resume，将 `resource_type` 设为 `storyboard`，使用本次返回的版本 ID 和 revision。
9. 推进后 run 变成 `SUCCEEDED`。任意审核 `REJECT` 均终止为 `REJECTED`；驳回后可用新幂等键启动新 run。
10. `/api/v1/runs/{run_id}/events` 查询事件；排队/等待审核可调用 `/cancel`；失败可主动调用 `/retry`，外部模型调用可能再次产生费用。

Swagger 的 **Authorize** 输入开发 Token 后即可执行上述接口。具体 API 列表以生成的 OpenAPI 为准。

## Alembic 数据库版本管理

```powershell
uv run alembic current
uv run alembic history
uv run alembic revision --autogenerate -m "describe schema change"
# 必须审阅新迁移后再执行。
uv run alembic upgrade head
uv run alembic check
# downgrade 可能丢失数据，确认备份与影响后才可使用。
# uv run alembic downgrade -1
```

- 初始迁移：`apps/api/migrations/versions/0001_initial_production_foundation.py`。
- 后续修改模型必须新增迁移，不要回改已部署的 `0001`。
- checkpoint 内部表由 LangGraph 官方 `setup()` 迁移，不纳入业务 Alembic；更新依赖后显式运行 checkpoint 初始化。
- `alembic check` 只管理本应用声明的业务表，不尝试删除 checkpoint 或其他工具的内部表。

## 切换真实文本模型

在 `.env` 中设置：

```dotenv
ARCLIGHT_PROVIDER=openai_compatible
ARCLIGHT_LLM_BASE_URL=https://your-provider.example/v1
ARCLIGHT_LLM_MODEL=your-structured-output-model
ARCLIGHT_LLM_API_KEY=your-private-key
```

适配器要求 Chat Completions 与 `response_format=json_schema` 能力；并非所有“兼容”服务都实现该能力。
该接口未经真实付费服务联调。不会自动降级为 Mock，不会对超时进行隐式付费重试。
模型输入会发送给配置的服务商；不要在未获授权时输入敏感素材。

## 质量检查

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

测试从 Alembic 空库迁移开始，覆盖升降级与元数据一致性、引用校验、鉴权、幂等冲突、版本绑定审核、checkpoint 恢复、重复投递、失败与显式重试、驳回和取消。

如需 PostgreSQL 测试，使用**专用空测试数据库** `arclight_test`：

```powershell
$env:ARCLIGHT_TEST_DATABASE_URL="postgresql+psycopg://user:password@localhost:5432/arclight_test"
uv run pytest -q
```

每个测试创建独立 schema；只允许数据库名 `arclight_test`，不应指向业务数据库。测试 schema 留待专用测试环境销毁。

## 目录

```text
apps/api/src/arclight/
  api/             路由、请求响应 Schema、鉴权与事务依赖
  application/     项目权限、运行命令、产物写入、执行服务
  agents/          编剧/导演的结构化输出合同
  core/            环境配置、领域异常
  domain/          状态定义
  persistence/     SQLAlchemy 模型、会话、元数据
  providers/       Mock 与真实文本服务适配器
  workflows/       LangGraph 生产图与 checkpoint 初始化
  workers/         Celery、Outbox 分发、单进程执行入口
apps/api/migrations/  Alembic 环境与迁移版本
tests/                API、工作流、合同、迁移测试
docs/                 原始设计、架构与实施边界
```

## 当前边界

- **未实现**前端、用户注册/JWT/多成员 RBAC、图片/语音/视频生成、MinIO 上传、Continuity Reviewer、时间轴/FFmpeg、依赖 DAG 失效传播、预算扣费与 Langfuse/OTel 完整集成。
- `visual_assets` / `asset_versions` / `asset_dependencies` 已建立最小数据库结构，但没有开放写入或选用 API；不代表资产服务已经实现。
- 当前只有单租户服务 Token 身份，不接受客户端任意指定用户 ID；生产前必须补齐身份系统、TLS、限流、密钥管理及审计策略。
- 内容版本不被当前服务覆写，审核状态另行推进；尚未增加阻止数据库直写的不可变触发器。
- 工作流推进期间持有 PostgreSQL 运行行锁，适合当前文本原型。运行中的模型请求不能即时取消，媒体长任务必须使用后续独立生成调度器。
- Outbox 会重复投递 QUEUED run；内部通过 revision、状态与行锁去重。模型调用与数据库/checkpoint 无法组成同一事务，崩溃时不能承诺外部调用 exactly-once。
- 健康 ready 仅检查数据库连接，不代表 Redis、模型服务或未来对象存储已经就绪。

架构与实现约束详见 `docs/architecture.md`。详细待办清单仅在本地维护，不纳入 Git 版本管理。
