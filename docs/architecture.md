# 架构与约束

## 分层

- `api` 负责 HTTP 校验、服务令牌身份、事务依赖及响应序列化。
- `application` 负责项目权限、运行创建/审核/取消/重试、数据库产物和图推进。
- `agents` 提供结构化剧本与分镜合同；`providers` 实现生成能力，业务服务不依赖具体模型 SDK。
- `workflows` 负责确定性路由和人工门禁，不直接在审核节点写数据库。
- `persistence` 统一 ORM、JSONB/SQLite JSON 类型、命名约定、UTC 时间与会话。
- `workers` 负责异步任务入口及 Outbox 分发。

采用同步 SQLAlchemy / LangGraph。FastAPI 同步路由在线程池运行；模型请求只在独立 Worker 或显式本地执行器中发起，不阻塞 API 事件循环。

## 工作流

```text
START → screenwriter → script_review [interrupt]
                            ├─ REJECT → END / REJECTED
                            └─ APPROVE → director → storyboard_review [interrupt]
                                                         ├─ REJECT → END / REJECTED
                                                         └─ APPROVE → END / SUCCEEDED
```

审核接口先检查身份、run 状态、revision、对象类型与具体版本，再在一个业务事务中保存 Review、推进产物状态、保存 resume payload，并插入 Outbox。
Worker 以相同 run UUID 作为 `thread_id`，用 `Command(resume=...)` 推进现有图，不会创建一个新的 thread。

工作流版本固定为 `1.0`；不兼容的版本拒绝自动推进。未来修改状态合同或节点名称必须提供工作流版本兼容/迁移策略，而不是修改运行中的旧图后强行恢复。

## 数据持久化

初始 Alembic 版本建立 12 张业务表：

- projects、episodes。
- workflow_runs、workflow_events、outbox_events、reviews。
- scripts、storyboards、shots。
- visual_assets、asset_versions、asset_dependencies（仅结构，不提供业务写入接口）。

每个剧本/分镜版本都绑定 run；分镜引用精确的剧本版本。内容保存后不被本服务覆盖；审核只改变状态。
故事、风格规范及目标时长在创建 run 时保存为输入快照。重试不读取变更后的项目配置，避免同一 run 静默改变输入。

业务表由 Alembic 管理。LangGraph checkpoint 表使用官方 PostgresSaver / SqliteSaver 的 `setup()`，不伪装成应用自有表，也不通过 `create_all()` 代替迁移。

## 可靠性和故障窗口

1. 创建 run 与 Outbox 同事务提交；Broker 不可用不导致已接受请求丢失。
2. Beat 周期性扫描仍为 QUEUED 且 revision 匹配的事件，即使此前已发布，也允许重新投递。
3. PostgreSQL `FOR NO KEY UPDATE` 行锁串行化同一 run 的执行，与子表外键所需的 key-share 锁兼容。
4. 过期 revision、已暂停或终态 run 的消息被忽略；相同剧集通过行锁避免同时启动多个活动 run。
5. 产物在各节点自己的事务中提交，`run_id` 唯一约束防止重复创建；重放先读取已存在产物，再考虑调用 Provider。
6. checkpoint 使用同步 durability。若 checkpoint 已推进但运行事务未提交，下次执行检查持久化快照，恢复现有审核节点或剩余步骤。
7. 仅当 checkpoint 的待审核对象与已保存 resume payload 精确一致时才发送该审核结果，避免重放时将剧本批准误用于分镜。
8. 失败使用稳定错误码，不公开供应商响应、密钥或内部绝对路径。失败不自动重提付费模型调用；用户可显式 retry。

**不是分布式事务**：业务库、官方 checkpoint、外部模型无法原子提交。模型返回后但产物事务提交前崩溃仍可能在下一次调用中产生额外费用。本原型不承诺外部 exactly-once。

运行推进期间持有 run 行锁，适合当前短文本工作流。未来媒体长任务不能沿用此策略，应转为 generation_jobs + generation_attempts + 不确定结果对账 + 预算预留机制。

## 安全边界

当前是单租户服务 Token，通过依赖注入提供固定 principal；不能用客户端传来的任意用户 ID 冒充身份。
生产配置拒绝默认/过短令牌、Mock Provider、SQLite 主库和 SQLite checkpoint，但这些保护**不等于已具备生产上线条件**。
尚缺 JWT/OIDC、多项目成员 RBAC、TLS、请求体字节上限、限流、完整审计与预算治理。

真实 Provider 只处理文本结构化输出；输入作为用户数据发送，没有 shell/文件写入工具。Schema 与引用校验只证明结构正确，不保证创作质量、安全内容或版权合规。

未来新增图片候选版本后，必须按原技术设计加入媒体完整性检查与 Continuity Reviewer，未经审查不能进入正式选用状态。本次不提供绕过此门禁的媒体 API。

## 取舍

- KISS：模块化单体和一个明确的双审核生产图，不预建空微服务和所有 Agent。
- DRY：统一事务、权限入口、Provider 合同和版本持久化规则。
- SOLID：图、HTTP、生成适配器与产物持久化分离，便于测试和替换 Provider。
- YAGNI：未实现的媒体能力不暴露空成功接口，不将数据库占位结构标成已完成服务。
