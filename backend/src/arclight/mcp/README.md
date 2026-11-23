# Arclight MCP

在仓库根目录启动 Arclight。MCP 随 API 容器一起运行。

```powershell
docker compose up --build -d
```

- 使用支持 Streamable HTTP 的客户端连接 `http://127.0.0.1:8080/api/mcp`。
- 客户端在每次请求发送 `Authorization: Bearer <访问令牌>`，使用 Arclight 登录会话的访问令牌。服务检查签名、过期时间、会话撤销状态和用户状态。
- 自定义域名部署从现有 API 网关转发 `/api/mcp`，并在 `ARCLIGHT_AUTH_ALLOWED_ORIGINS` 配置受信来源。该值同时授权来源对应的 Host。
- 每次查询以令牌所属账号检查项目权限。Arclight 内置 Agent 在进程内调用同一查询服务，并额外受当前角色、阶段和运行授权限制。

| 工具 | 输入范围 |
| --- | --- |
| `arclight_read_content` | 指定剧集的已批准剧本版本，可选择场次 |
| `arclight_query_story_facts` | 指定剧集内冻结了叙事上下文的运行，可筛选场次、故事时间和角色 |
| `arclight_read_shot_context` | 指定剧集的已批准分镜版本和镜头 |
| `arclight_read_visual_references` | 指定项目的视觉规范版本和当前可用的项目级已批准参考 |
| `arclight_read_production_status` | 制片运行的最新计划、任务状态和预算用量 |
| `arclight_read_review_evidence` | 指定运行和阶段的取证轨迹及已有审查报告，可限制取证轮次 |
| `arclight_preview_change_impact` | 指定项目中一个精确资产版本变化后受影响的对象 |

成功调用在 `structuredContent.data` 返回结构化数据。无法读取时，调用结果包含错误码。工具均为只读。
