# Arclight MCP

Arclight 的五类内置 Agent 通过 MCP SDK 的内存传输查找并调用项目工具。任务在哪个进程执行，MCP 服务就随该次运行在该进程内提供。按现有 Compose 命令启动即可。

```powershell
docker compose up --build -d
```

后端将当前运行已授予、且符合角色与活动阶段的工具通过 `tools/list` 提供给模型。模型选中工具后，运行时重新校验授权，再通过 `tools/call` 调用。查询沿用同一个事务，并使用后端确定的账号、项目、剧集和运行范围。MCP 仅在运行进程内部可访问。

| 工具 | 允许的角色 | 查询范围 |
| --- | --- | --- |
| `arclight_read_content` | 编剧、导演、美术指导、连续性审查 | 本剧集已批准的剧本，可选择场次 |
| `arclight_query_story_facts` | 编剧、导演、连续性审查 | 本次运行的冻结叙事上下文，可筛选场次、故事时间和角色 |
| `arclight_read_shot_context` | 导演、美术指导、连续性审查 | 本剧集已批准的分镜和镜头 |
| `arclight_read_visual_references` | 导演、美术指导、连续性审查 | 本项目的视觉规范和已批准参考 |
| `arclight_read_production_status` | 制片 | 本次制片运行的计划、任务状态与预算 |
| `arclight_read_review_evidence` | 制片、连续性审查 | 本次运行的证据与审查报告，制片也可查同一账号发起的直属委派审查 |
| `arclight_preview_change_impact` | 制片、编剧、导演 | 本项目的资产版本变更影响。编剧限写稿和改稿阶段 |

七项工具均为只读。权限表给出各角色可被授予的工具。每次运行的实际清单还取决于工作流的授权、阶段和数据范围。
