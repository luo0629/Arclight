# 账户、协作与内容版本 API

## 身份与权限

- `/api/v1/auth/register`、`/login`：注册或登录，返回短期 Access JWT；密码使用 Argon2 保存。
- `/refresh`：使用 HttpOnly、SameSite=Strict Cookie 换取新的 Access JWT，刷新令牌单次轮换；旧刷新令牌不可重放。
- `/logout`：撤销当前 Cookie 对应会话；没有 Cookie 时可通过 Access Bearer 撤销会话。退出后该会话的 Access Token 也失效。
- `/me`：查询当前账户。前端 Access Token 仅存于内存，不写入 localStorage。
- 非 GET 账户操作拒绝未允许的 Origin。TLS 终止部署必须配置实际外部 Origin，不应允许任意站点。
- 项目拥有者自动为 Owner；可按已注册邮箱加入 Editor / Viewer，成员角色修改不允许静默转移拥有权。
- Viewer 只读；Editor 可创建任务、编辑内容、取消/重试；Owner 负责内容批准、项目归档、预算配置、成员管理和审计查询。
- 非项目成员访问返回 404；成员越权返回 403。非生产环境仍保留开发 Service Token；生产禁用此入口。

## 项目与剧集

- `PATCH /projects/{id}`：Owner 修改名称、视觉规范、输出配置、预算额度或归档/恢复。
- `PATCH /episodes/{id}`：Editor 可修改标题、目标时长；归档/恢复仅 Owner。
- 存在排队、运行或待审核工作流时，禁止改写相关生产输入；归档后只读，恢复必须单独操作。
- `/projects/{id}/members`：查看/添加成员；`/{user_id}` 修改角色或移除。
- `/projects/{id}/audit-events`：Owner 分页查询内容审核、成员及配置变更审计。
- `/episodes/{id}/runs`：分页查询工作流历史。

## 不可变内容版本

- `POST /episodes/{id}/scripts`：`expected_version_no` 加完整结构化 `content`，保存人工新版本。
- `POST /episodes/{id}/storyboards`：另需 `script_version_id`，必须引用本剧集已批准的精确剧本版本。
- 内容版本只增不覆盖；版本号冲突返回 409。分镜还需通过场景/角色/台词引用及目标时长校验。
- `POST /episodes/{id}/{scripts|storyboards}/{version_id}/review`：Owner 审核人工版本，正文 `decision=APPROVE|REJECT`、可选 `comment`。
- 工作流生成版本不能从此接口绕过审核，必须使用原 `/runs/{id}/resume`，绑定具体版本和 revision。
- 前端在剧集起点选择“自己写剧本”，从空白表单创建人工剧本；保存前仅是本地草稿，保存后可选择该版本由 Owner 审核。
- 前端在剧集起点选择“自己写分镜”，通过 Radix UI 选择本剧集已批准的精确剧本版本（包含超过 100 条的历史），再填写场景、画面、角色、时长、景别、运镜和字幕，可增删镜头。保存使用现有人工接口，不调用模型；结果为独立待审核版本，需在画布或版本列表中选择后由 Owner 审核。只读、归档或活动任务时禁止起稿；请求失败保留当前草稿，版本冲突需刷新后显式重试。
- 人工版本审核通过不会自动调用模型；在已批准剧本上选择“从此版本生成分镜”，显式启动独立导演任务。

## 从指定剧本启动导演

`POST /api/v1/episodes/{id}/storyboard-runs`，必需 `Idempotency-Key` 请求头，正文：

```json
{"script_version_id": "指定已批准剧本的 UUID"}
```

- 返回 202，格式与 `script-runs` 相同：`run_id`、`status`、`status_url`、`events_url`。
- Editor / Owner 可发起；Viewer 403，非成员或其他剧集的剧本 404；未批准剧本、归档项目／剧集或同剧集已有活动任务 409。
- 输入固定精确剧本 ID、正文快照和目标时长；不会调用编剧或新增剧本，不会在执行／重试时改用最新版。
- `workflow_type=storyboard`、`workflow_version=director-1.0`；旧 `1.0` 双审核图保留，已有 checkpoint 继续按原拓扑恢复。没有数据库结构变化，无需新增 Alembic revision。
- 两种启动入口共享同一剧集的幂等键空间；同键同正文返回原任务，同键不同正文／入口返回 409。成功提交后再次主动生成请使用新键。
- 只有分镜人工审核节点，仍用 `/runs/{id}/resume` 绑定精确版本和 revision；取消、失败重试、事件及重复消息保护复用既有协议。

## 画布

- `GET /episodes/{id}/canvas`：从实际剧本、分镜、镜头版本派生节点和依赖连线，不接受客户端伪造依赖。
- `PUT /episodes/{id}/canvas/layout`：保存 `positions`、`viewport`、`expected_revision`；版本竞争返回 409，不覆盖他人布局。
- 仅接受本剧集节点和有限坐标/缩放；只读成员不可保存布局。

## 数据库与部署边界

业务表通过 Alembic `0002` 增加账户、可撤销会话、项目成员、审计及画布布局，并兼容 `0001` 旧数据回填。
有人工内容版本时不能无损降级到要求 run_id 非空的 `0001`，迁移会在修改结构前拒绝；请先备份并制定数据迁移方案。

请求体默认限制 2,000,000 字节；账户 POST 默认每来源地址、每 API 进程 30 次/分钟。
多副本须在统一网关执行全局限流，反向代理部署还必须限定可信代理，不能盲目信任客户端转发头。
预算目前只保存额度，尚不执行费用预留/扣费；审计并非所有只读访问和所有运行事件的通用合规审计。
本功能不包含图片、对象存储、连续性审查和成片，不能将账户及版本接口的完成当作完整生产系统验收。
