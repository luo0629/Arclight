# Arclight 开发与运行指南

本指南包含前后端启动、接口演示、数据库迁移、模型配置和质量检查。
项目定位与能力介绍见 [项目首页](../README.md)，架构约束见 [架构说明](architecture.md)。

除特别注明外，每个小节从仓库根目录开始；后端命令在 `backend/` 执行，前端命令在 `fronted/` 执行，Compose 在根目录执行。若已有 `.env`，请保留并调整现有配置，不要直接覆盖。

根目录 `.env` 用于 Compose 变量替换；本机运行后端时，从公共示例创建 `backend/.env`，由后端进程加载。两者均被 Git 忽略，不要填写真实密钥后提交。

## 快速启动：Docker Compose

需要 Docker Compose。首次启动会下载镜像并构建 Python 环境。
默认凭据和端口绑定只适用于本机开发，**不要直接部署到公网**。

```powershell
Copy-Item ".env.example" ".env"
# 启动新开发环境时，这条命令会执行 Alembic 与 checkpoint 初始化。
docker compose up --build -d
```

- 前端工作台：`http://localhost:8080`，通过 Nginx 同源代理 API；首次访问创建账号。
- Swagger：`http://localhost:8000/docs`
- 存活探针：`http://localhost:8000/health/live`
- 数据库连接探针：`http://localhost:8000/health/ready`
- 工作台使用账户登录：Access Token 仅放内存，刷新凭证使用 HttpOnly Cookie。
- API 调试仍可使用开发 Service Token：`local-development-token-change-me`；生产模式禁用此身份。
- Compose 的 `migrate` 是一次性服务；API/Worker 等迁移成功后启动。

查看日志与正常停止（保留数据卷）：

```powershell
docker compose logs -f web api worker beat
docker compose down
```

不要对有用数据执行 `docker compose down -v`。迁移到已有数据库前先备份并审阅迁移。

## 本地 Python 开发

先启动依赖服务，再显式迁移：

```powershell
docker compose up -d postgres redis
Set-Location "backend"
uv sync --frozen
Copy-Item "../.env.example" ".env"
uv run alembic upgrade head
uv run python -m arclight.workflows.checkpoints
uv run uvicorn arclight.main:app --reload
```

其他终端先进入 `backend/`，再分别启动：

```powershell
# Windows 本机使用 solo；Linux/Docker 可使用默认 prefork。
uv run celery -A arclight.workers.celery_app:celery_app worker --pool=solo --loglevel=info
uv run celery -A arclight.workers.celery_app:celery_app beat --loglevel=info --schedule=celerybeat-schedule
```

也可完全不运行 PostgreSQL/Redis：编辑 `backend/.env` 中以下两项，保持 `ARCLIGHT_PROVIDER=mock`：

```dotenv
ARCLIGHT_DATABASE_URL=sqlite:///./arclight.db
ARCLIGHT_CHECKPOINT_URL=sqlite:///./checkpoints.db
```

然后执行迁移、checkpoint 初始化和 API 启动命令。每次创建或恢复 run 后，用以下命令手动推进一次：

```powershell
uv run python -m arclight.workers.local "替换为实际-run-UUID"
```

SQLite 仅支持此处的单进程演示；生产及并发 Worker 必须使用 PostgreSQL。

## 本地前端开发

需要 Node.js 24 和 npm。在另一个终端从仓库根目录执行：

```powershell
Set-Location "fronted"
npm.cmd ci
npm.cmd run dev
```

访问 `http://localhost:5173`。Vite 默认将 `/api`、`/health` 转发至本机 8000 端口，保留原始 Host 与 Origin；API 与 Worker 请按上一节启动。需要更换目标时设置进程环境变量 `ARCLIGHT_API_PROXY`。

界面体验顺序：注册 → 创建项目 → 添加剧集 → 输入故事 → 发起编剧任务 → 任务中心批准剧本 → 批准分镜 → 在画布／版本列表查看结果。人工编辑会另存新版本，不覆盖旧内容；工作流审核与人工版本审核是独立入口。图片与成片页面会明确提示尚未实现。

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

以下命令在 `backend/` 执行；迁移配置为 `backend/alembic.ini`。目录迁移不改变数据库 revision，不需要新增空迁移或重建数据库。

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

- 初始迁移：`backend/migrations/versions/0001_initial_production_foundation.py`。
- 第二版迁移 `0002` 增加账户、会话、成员、审计、画布布局与人工内容版本支持。
- 后续修改模型必须新增迁移，不要回改已部署的历史版本。
- 存在不关联 run 的人工版本时，回退到 `0001` 会在任何 DDL 前拒绝执行，避免删除内容或伪造关联。
- checkpoint 内部表由 LangGraph 官方 `setup()` 迁移，不纳入业务 Alembic；更新依赖后显式运行 checkpoint 初始化。
- `alembic check` 只管理本应用声明的业务表，不尝试删除 checkpoint 或其他工具的内部表。

## 切换真实文本模型

本机后端在 `backend/.env` 中设置；Compose 在根目录 `.env` 中设置：

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
Set-Location "backend"
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

## 浏览器端到端验收

先在 `backend/` 运行 `uv sync --frozen` 创建后端虚拟环境，再从仓库根目录执行：

```powershell
Set-Location "fronted"
npm.cmd ci
npm.cmd run build
# 默认使用已安装的 Chrome；CI 使用 Playwright Chromium。
# 如在本地缺少 Chrome，可执行：npx playwright install chrome
npm.cmd run test:e2e
```

测试自动启动临时 SQLite + Mock API（8766）与 Vite（4177），不连接 `.env` 里的业务数据库，也不调用收费模型。覆盖注册、弹窗键盘焦点与 Escape、创建项目剧集、双审核、画布保存、人工新版本、刷新会话与退出，以及 390px 移动端无横向溢出。协作测试覆盖添加成员、只读拒绝写入、切换编辑角色、项目修改／归档恢复、移除后隐藏项目。第三条测试从空白人工剧本开始，保存并独立审核后启动精确版本导演任务，只经历分镜审核，并核对没有新增第二份剧本。截图与失败 trace 位于 `fronted/test-results/`，已忽略，不提交。

这条测试使用本地执行器，不替代 PostgreSQL / Redis / Celery 的容器联合验收。GitHub Actions 分别运行后端 SQLite/PostgreSQL 测试和前端构建、浏览器测试；配置存在不等于远程执行已经成功。

## 隔离 Docker 联合验收

安装前端依赖并构建默认镜像后，在仓库根目录执行：

```powershell
docker compose build web api
uv run --directory "backend" python "scripts/verify_compose.py"
```

脚本使用本地 `arclight-web:latest` / `arclight-api:latest` 镜像，创建随机 `arclight-verify-*` Compose 项目、独立网络和两个专用测试卷。PostgreSQL 使用该隔离实例内的 `arclight_test`，不发布数据库／Redis／API 端口，Nginx 使用本机随机端口；固定使用 Mock、随机 JWT 密钥，不调用收费服务。本机 HTTP 探测不继承环境或系统代理，避免 loopback 请求误走代理；不会修改系统代理设置。

验收包括空库迁移、checkpoint 初始化、Nginx 配置和安全头／缓存策略，以及通过真实 PostgreSQL、Redis、Worker、Beat 完成三条浏览器链路。无论成功失败，脚本都尝试清理**仅本次随机项目**的容器和测试卷；拒绝 bind mount 或外部卷混入。运行前应明确同意这项测试资源清理，不要将脚本改为指向业务项目。若终端被强制终止，使用日志中的确切随机项目名检查残留资源，不能全局 prune。

## 部署与代理信任边界

默认 Compose 仅供本机开发：前端 8080、API 8000、PostgreSQL 5432、Redis 6379 均仅绑定 loopback。Nginx 覆盖客户端传来的转发 IP／协议头，并对账户 POST 请求做网关限流；API 另有请求体上限与进程内账户限流。

`ARCLIGHT_TRUSTED_PROXY_IPS` 默认 `*`，用于受控本地 Compose 网关。**这不是公网推荐值**：公网部署必须限制为真实代理 IP／网段，隔离 API 与数据库网络，配置 HTTPS、独立高强度 JWT 密钥、精确的 `ARCLIGHT_AUTH_ALLOWED_ORIGINS`，并按需要关闭 `ARCLIGHT_ALLOW_REGISTRATION`。若有外层 TLS 终止代理，须显式配置浏览器来源；不要盲目信任用户提交的转发头。仅在 Nginx 或本机设置限流，不等于分布式全局限流已经完成。

## 当前边界

- **未实现**图片/语音/视频生成、MinIO 上传、Continuity Reviewer、时间轴/FFmpeg、资产依赖 DAG 失效传播、预算扣费与 Langfuse/OTel 完整集成。
- 已实现账户/JWT/多成员 RBAC、画布、人工内容编辑与审核、从零人工起草剧本及从指定已批准剧本单独启动导演；从零人工起草分镜的前端入口仍待补齐。
- `visual_assets` / `asset_versions` / `asset_dependencies` 已建立最小数据库结构，但没有开放写入或选用 API；不代表资产服务已经实现。
- 已有账户与可撤销会话、Owner/Editor/Viewer 权限及关键操作审计，但 OIDC、多进程全局限流、完整运营与合规策略仍未接入。
- 内容版本不被当前服务覆写，审核状态另行推进；尚未增加阻止数据库直写的不可变触发器。
- 工作流推进期间持有 PostgreSQL 运行行锁，适合当前文本原型。运行中的模型请求不能即时取消，媒体长任务必须使用后续独立生成调度器。
- Outbox 会重复投递 QUEUED run；内部通过 revision、状态与行锁去重。模型调用与数据库/checkpoint 无法组成同一事务，崩溃时不能承诺外部调用 exactly-once。
- 健康 ready 仅检查数据库连接，不代表 Redis、模型服务或未来对象存储已经就绪。
