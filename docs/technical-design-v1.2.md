# Arclight：多智能体 AI 漫剧生产工作台

> **文档类型：** 产品需求与技术设计（PRD + TDD）  
> **版本：** v1.2  
> **状态：** 设计基线，可作为项目实现与 GitHub 文档的起点  
> **目标产品：** 面向 2D 竖屏动态漫剧的多智能体辅助生产平台  
> **核心定位：** 将剧本、分镜、视觉资产、音频、视频片段和最终成片组织成可追踪、可恢复、可审核的生产流程。

---

## 目录

1. [项目概述](#1-项目概述)
2. [产品目标与范围](#2-产品目标与范围)
3. [用户角色与典型流程](#3-用户角色与典型流程)
4. [功能需求设计](#4-功能需求设计)
5. [多智能体系统设计](#5-多智能体系统设计)
6. [生产工作流与状态机](#6-生产工作流与状态机)
7. [核心 Harness 机制](#7-核心-harness-机制)
8. [系统架构与技术选型](#8-系统架构与技术选型)
9. [数据模型与资产版本管理](#9-数据模型与资产版本管理)
10. [API 设计](#10-api-设计)
11. [模型服务与媒体生成管线](#11-模型服务与媒体生成管线)
12. [质量控制与连续性管理](#12-质量控制与连续性管理)
13. [前端信息架构](#13-前端信息架构)
14. [安全、权限与成本治理](#14-安全权限与成本治理)
15. [可观测性与评测体系](#15-可观测性与评测体系)
16. [测试策略](#16-测试策略)
17. [部署方案与配置](#17-部署方案与配置)
18. [代码仓库结构](#18-代码仓库结构)
19. [开发里程碑与验收标准](#19-开发里程碑与验收标准)
20. [演示方案与简历呈现](#20-演示方案与简历呈现)
21. [风险与技术决策](#21-风险与技术决策)
22. [官方参考资料](#22-官方参考资料)

---

## 1. 项目概述

### 1.1 项目背景

AI 漫剧生产不是单次文本生成任务，而是一条跨文本、图像、音频、视频和人工审核的内容生产链。一个可交付的视频需要剧本结构、分镜计划、角色与场景素材、画面生成、配音、字幕、时间轴和最终渲染彼此匹配。只把多个模型 API 串联起来，往往会遇到以下问题：

- 不同集数中的角色外观、服装、画风和场景难以保持一致。
- 修改剧本后，不知道哪些分镜、图片、视频片段和字幕需要重新生成。
- 图像或视频任务耗时长、失败率高，整条生产流程容易被单个任务拖垮。
- 多智能体之间传递自然语言，缺乏稳定的输入输出合同和状态管理。
- 生成质量无法自动验收，系统可能把损坏、缺失或不匹配的素材直接送入成片。
- 缺少模型调用成本、任务耗时、版本历史、人工修改和失败原因的统一记录。

Arclight 的目标是将这些内容生产活动组织成一套可视化、可恢复、可审计的生产系统。

### 1.2 产品定义

Arclight 是一款面向个人创作者、小型漫剧工作室和 AI 内容制作团队的 Web 工作台。用户可以创建漫剧项目，导入故事或剧本，使用多智能体生成分集剧本和分镜，管理角色与场景资产，批量生成画面和配音，组装时间轴，并导出竖屏动态漫剧。

系统采用 **Multi-Agent Orchestration + Asset Graph + Durable Workflow + Media Pipeline + Human Review** 的总体设计：

- 多智能体负责不同专业环节的分析与创作。
- Harness 负责任务调度、状态管理、依赖关系、重试、暂停恢复和质量门禁。
- 资产图记录剧本、分镜、角色参考图、镜头素材、音频与成片之间的依赖关系。
- 后台任务系统运行图像、音频、视频生成及 FFmpeg 渲染等耗时作业。
- 人类创作者能够随时查看、修改、批准、驳回或重新生成特定资产。

### 1.3 设计原则

1. **Asset-first，而不是 Chat-first：** 剧本、分镜、人物设定、素材与成片是正式资产，不能仅保存在对话历史中。
2. **LLM 做判断，确定性代码管执行：** 模型可以提出内容和计划，但任务状态迁移、权限、预算、版本一致性和交付验收由服务端代码控制。
3. **任务可恢复：** 服务重启、供应商超时或一个镜头失败，不应导致已经完成的工作全部重做。
4. **局部重建：** 上游资产发生修改时，只让受影响的下游资产失效，而不是重新生成整集。
5. **所有生成物可追踪：** 记录使用的模型、参数、提示词版本、参考资产、费用、耗时和生成结果。
6. **人工参与是一等能力：** 关键创作节点支持审核，不把人工审核当作失败兜底的临时补丁。
7. **模型服务可替换：** 业务代码不直接依赖某个厂商的 SDK，所有供应商通过统一适配层接入。
8. **先做稳定闭环，再扩展规模：** MVP 先完成 1–3 分钟竖屏动态漫剧的制作与导出，不一开始追求长篇全视频生成。

---

## 2. 产品目标与范围

### 2.1 MVP 目标

第一版需要能够完整走通以下流程：

1. 创建一个漫剧项目并设置画幅、画风、目标时长与单集配置。
2. 输入故事大纲或上传小说片段。
3. 生成分集剧本，并允许创作者编辑和确认。
4. 创建角色卡、场景卡和视觉风格规范。
5. 将确认后的剧本拆解成结构化分镜。
6. 为每个镜头生成静态画面，支持参考图、重试、选择与手动替换；每张候选画面都必须经过确定性媒体检查和 Continuity Reviewer 连续性审查，问题结果进入修订或人工审核流程。
7. 生成或导入配音，维护台词、角色音色和字幕。
8. 将画面、音频、字幕组装成时间轴。
9. 使用 FFmpeg 渲染成 MP4，执行文件完整性检查。
10. 支持查看每个任务的状态、日志、费用、版本和失败原因。
11. 修改某个上游内容时，识别并显示受影响的下游资产。
12. 通过 Docker Compose 在本地启动完整开发环境。

### 2.2 非目标（MVP 暂不实现）

- 不承诺仅凭一个提示词就能生成专业级、完全无需人工审核的完整剧集。
- 不实现完整的 3D 动画制作、动作捕捉或复杂角色骨骼动画。
- 不直接接入真实支付、版权交易或内容分发平台。
- 不实现大型团队级权限目录、复杂组织计费和企业级租户隔离的全部能力。
- 不训练基础图像、视频或语音模型；以调用外部模型或本地模型服务为主。
- 不在 MVP 阶段实现完全自动化的口型同步。可以预留音素对齐或口型服务接口。
- 不追求所有镜头都使用 AI 视频生成。MVP 的核心交付是动态漫剧：静态画面、轻量运镜、配音、字幕、音效与转场组成可观看的短片。

### 2.3 成功交付标准

一个项目被视为完成，至少满足：

- 剧本、分镜及必需的视觉资产已确认。
- 所有必需镜头都有有效画面素材；每张候选画面均完成 Continuity Reviewer 审查，问题已修订或由人工明确处理，不能跳过连续性审查直接进入正式选用状态。
- 所有需要发声的台词都有音频，或者创作者明确设置为静音/字幕展示。
- 字幕时间轴有效且与台词记录对应。
- 最终视频达到项目设定的画幅、时长范围和编码要求。
- 视频文件可解码，音轨和视频轨均符合配置。
- 成片、分镜和生成素材之间存在可追溯的资产关联。
- 制作任务以 `COMPLETED` 结束，或明确以 `COMPLETED_WITH_WARNINGS` 结束并列出人工确认的例外。

---

## 3. 用户角色与典型流程

### 3.1 用户角色

| 角色 | 权限与主要目标 |
|---|---|
| 项目所有者 / Producer | 创建项目、设定制作规格、分配任务、批准关键版本、查看预算与整体进度 |
| 编剧 / Writer | 编写与修改故事、分集剧本、角色对白和场景说明 |
| 分镜师 / Director | 设计镜头顺序、景别、构图、运镜、时长及镜头衔接 |
| 美术 / Artist | 审核角色设定、场景、服装、风格和生成画面 |
| 剪辑 / Editor | 调整镜头顺序、时长、转场、音频、字幕和渲染设置 |
| 管理员 / Admin | 管理用户、模型服务配置、默认预算、审计和系统配置 |

MVP 可以先将角色简化为 Owner、Editor、Viewer 三种系统权限，但数据模型应允许后续扩展专业角色权限。

### 3.2 典型端到端流程

```mermaid
flowchart TD
    A[创建漫剧项目] --> B[导入故事或输入大纲]
    B --> C[生成分集剧本草稿]
    C --> D{创作者确认}
    D -- 修改 --> C
    D -- 确认 --> E[生成角色与场景设定]
    E --> F[生成分镜表]
    F --> G{分镜审核}
    G -- 退回修改 --> F
    G -- 通过 --> H[批量生成镜头画面]
    H --> I[连续性与素材质量检查]
    I --> J{是否通过}
    J -- 否 --> K[重试/手工替换/调整提示词]
    K --> H
    J -- 是 --> L[生成配音与字幕]
    L --> M[时间轴组装]
    M --> N[FFmpeg 渲染]
    N --> O[视频完整性检查]
    O --> P{验收通过}
    P -- 否 --> M
    P -- 是 --> Q[成片交付]
```

### 3.3 用户体验原则

- 每一个耗时操作都应立即返回任务 ID，而不是让 HTTP 请求一直等待。
- 用户始终可以看到当前处于“规划、等待审核、生成中、质检中、渲染中”等哪个阶段。
- 失败信息应说明受影响的资产、失败类型、是否可重试，以及下一步建议。
- 同一批次允许逐镜头查看结果，用户可接受部分结果，不必等所有镜头生成完成。
- 用户对剧本、角色或分镜的人工修改，应转化为新的资产版本并触发依赖检查。

---

## 4. 功能需求设计

### 4.1 项目与剧集管理

**功能项**

- 创建、编辑、归档和删除项目。
- 设置项目名称、简介、目标受众、题材、视觉风格、画幅比例和默认目标时长。
- 支持一个项目下多个剧集，每集有独立剧本、分镜、素材和成片。
- 支持项目级世界观、角色、地点、物品和风格规则。
- 显示剧集状态、待审核资产数、生成任务进度、成本和成片版本。
- 支持复制剧集作为新集草稿；复制时可复用角色与场景资产，不复制已失效任务。

**验收要求**

- 用户创建项目后，可以创建至少一个剧集。
- 剧集与项目的归属关系必须在后端校验。
- 项目列表展示状态和最近更新时间，排序与分页在数据库层执行。

### 4.2 故事导入与剧本工作台

**输入形式**

- 文本输入故事大纲或章节。
- 上传 TXT、Markdown、DOCX 等文本型素材（首版可先实现 TXT/Markdown）。
- 用户提供目标集数、单集时长、主要角色、题材风格和内容限制。

**核心能力**

- 将原始故事提炼为故事摘要、主要冲突、角色表和关键事件。
- 生成分集大纲，并标明每集目标、冲突、转折和结尾钩子。
- 将分集大纲改编为可制作的场景与剧本，包含场景标题、动作描述、角色、对白、旁白和情绪。
- 支持按章节或选定段落重新改写，不要求整部作品重生成。
- 支持“语气调整、缩短对白、强化冲突、降低旁白比例”等局部编辑指令。
- 剧本版本对比：显示文字差异、修改人、修改时间及下游受影响资产。
- 支持人工确认剧本后进入分镜阶段。

**重要约束**

剧本生成必须输出 JSON/Pydantic 结构，并对角色引用、场景编号、对白归属和必需字段进行校验。模型生成的角色名称不能自动创建重复角色；需要先进行别名匹配或请求用户确认。

### 4.3 角色、场景、道具与视觉风格资产中心

每个视觉资产支持以下字段：

- 名称、类型、简介、标签。
- 结构化描述：年龄段、外观、发型、服装、体态、常用表情、色彩、材质等。
- 参考图片及图片版本。
- 生成模型、模型版本、提示词、随机种子（服务商支持时）、尺寸和参数。
- 审核状态、创建来源、创建者和使用范围。
- 被哪些剧集、场景、分镜和镜头引用。

**角色资产功能**

- 生成角色初始设定图或多角度参考图。
- 维护“固定特征”和“允许变化特征”，例如脸部、发色、服装款式属于固定属性；情绪、姿势和镜头角度可以变化。
- 为角色绑定默认服装与可选服装。
- 允许用户将某张生成图设为标准参考图。
- 显示角色出现在什么集、什么场景、哪些镜头中。

**场景与道具功能**

- 管理空间布局、时间段、灯光、色彩和环境描述。
- 管理关键道具的外观、持有角色和剧情状态。
- 支持场景参考图被多个镜头复用。

**视觉规范（Visual Bible）**

项目拥有统一视觉规范，包括画风描述、线条风格、渲染风格、主要配色、光照原则、构图偏好、禁止元素和负面提示词。生成 Agent 不能随意覆盖已锁定的规范字段。

### 4.4 分镜与镜头工作台

每一个分镜镜头（Shot）至少包含：

- 镜头序号和所属场景。
- 景别：远景、全景、中景、近景、特写等。
- 画面描述、角色引用、场景引用、道具引用。
- 角色动作、表情、视线方向和画面构图。
- 运镜类型：固定、推近、拉远、平移、轻微摇移等。
- 台词、旁白、音效及对应角色。
- 预计时长、转场类型、字幕内容。
- 参考素材、提示词版本、生成参数和当前选用素材。
- 质检结果、人工备注和审核状态。

**核心操作**

- 以表格模式批量编辑镜头；以卡片/画布模式查看视觉效果。
- 拖动调整镜头顺序，并自动重新计算序号和时间轴依赖。
- 选择多个镜头进行批量生成、批量审核或批量重试。
- 对某一个镜头执行“重新构图、调整表情、替换场景、修复手部、换服装”等局部修改。
- 查看当前镜头提示词中合并了哪些角色、场景和风格资产。
- 在画面资产被替换后，将关联的预览视频和成片标记为需要重新渲染。

### 4.5 AI 画面与视频生成中心

**任务能力**

- 静态画面生成。
- 基于参考图的图生图或变体生成（具体能力由 Provider 支持情况决定）。
- 可选的图生视频/短视频片段生成。
- 批量生成候选结果并由用户选择最佳版本。
- 支持排队、并发上限、取消、重试、失败分类和超时管理。
- 对所有结果进行元数据记录，不覆盖历史素材。
- 显示预计成本和实际成本（供应商提供计费数据时）；无法获取准确成本时应标记为估算值。

**任务状态**

`QUEUED → RUNNING → SUCCEEDED`，或进入 `RETRY_WAIT`、`FAILED`、`CANCEL_REQUESTED`、`CANCELLED`。

**任务结果约束**

- 所有图片/视频结果先进入临时存储区，完成格式和文件完整性检查后再注册为正式资产。
- 生成结果应有唯一 `generation_job_id`，并与源镜头、输入参考资产、模型配置和提示词版本关联。
- 同一任务重试产生新 attempt 记录，不覆盖之前的失败日志。
- 对不支持可靠幂等的外部生成请求，不应假设重复请求会返回同一个结果；应用层需要追踪请求、尝试与结果，避免重复注册资产。

### 4.6 配音、字幕与音效

**功能**

- 从剧本中抽取对白和旁白，生成语音任务。
- 为角色绑定默认音色、语速、音高或情绪参数（以供应商支持项为准）。
- 对单句音频重新生成，不必重生成整集音频。
- 为每条台词维护文本、角色、起止时间、音频资产 ID 和版本。
- 支持导入外部音频。
- 支持背景音乐、环境音、转场音效的轨道管理。
- 生成 SRT 字幕或内部结构化字幕轨，允许人工调整时间点。
- 对音频文件检查编码、采样率、时长和静音情况。

**MVP 处理策略**

第一版先使用句级配音和基于镜头/台词时间的字幕，不强制实现精确口型同步。时间轴编辑器应允许用户对配音时长和字幕出现时间进行微调。

### 4.7 时间轴与成片渲染

**时间轴结构**

- 视频/画面轨：镜头素材、持续时间、裁切与轻量运动效果。
- 配音轨：角色对白、旁白。
- 音效轨：环境音、动作音效和转场音效。
- 背景音乐轨：音量包络、淡入淡出。
- 字幕轨：文本、样式、开始和结束时间。

**渲染能力**

- 设置输出分辨率、帧率、画幅、视频编码、音频编码、字幕样式。
- 生成渲染计划和 FFmpeg 命令参数；不允许客户端任意传入 Shell 命令。
- 将渲染提交为后台任务，返回任务 ID。
- 输出预览版与正式版，正式版可启用更高码率或更高分辨率。
- 渲染失败后保留日志与临时文件清理状态，允许重新渲染。
- 生成视频后执行 ffprobe/解码检测、音视频流存在性、时长、尺寸和文件大小检查。
- 成片不可变更；任何时间轴修改都生成新的 Render Version。

### 4.8 任务与生产运营中心

任务列表需要支持按项目、剧集、任务类型、状态、创建时间和失败原因过滤。每个任务详情应显示：

- 任务输入摘要、输出资产、当前状态与进度。
- 执行步骤、Agent 事件、模型调用和工具调用。
- 重试次数、错误类型、耗时、Token 数及成本信息。
- 上游依赖和下游受影响资产。
- 取消、重试、恢复、忽略非阻塞警告等操作（根据用户权限控制）。

任务面板应区分“业务失败”和“系统基础设施失败”。例如，模型拒绝内容、生成结果质量不合格、供应商网络超时、文件存储失败，都应有不同错误类别。

### 4.9 版本、变更影响与历史记录

- 每次人工保存或 Agent 生成，都产生明确版本或修订记录。
- 版本不可被静默覆盖，用户可以查看历史版本、恢复为新版本。
- 每个生成素材记录输入资产版本、提示词模板版本和模型配置版本。
- 剧本变更触发影响分析：对白改变会影响语音和字幕；场景描述改变可能影响相关分镜图；仅调整镜头顺序通常只需要重算时间轴与成片。
- 用户确认的资产可以标记为 `LOCKED`，自动生产任务不得静默替换锁定资产。

---

## 5. 多智能体系统设计

### 5.1 总体设计

Arclight 采用 Supervisor 协调多个专业 Agent 的模式。Agent 不直接持有数据库写权限，也不直接管理任务队列；它们通过有限的领域工具获取数据、提出内容和创建结构化交付物。工作流引擎、资产服务和后台任务服务负责执行真正的状态转换。

### 5.2 Agent 职责定义（收敛后的实现基线）

MVP 不以 Agent 数量作为目标。创作决策由少量专业 Agent 承担；任务排队、供应商调用、渲染、状态迁移和文件检查由确定性服务完成。

#### 5.2.1 Producer / Workflow Coordinator（编排协调器）

MVP 中优先实现为 **确定性 Workflow Service + LangGraph 路由节点**，而不是一个拥有自由执行权的全能 Agent。

负责：
- 根据用户命令和资产状态选择预定义工作流，例如 `story_to_script`、`script_to_storyboard`、`shot_generation`、`episode_render`。
- 检查前置资产、审核状态、预算和任务配额。
- 创建带依赖关系的任务计划，并将任务交给工作流或后台 Worker。
- 处理暂停、恢复、取消、有限重试和失败升级。

LLM 可以提出计划建议，但服务端必须将建议映射到允许的工作流类型和工具白名单；不允许模型构造任意任务、SQL 或 Shell 命令。

#### 5.2.2 Screenwriter Agent（编剧 Agent）

输入：故事文本、项目设定、目标时长、集数规划和已确认的角色信息。  
输出：结构化分集大纲、场景列表、对白及叙事说明。

关键约束：输出必须通过 Pydantic Schema 校验；角色、场景和对白引用必须可解析；估算时长须在配置范围内。剧本生成后创建新版本，不覆盖历史版本；进入下游生产前须通过审核或明确配置的自动放行规则。

#### 5.2.3 Director Agent（导演 / 分镜 Agent）

输入：已确认的剧本版本、角色/场景资产版本、视觉规范和目标时长。  
输出：结构化分镜与镜头清单，包括景别、构图、动作、时长、台词引用、角色/场景/道具引用和画面描述。

关键约束：所有引用必须指向明确的资产版本；镜头顺序稳定；对白必须关联有效台词；镜头时长总和须符合目标时长约束。Director 不能直接触发任意供应商 API。

#### 5.2.4 Art Director Agent（美术指导 Agent）

输入：角色与场景设定、风格指南、已批准的参考素材。  
输出：视觉设定提案、提示词片段、角色/场景/道具绑定和参考素材清单。

关键约束：已批准或锁定的角色外观和风格字段不可被 Agent 静默覆盖。需要改变规范时，必须生成新版本或修改提案，经用户确认后生效。

#### 5.2.5 Continuity Reviewer（连续性审查 Agent，必需）

Continuity Reviewer 是 MVP 的核心生产 Agent，不是可选增强项。每张候选镜头图片在通过确定性媒体检查后，都必须进入连续性审查；审查完成前，不得将该候选图片登记为已完成连续性检查的正式可用素材。第一版可使用受限的视觉 LLM 调用完成判断，不需要独立的长期自主规划循环。

输入：候选图片、目标分镜、当前角色/场景/道具参考图、锁定的视觉规范，以及必要的前后相邻镜头或相关剧情上下文。  
输出：结构化问题列表、可定位的证据区域或简短依据、问题类别、风险等级、建议动作、置信度，以及明确的审查结论。

审查至少覆盖角色外观与服装一致性、场景与道具连续性、画面是否符合分镜意图，以及与相邻镜头是否存在明显矛盾。结论应采用固定 Schema，并区分 `PASS`、`REVISION_REQUIRED` 和 `HUMAN_REVIEW` 等结果；置信度低、存在重大矛盾或证据不足时转人工审核，发现可修正问题时进入局部重生成或提示词修订流程。

该 Agent 提供质量判断与修订建议，不直接把素材标记为最终通过，也不能覆盖用户已锁定的参考资产。文件损坏、尺寸错误、引用缺失、字幕越界等仍由确定性检查器判定；连续性审查负责视觉和叙事语义层面的判断。工作流必须记录审查所用的输入资产版本、模型/提示词版本、结论和问题明细，确保审查可追踪、可重跑、可审计。

#### 5.2.6 确定性媒体服务（不作为 LLM Agent）

以下职责实现为普通服务和后台 Worker：
- `GenerationService`：根据生成规格调用图像/语音/视频 Provider Adapter，维护任务、尝试和结果。
- `TimelineService`：根据已选镜头、音频和字幕生成规范化时间轴。
- `RenderService`：使用 FFmpeg/ffprobe 执行渲染与文件验证。
- `QualityGateService`：运行确定性检查，并汇总必需的 Continuity Reviewer 结论与人工审核结果；候选图片未完成连续性审查时不得通过质量门禁。
- `RecoveryManager`：分类错误、判断能否重试、执行对账与局部重建。

这些服务不需要为每个动作增加 LLM 调用。若未来需要自然语言剪辑建议，可单独增加 Editor Agent，但最终时间轴必须通过 Schema 和业务规则验证后交给确定性渲染服务。

### 5.3 Agent 间数据合同

Agent 之间只传递必要的结构化数据或资产 ID，不传递超大图片二进制或无界的全部历史消息。大型素材通过对象存储访问，结构化元数据从数据库读取。

推荐所有 Agent 结果统一包含以下元信息：

```json
{
  "result_id": "uuid",
  "project_id": "uuid",
  "episode_id": "uuid",
  "agent_name": "director_agent",
  "schema_version": "1.0",
  "status": "completed",
  "summary": "生成了 18 个镜头草案",
  "artifact_ids": ["uuid-1", "uuid-2"],
  "warnings": [],
  "requires_human_review": true
}
```

这是交付结果的通用外层协议；具体业务字段由 Pydantic 模型定义，并配置严格的类型、枚举、长度与引用校验。

### 5.4 LangGraph、Celery 与数据库的职责边界

**LangGraph = 长流程的决策与流程状态；Celery = 单项后台作业的执行；PostgreSQL = 业务状态权威来源。**

- **LangGraph**：编排剧本、分镜、审核、依赖检查等阶段；保存可恢复的工作流状态；支持人工中断与恢复。持久化 checkpointer 中的状态用于恢复流程，不替代领域表中的资产、任务和审核状态。
- **Celery Worker**：执行可独立调度的耗时任务，例如图像/语音生成、文件下载、媒体检查和 FFmpeg 渲染。Celery result backend 仅用于运行状态辅助，不作为业务事实的唯一来源。
- **PostgreSQL**：权威保存 `workflow_runs`、`generation_jobs`、`generation_attempts`、资产版本、审核记录、业务状态和 outbox 事件。
- **Redis**：消息 broker、限流与短期缓存；不保存唯一的任务状态或资产元数据。
- **对象存储**：保存媒体二进制；数据库记录对象 key、checksum、MIME、大小和来源。

推荐交互方式：

1. LangGraph 节点调用应用服务，在事务中创建 `generation_job` 与 outbox 事件。
2. Outbox Dispatcher 发布任务消息，Celery Worker 按 `job_id` 执行。
3. Worker 将状态与结果写回 PostgreSQL，并发布完成事件。
4. 工作流协调器消费事件，检查输入版本、任务状态和下游依赖，再推进 LangGraph。
5. 每次推进都重新读取 PostgreSQL 中的权威状态，不依赖过期的内存状态推断任务已完成。

必须假设消息可能重复投递、Worker 可能重启、LangGraph 节点可能重入。所有具有外部副作用的步骤都需要幂等控制。官方参考：[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Celery Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)。

---

## 6. 生产工作流与状态机

### 6.1 项目生产阶段状态

一个剧集可以采用以下状态：

- `DRAFT`：初始草稿。
- `SCRIPTING`：剧本生成或编辑中。
- `WAITING_SCRIPT_REVIEW`：等待剧本审核。
- `ASSET_PREPARATION`：角色、场景和风格资产准备中。
- `STORYBOARDING`：分镜生成中。
- `WAITING_STORYBOARD_REVIEW`：等待分镜审核。
- `GENERATING_MEDIA`：画面、视频或音频生成中。
- `MEDIA_QA`：素材检查中。
- `EDITING`：时间轴组装或编辑中。
- `RENDERING`：视频渲染中。
- `DELIVERY_QA`：成片检查中。
- `COMPLETED`：验收完成。
- `COMPLETED_WITH_WARNINGS`：存在已知非阻塞警告且经过确认。
- `FAILED`：任务不可继续，需要人工处理。
- `CANCELLED`：用户取消。

状态转换必须由服务端工作流服务统一控制，不能让前端直接提交任意状态值。

### 6.2 镜头生产状态

单个镜头的媒体生产状态独立于剧集状态。确定性检查通过后，必须进入连续性审查阶段：

`NOT_STARTED → READY → QUEUED → GENERATING → QA_PENDING → CONTINUITY_REVIEW → READY_FOR_REVIEW → APPROVED`

其中 `READY_FOR_REVIEW` 表示连续性审查已结束、候选素材可供创作者选择或进入配置好的后续审核；`APPROVED` 仅表示候选素材已被创作者或明确授权的审核策略接受。Continuity Reviewer 本身不能直接批准素材。审查发现问题时转入 `NEEDS_REVIEW` 或 `REJECTED`，完成修订/重生成后必须针对新候选版本重新审查。

异常分支：

- `RETRY_WAIT`：可重试失败。
- `NEEDS_REVIEW`：需要人工确认。
- `REJECTED`：当前结果被拒绝，等待新生成。
- `FAILED`：达到重试上限或不可恢复失败。
- `CANCELLED`：取消任务。

一个剧集允许不同镜头处于不同状态。整体进度应基于必需镜头、审核任务和渲染状态计算，而不是只根据最后完成的一个任务判断。

### 6.3 事件驱动的工作流

媒体生成和渲染不适合放在一个长时间保持运行的 HTTP 请求中。建议采用以下模式：

1. API 接收命令并校验权限。
2. 在数据库事务中创建业务记录与 outbox 事件。
3. Dispatcher 将事件发布到后台队列。
4. Worker 处理任务，更新任务和资产状态。
5. Worker 产生结果事件，通知工作流服务检查下一步依赖。
6. Web 前端通过 SSE 或轮询获取状态变化。

为避免“数据库已提交但队列消息未发出”的双写问题，建议采用 Transactional Outbox Pattern：业务变更和待发布事件在同一数据库事务中写入；后台 Dispatcher 再将事件投递到队列。消费者应按事件 ID 做去重。

### 6.4 失败分类与恢复策略

| 失败类别 | 示例 | 处理策略 |
|---|---|---|
| 可重试服务错误 | 模型供应商超时、HTTP 429、短暂网络故障 | 退避重试，遵守最大尝试次数 |
| 不可重试输入错误 | 缺少参考图、Schema 校验失败 | 立即失败并提示修正输入 |
| 质量未通过 | 图像格式有效但画面不符合预期 | 进入重生成或人工审核，不视为基础设施故障 |
| 资产失效 | 上游剧本/角色参考图版本发生变化 | 标记下游资产 stale，等待局部重建 |
| 文件系统错误 | 对象存储不可用、校验和不一致 | 暂停后续依赖任务，重试存储或进入人工诊断 |
| 用户中止 | 用户取消批次或镜头 | 取消未开始任务；正在运行的外部调用尽力取消并记录结果不确定性 |
| 预算超限 | 项目或批次达到成本上限 | 不再创建新生成任务，允许查看已有结果或申请预算提升 |

不要对所有异常使用无限重试。所有重试必须设置最大次数、总耗时上限与可重试错误白名单。对于供应商请求结果不确定的任务，重试前需要先查询或对账，防止重复计费与重复登记资产。

---

## 7. 核心 Harness 机制

本项目的 Harness 不等于 LangGraph 本身，而是围绕 Agent 与生成模型提供的运行时、治理、状态、上下文和质量控制层。

### 7.1 Task Runtime

每次创作任务都具有：

- `run_id`：一次工作流运行的唯一标识。
- `project_id`、`episode_id`：作用范围。
- `requested_by`：发起用户。
- `task_type`：剧本、分镜、图像生成、配音、渲染等。
- `input_snapshot`：启动时使用的输入资产版本。
- `workflow_version`：工作流定义版本。
- `status`、`started_at`、`finished_at`。
- `retry_count`、`budget_limit`、`cost_estimate`。
- `trace_id`：用于日志与链路追踪。

任务执行开始后，关键输入资产版本应固定下来。若用户在任务运行中编辑上游资产，应创建新版本，不要静默改变正在运行任务的输入。完成时标记产物基于哪些版本生成；如不再是最新版本，可标记为 stale。

### 7.2 Context Builder

不要把整个项目的所有剧本、角色描述、图像历史和日志全部塞入每一次 LLM 调用。Context Builder 根据任务类型组织有限上下文：

- 全局上下文：项目风格规范、内容限制和输出规格。
- 剧集上下文：已确认剧本、角色列表、场景列表和情节摘要。
- 局部上下文：当前镜头、相邻镜头、相关角色和道具参考资产。
- 约束上下文：当前任务允许修改哪些字段、哪些资产已锁定、模型预算。
- 历史上下文：只保留与当前任务相关的审核反馈和失败尝试摘要。

上下文必须记录其组装版本和引用资产 ID，便于重现问题。图片等二进制素材通过服务商支持的图像输入机制传递，避免直接把文件编码文本记录到数据库日志中。

### 7.3 Prompt / Schema 版本管理

每个 Agent 的系统提示词和输出 Schema 都应有版本号。生成结果至少关联：

- Agent 版本。
- Prompt Template 版本。
- Pydantic Schema 版本。
- 模型供应商与模型名。
- 参数配置摘要。
- 输入资产版本清单。

修改提示词或模型后，可以对固定测试集进行回归运行，比较结构化输出有效率、人工接受率、重生成率和成本变化。

### 7.4 Asset Dependency Graph

资产依赖图是本项目的重要工程能力。示例：

```text
原始故事
  └── 分集大纲
       └── 第 1 集剧本 v3
            ├── 场景列表
            │    ├── 场景 1 分镜
            │    │    ├── 镜头 1 图片 v2
            │    │    ├── 镜头 2 图片 v1
            │    │    └── 镜头 1/2 预览片段
            │    └── 场景 2 分镜
            ├── 角色对白
            │    ├── 角色 A 配音 v2
            │    └── 字幕轨 v3
            └── 时间轴 v4
                 └── 成片 v2
```

每条依赖边必须连接两个不可变的具体版本：`source_asset_version_id -> target_asset_version_id`，并记录 `dependency_type` 和生成时使用的 `dependency_manifest_id`。不能只引用 `target_asset_id`，因为该资产的当前版本指针会变化，无法准确还原历史生成输入。

例如，镜头图片 v4 的输入清单固定引用角色参考图 v2、场景图 v3、分镜 v5、提示词模板 v7 和模型参数快照。角色参考图升级到 v3 时，系统从被引用的具体版本反向查找受影响产物，按依赖类型规则标记下游候选资产为 `STALE`，而不修改旧版本的历史来源。

失效传播由代码中的依赖规则执行，并遵循明确策略。例如对白改变通常使相关语音、字幕、时间轴和成片失效，但不自动使无关背景图失效；镜头顺序变化通常只影响时间轴和成片。每次传播生成可预览的“影响清单”，允许用户确认重建范围。

`STALE` 不等于删除。旧版素材保留以支持版本对比、审计和回退，只有清理策略满足条件且不存在有效引用时才允许删除物理文件。

### 7.5 生成任务调度器

#### 7.5.1 通用任务目标与输入清单

生成任务不能只通过 `shot_id` 建模，因为角色设定图、场景图、封面、配音、音效和最终渲染不一定属于单个镜头。每个任务使用通用目标描述：

- `target_type` / `target_asset_id`：目标资产类型与目标资产（尚未注册正式资产时可为空）。
- `input_manifest_id`：不可变输入清单，列出精确的资产版本、提示词模板版本、模型、参数、输出规格和必要上下文摘要。
- `idempotency_key`：本次逻辑生成请求的唯一键。
- `provider_request_id`：供应商返回的异步任务标识（若供应商支持）。
- `attempt_no`：一次真实提交的序号；重试必须创建新的 attempt 记录，不覆盖之前的请求与错误信息。

#### 7.5.2 任务状态机

```text
CREATED -> SUBMITTING -> SUBMITTED -> RUNNING -> SUCCEEDED
    |           |            |          |
    |           |            |          +-> FAILED / CANCELLED
    |           |            +-> RESULT_UNKNOWN
    |           +-> RESULT_UNKNOWN
    +-> CANCELLED

RESULT_UNKNOWN -> RECONCILING -> RUNNING / SUCCEEDED / RETRY_WAIT / MANUAL_REVIEW
FAILED -> RETRY_WAIT -> SUBMITTING
```

`RESULT_UNKNOWN` 表示客户端没有得到明确响应，不能推断供应商没有接收请求。进入该状态后，Recovery Manager 必须先按供应商请求 ID、幂等键、回调记录或任务查询接口进行对账。若供应商不支持查询和幂等，默认转人工处理或按显式风险策略处理，不得盲目重复提交。

#### 7.5.3 幂等与重复消息

- API 命令通过 `Idempotency-Key` 或业务生成的幂等键去重，并保存请求指纹；相同键但不同请求体返回冲突错误。
- Worker 消费消息时以 `job_id` 和事件 ID 去重；重复投递不能创建重复的正式资产版本。
- 供应商支持幂等键时，将稳定键传递给供应商；不支持时，应用层只能保证内部记录和结果登记幂等，不能宣称外部调用绝对 exactly-once。
- 同一逻辑任务的不同尝试独立记录；成功结果先存为候选资产版本，通过文件校验和质量门禁后，才更新资产的 `current_version_id`。
- 更新当前版本指针使用事务与乐观锁/版本条件，避免并发任务互相覆盖。


生成任务需要支持：

- 按项目和供应商限制并发数。
- 全局与用户级速率限制。
- 高优先级交互任务与低优先级批量任务分队列。
- 预算预留与实际成本回写。
- 超时、重试、取消与死信处理。
- 同一个资产的互斥生成策略，避免两个任务同时覆盖“当前选用版本”。
- 供应商故障时暂停对应队列或切换到符合要求的备用 Provider。

任务去重键建议由 `source_asset_version + task_type + provider + model + normalized_parameters + prompt_version` 计算。它用于识别相同配置的请求，不能在未确认业务语义时强制复用旧结果；用户显式要求探索随机变体时应纳入 variation/seed 等参数。

### 7.6 人工审核与恢复

以下节点默认支持人工审核：

- 分集剧本确认。
- 角色主参考图和视觉规范确认。
- 关键镜头分镜确认。
- 存在明显连续性问题的镜头。
- 正式成片导出。

审核记录需要包含审核人、时间、对象版本、批准/拒绝结果和意见。通过 LangGraph interrupt 暂停的执行，应在持久化 checkpointer 中保存必要状态；恢复时使用同一 run/thread 标识，并再次校验被审核的资产版本是否已经变化。

---

## 8. 系统架构与技术选型

### 8.1 架构总览

```mermaid
flowchart LR
    UI[React / Next.js Web App] --> API[FastAPI API]
    API --> DB[(PostgreSQL)]
    API --> REDIS[(Redis)]
    API --> OBJ[(MinIO / S3 Object Storage)]
    API --> WF[Workflow Service / LangGraph]
    WF --> DB
    WF --> MODEL[Model Gateway]
    WF --> QUEUE[Celery Queue]
    QUEUE --> WORKER[Media Workers]
    WORKER --> MODEL
    WORKER --> OBJ
    WORKER --> DB
    WORKER --> FFMPEG[FFmpeg / ffprobe]
    WF --> OBS[OpenTelemetry / Langfuse]
    WORKER --> OBS
```

### 8.2 架构风格

推荐 **模块化单体 + 独立后台 Worker**：

- API、领域服务、Agent Runtime、资产服务可以放在同一个 Python 仓库内，以模块边界隔离职责。
- Worker 独立进程执行媒体生成与渲染，避免阻塞 API 服务。
- PostgreSQL 是业务事实的主存储。
- Redis 提供任务队列 Broker、限流或短期缓存，不作为正式资产状态的唯一来源。
- MinIO 或兼容 S3 的存储保存图片、音频、视频和大文件。
- Langfuse/OTel 用于运行链路、模型调用和评测追踪，不代替业务数据库。

不建议在 MVP 里拆成大量微服务。等到媒体 Worker 需要独立扩容、供应商适配器有独立部署需求，或团队边界明确时再拆分。

### 8.3 推荐技术栈

| 技术 | 作用 | 选择理由 |
|---|---|---|
| Python 3.12+（按依赖兼容性锁定） | 后端与 Agent 逻辑 | 生态适合模型调用、媒体处理和数据验证 |
| `uv` | 依赖与项目管理 | 锁定依赖、管理环境和运行命令 |
| FastAPI | REST API 与异步接口 | 支持类型驱动接口、自动 OpenAPI 文档 |
| Pydantic v2 | Agent 输入输出与 API Schema | 运行时结构校验与 JSON Schema |
| LangGraph | 多阶段 Agent 工作流、持久化与中断 | 将任务路由、条件分支与人工审核建模为状态图 |
| PostgreSQL | 主数据、资产版本、任务状态、审计 | 事务、约束、索引和关系查询 |
| SQLAlchemy 2.x + Alembic | ORM 与迁移 | 统一数据库访问和版本化迁移 |
| Redis | 队列 Broker、限流、短期缓存 | 支持异步任务与高频状态查询辅助能力 |
| Celery | 后台任务 | 媒体生成、渲染、重试与队列隔离 |
| MinIO / S3 | 对象存储 | 存放大文件与生成资产 |
| FFmpeg / ffprobe | 音视频合成与校验 | 时间轴渲染、编码、字幕、音频混合和媒体信息检查 |
| React + Next.js + TypeScript | Web 工作台 | 复杂资产工作区与任务面板 |
| OpenTelemetry | 标准化可观测性 | 将 API、Worker 和 Agent trace 关联起来 |
| Langfuse | LLM 追踪、数据集与实验 | 记录模型运行、提示词和评测结果 |
| Docker Compose | 本地开发与演示部署 | 将 API、Worker、DB、Redis、对象存储统一启动 |

版本号不要在文档中永久写死。实现时应在 `pyproject.toml` 和锁文件中锁定兼容版本，CI 中定期检查依赖更新。Celery 任务应设计为可安全重试，不能假设任务只会投递或执行一次。官方文档也建议任务函数尽可能保持幂等，并对可恢复错误设置重试策略：[Celery Tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)。

### 8.4 模块边界

- `api`：认证、请求校验、REST/SSE 接口，不直接实现复杂业务逻辑。
- `application`：用例编排，例如创建剧集、批准分镜、启动生成批次。
- `agents`：各专业 Agent 的提示词、Schema 和推理逻辑。
- `workflows`：LangGraph 状态、节点、边、暂停/恢复和 workflow 版本。
- `domain`：项目、剧集、资产、时间轴等领域实体及业务规则。
- `services`：资产管理、生成调度、媒体渲染、成本核算等应用服务。
- `providers`：图像、视频、语音、大模型供应商适配器。
- `persistence`：SQLAlchemy Models、Repositories、事务管理与迁移。
- `workers`：Celery 任务入口与媒体任务处理。
- `observability`：Trace、指标、日志和评测埋点。

---

## 9. 数据模型与资产版本管理

### 9.1 数据建模原则

- 核心关联与状态用关系型字段存储；可变的模型参数和扩展元数据可使用 JSONB。
- 大型媒体文件不存数据库二进制字段，只存对象存储 key、大小、MIME、哈希和元数据。
- 用户可见内容与生成执行记录分离。一个资产可有多个版本，一个版本也可能被多个任务引用。
- 所有有状态对象包含 `created_at`、`updated_at`；重要业务操作另记审计事件。
- 所有任务和资产使用 UUID 或同等不可预测 ID；不把连续数据库主键暴露给外部作为安全边界。
- 删除优先采用软删除或归档。物理清理需要检查是否还有引用和保留期限。

### 9.2 核心实体

| 实体 | 说明 | 关键字段示例 |
|---|---|---|
| `users` | 用户 | `id`, `email`, `display_name`, `status` |
| `projects` | 漫剧项目 | `id`, `owner_id`, `name`, `style_guide`, `output_profile`, `budget_limit` |
| `project_members` | 项目成员 | `project_id`, `user_id`, `role` |
| `episodes` | 剧集 | `id`, `project_id`, `episode_no`, `title`, `status`, `target_duration_ms` |
| `story_sources` | 原始故事与素材 | `id`, `episode_id`, `source_type`, `text`, `file_asset_id` |
| `scripts` | 剧本版本 | `id`, `episode_id`, `version_no`, `content_json`, `status`, `based_on_version_id` |
| `scenes` | 场景 | `id`, `episode_id`, `scene_no`, `script_version_id`, `content_json` |
| `characters` | 角色定义 | `id`, `project_id`, `name`, `canonical_description`, `status` |
| `locations` | 场景/地点定义 | `id`, `project_id`, `name`, `description`, `canonical_asset_id` |
| `visual_assets` | 角色图、场景图、道具图等 | `id`, `project_id`, `asset_type`, `name`, `status`, `current_version_id` |
| `asset_versions` | 不可变资产版本 | `id`, `asset_id`, `version_no`, `object_key`, `checksum`, `metadata_json`, `created_by` |
| `storyboards` | 分镜版本 | `id`, `episode_id`, `version_no`, `status`, `content_json` |
| `shots` | 镜头 | `id`, `storyboard_id`, `shot_no`, `duration_ms`, `description`, `status` |
| `shot_asset_links` | 镜头引用角色/场景/道具/参考素材 | `shot_id`, `asset_version_id`, `link_type` |
| `generation_jobs` | 通用生成任务 | `id`, `target_type`, `target_asset_id`, `input_manifest_id`, `task_type`, `provider`, `model`, `status`, `attempt_count`, `idempotency_key`, `provider_request_id` |
| `generation_attempts` | 每次真实调用尝试 | `id`, `job_id`, `attempt_no`, `request_fingerprint`, `provider_request_id`, `status`, `error_code`, `latency_ms`, `cost`, `submitted_at`, `finished_at` |
| `audio_clips` | 配音与音效资产 | `id`, `episode_id`, `line_id`, `asset_version_id`, `duration_ms` |
| `dialogue_lines` | 对白/旁白 | `id`, `script_version_id`, `character_id`, `text`, `line_type`, `sequence_no` |
| `timelines` | 时间轴版本 | `id`, `episode_id`, `version_no`, `timeline_json`, `status` |
| `render_jobs` | 渲染任务 | `id`, `timeline_id`, `output_profile`, `status`, `output_asset_version_id` |
| `workflow_runs` | Agent 工作流运行 | `id`, `episode_id`, `workflow_type`, `workflow_version`, `status`, `trace_id` |
| `workflow_events` | 状态事件/节点事件 | `id`, `run_id`, `event_type`, `payload_json`, `created_at` |
| `asset_dependencies` | 精确版本间的依赖边 | `source_asset_version_id`, `target_asset_version_id`, `dependency_type`, `dependency_manifest_id`, `created_at` |
| `reviews` | 人工审核记录 | `id`, `asset_type`, `asset_id`, `version_id`, `reviewer_id`, `decision`, `comment` |
| `audit_logs` | 审计日志 | `id`, `actor_id`, `action`, `resource_type`, `resource_id`, `metadata_json` |
| `outbox_events` | 事务性事件发件箱 | `id`, `event_type`, `payload_json`, `published_at`, `attempt_count` |

实际实现不必一开始建立所有表，但应尽早区分资产当前指针、不可变资产版本和后台执行尝试，否则后续补版本与追踪会很困难。

### 9.3 资产版本状态

建议定义统一状态：

- `DRAFT`：草稿，尚未审核。
- `READY_FOR_REVIEW`：已生成，等待审核。
- `APPROVED`：已审核通过。
- `REJECTED`：审核未通过。
- `LOCKED`：已锁定，自动任务不能静默替换。
- `STALE`：依赖的上游版本已变化，当前版本不再是最新生产候选。
- `ARCHIVED`：归档，不默认用于新任务。

资产“当前版本”只是一个指针。旧版本必须保留其对象 key、哈希、来源和参数，以支持回溯、重现和恢复。

### 9.4 依赖边与失效传播

`asset_dependencies` 至少区分：

- `CONTENT_DEPENDENCY`：内容直接来源，例如语音来自对白文本。
- `STYLE_DEPENDENCY`：依赖画风/角色/场景规范。
- `SEQUENCE_DEPENDENCY`：依赖镜头顺序或时间轴结构。
- `REFERENCE_DEPENDENCY`：生成时使用的参考图。
- `RENDER_DEPENDENCY`：时间轴、音频、字幕共同构成成片。

失效传播规则由代码实现，而不是由 LLM 每次临时决定。系统可以先把受影响资产标记为 `STALE`，再由用户或 Producer 工作流选择局部重建。对于依赖图更新，必须检查是否形成循环；生产依赖图要求是有向无环图（DAG）。

### 9.5 示例：Shot 结构

以下是概念性 JSON Schema 示例，实际模型应在代码中使用 Pydantic 定义：

```json
{
  "shot_no": 12,
  "scene_id": "scene-uuid",
  "shot_type": "medium",
  "duration_ms": 4500,
  "visual_description": "雨夜，角色 A 站在便利店门口，回头看向巷子深处",
  "characters": [
    {
      "character_id": "character-a-uuid",
      "costume_asset_version_id": "costume-v3-uuid",
      "expression": "警惕",
      "action": "缓慢回头"
    }
  ],
  "location_asset_version_id": "rainy-street-v2-uuid",
  "prop_asset_version_ids": [],
  "camera": {
    "shot_size": "medium",
    "movement": "slow_push_in",
    "composition": "character_right_third"
  },
  "dialogue_line_ids": ["line-uuid"],
  "transition_out": "cut",
  "selected_visual_asset_version_id": null,
  "review_status": "draft"
}
```

### 9.6 数据库约束与索引

建议设置：

- `UNIQUE(project_id, episode_no)`：项目内集数唯一。
- `UNIQUE(asset_id, version_no)`：资产版本号唯一。
- `UNIQUE(job_id, attempt_no)`：任务尝试号唯一。
- `UNIQUE(provider, provider_request_id)`（允许为空）：关联供应商请求。
- `UNIQUE(idempotency_key)`：对需要幂等的命令或任务保证重复请求可识别。
- 对 `project_id`、`episode_id`、`status`、`created_at` 和外键设置合理索引。
- 对 `asset_dependencies(source_asset_version_id)` 和 `asset_dependencies(target_asset_version_id)` 建索引。
- `generation_jobs.target_asset_id` 可为空（例如生成候选结果后才注册正式资产），但任务必须有明确的 `target_type` 和 `input_manifest_id`。
- `asset_dependencies` 两端都指向不可变的 `asset_versions`，而不是可变化的资产“当前版本”指针。新增依赖边时必须检查项目边界并检测循环。
- 对 JSONB 的高频查询字段优先提升为真实列，不要把所有字段都塞入 JSONB 后依赖复杂表达式查询。

---

## 10. API 设计

### 10.1 API 约定

- 前缀：`/api/v1`。
- 成功响应采用清晰的资源结构；错误响应使用统一错误码、消息、request ID 和可安全公开的细节。
- 长任务接口返回 `202 Accepted` 和任务/运行 ID。
- 支持分页、排序和过滤；限制分页上限。
- 写操作需要鉴权、项目权限检查及适当的幂等控制。
- 不在 URL、日志或异常中暴露模型 API Key、对象存储凭证和内部绝对路径。
- 对可能涉及大文件的请求使用预签名上传/下载地址，不通过 API 进程转发大量媒体二进制。

### 10.2 项目与剧集

| Method | Endpoint | 说明 |
|---|---|---|
| `POST` | `/api/v1/projects` | 创建项目 |
| `GET` | `/api/v1/projects` | 项目列表 |
| `GET` | `/api/v1/projects/{project_id}` | 项目详情 |
| `PATCH` | `/api/v1/projects/{project_id}` | 修改项目配置 |
| `POST` | `/api/v1/projects/{project_id}/episodes` | 创建剧集 |
| `GET` | `/api/v1/episodes/{episode_id}` | 获取剧集详情与进度 |
| `PATCH` | `/api/v1/episodes/{episode_id}` | 修改剧集信息 |

### 10.3 剧本与分镜

| Method | Endpoint | 说明 |
|---|---|---|
| `POST` | `/api/v1/episodes/{episode_id}/script-runs` | 启动剧本生成/改编任务 |
| `GET` | `/api/v1/scripts/{script_id}/versions` | 查询剧本版本 |
| `POST` | `/api/v1/scripts/{script_id}/reviews` | 审核剧本版本 |
| `POST` | `/api/v1/episodes/{episode_id}/storyboard-runs` | 启动分镜生成任务 |
| `GET` | `/api/v1/episodes/{episode_id}/shots` | 查询镜头列表 |
| `PATCH` | `/api/v1/shots/{shot_id}` | 修改镜头结构化内容 |
| `POST` | `/api/v1/shots/{shot_id}/reviews` | 审核镜头或分镜资产 |

### 10.4 资产与生成任务

| Method | Endpoint | 说明 |
|---|---|---|
| `POST` | `/api/v1/assets/upload-url` | 创建上传地址 |
| `POST` | `/api/v1/assets/{asset_id}/versions` | 注册新资产版本 |
| `GET` | `/api/v1/assets/{asset_id}` | 查询资产和当前版本 |
| `GET` | `/api/v1/assets/{asset_id}/versions` | 查询资产历史版本 |
| `POST` | `/api/v1/shots/{shot_id}/generation-runs` | 为镜头创建生成任务 |
| `GET` | `/api/v1/generation-jobs/{job_id}` | 查询生成状态和结果 |
| `POST` | `/api/v1/generation-jobs/{job_id}/retry` | 手动重试 |
| `POST` | `/api/v1/generation-jobs/{job_id}/cancel` | 请求取消任务 |
| `POST` | `/api/v1/episodes/{episode_id}/batch-generation-runs` | 批量生成镜头素材 |

### 10.5 音频、时间轴与渲染

| Method | Endpoint | 说明 |
|---|---|---|
| `POST` | `/api/v1/episodes/{episode_id}/voice-runs` | 启动配音批次 |
| `GET` | `/api/v1/episodes/{episode_id}/dialogue-lines` | 查询对白与配音关联 |
| `POST` | `/api/v1/episodes/{episode_id}/timelines` | 创建时间轴版本 |
| `GET` | `/api/v1/timelines/{timeline_id}` | 查询时间轴 |
| `PATCH` | `/api/v1/timelines/{timeline_id}` | 更新时间轴草稿 |
| `POST` | `/api/v1/timelines/{timeline_id}/render-runs` | 创建渲染任务 |
| `GET` | `/api/v1/render-jobs/{job_id}` | 查询渲染状态 |
| `GET` | `/api/v1/episodes/{episode_id}/deliveries` | 查询成片与交付报告 |

### 10.6 工作流与可观测性

| Method | Endpoint | 说明 |
|---|---|---|
| `GET` | `/api/v1/runs/{run_id}` | 工作流状态 |
| `GET` | `/api/v1/runs/{run_id}/events` | 查询事件或日志摘要 |
| `POST` | `/api/v1/runs/{run_id}/resume` | 提交审核意见并恢复暂停任务 |
| `POST` | `/api/v1/runs/{run_id}/cancel` | 取消工作流 |
| `GET` | `/api/v1/projects/{project_id}/costs` | 项目成本统计 |
| `GET` | `/api/v1/projects/{project_id}/dependency-impact?asset_version_id=...` | 查询资产变更影响 |

### 10.7 长任务响应示例

```json
{
  "run_id": "0d94e8e9-43d1-4df0-a9cc-d9877df2b82d",
  "status": "QUEUED",
  "task_type": "episode_storyboard_generation",
  "episode_id": "5fe4d01c-f6cc-4f75-8bdd-92417f8d2f19",
  "status_url": "/api/v1/runs/0d94e8e9-43d1-4df0-a9cc-d9877df2b82d",
  "events_url": "/api/v1/runs/0d94e8e9-43d1-4df0-a9cc-d9877df2b82d/events"
}
```

### 10.8 错误响应示例

```json
{
  "error": {
    "code": "ASSET_VERSION_CONFLICT",
    "message": "当前镜头引用的角色参考图已更新，请刷新后再提交。",
    "request_id": "req-7b9e...",
    "details": {
      "asset_id": "character-asset-uuid",
      "expected_version": 3,
      "current_version": 4
    }
  }
}
```

错误细节不能包含密钥、内部文件路径、原始敏感数据或完整供应商异常堆栈。

### 10.9 SSE 事件类型

前端可以订阅任务事件，推荐事件名称：

- `run.started`
- `run.status_changed`
- `agent.started`
- `agent.completed`
- `generation.queued`
- `generation.progress`
- `generation.completed`
- `generation.failed`
- `review.required`
- `review.completed`
- `asset.stale`
- `render.progress`
- `render.completed`
- `run.completed`
- `run.failed`

SSE 仅用于状态通知，不应作为唯一的任务数据来源。客户端重连后必须能够通过事件 ID 或查询 API 补取遗漏状态。

---

## 11. 模型服务与媒体生成管线

### 11.1 Provider Adapter

模型厂商的 SDK 和参数各不相同，建议定义统一接口：

```python
from dataclasses import dataclass
from typing import Protocol, Any


@dataclass
class GenerationRequest:
    task_type: str
    prompt: str
    negative_prompt: str | None
    reference_asset_uris: list[str]
    parameters: dict[str, Any]
    idempotency_key: str


@dataclass
class GenerationResult:
    provider: str
    model: str
    provider_request_id: str | None
    output_uris: list[str]
    usage: dict[str, Any]
    estimated_cost: float | None
    metadata: dict[str, Any]


class MediaProvider(Protocol):
    async def submit(self, request: GenerationRequest) -> str: ...
    async def poll(self, provider_job_id: str) -> dict[str, Any]: ...
    async def fetch_result(self, provider_job_id: str) -> GenerationResult: ...
    async def cancel(self, provider_job_id: str) -> bool: ...
```

这是接口草图，具体异步语义需要结合供应商能力调整。有的供应商可以异步提交并轮询，有的可能使用 Webhook，有的只有同步 API。统一适配层要明确区分“已提交”“正在执行”“成功”“失败”和“结果未知”。

### 11.2 Model Gateway

Model Gateway 统一负责：

- Provider 选择与模型能力注册。
- 按任务类型选择可用模型，例如文本、图像、视频或 TTS。
- 超时、限流、重试和熔断。
- API Key 读取与脱敏日志。
- 请求参数归一化与模型配置版本。
- Token/图片/秒数等用量记录以及成本估算。
- 备用 Provider 策略（仅在功能兼容、成本与版权要求允许时启用）。

不要默认把所有供应商的参数都映射成完全相同的能力。Provider Adapter 应提供 `capabilities`，明确支持的输入图像数、长宽比、输出尺寸、视频时长、同步/异步方式和取消能力。

### 11.3 画面生成步骤

建议的单镜头生产管线：

1. 加载镜头版本和引用的角色/场景/道具资产版本。
2. Context Builder 组装项目风格、镜头描述、参考图和负面约束。
3. Prompt Composer 输出最终提示词，并保存模板版本与输入资产清单。
4. 通过 Model Gateway 选择图像模型和参数。
5. 提交后台生成任务。
6. Worker 轮询状态或接收 Webhook。
7. 下载生成结果到临时对象存储位置。
8. 检查 MIME、扩展名、文件头、尺寸、像素数量、解码能力和哈希。
9. 创建候选 `asset_version`，保存结果元数据；候选版本与正式采用版本分离。
10. 运行确定性质量检查，先阻断无法解码、尺寸不符、引用缺失等基础问题。
11. 对通过基础检查的每张候选图片调用必需的 Continuity Reviewer，并保存所用输入版本、结论、证据、风险等级和置信度。
12. 根据审查结果路由：`PASS` 进入候选素材人工选择/后续审核，`REVISION_REQUIRED` 进入提示词修订或局部重生成，`HUMAN_REVIEW` 暂停并等待人工处理。Continuity Reviewer 不直接把素材标记为最终通过。
13. 只有候选版本通过工作流要求的审核并被选用后，才更新资产的 `current_version_id`；必要时标记时间轴和成片为 stale。新候选版本必须重新运行连续性审查。

### 11.4 Prompt Composer

建议采用分块构建提示词，而不是把所有内容放在一条手工拼接的长字符串中：

- `project_style`：项目画风、渲染与配色。
- `character_constraints`：当前镜头涉及的角色外观与服装。
- `location_constraints`：场景布局、时间和光照。
- `shot_description`：当前镜头动作、构图和情绪。
- `camera_language`：景别、视角和运镜意图。
- `continuity_context`：上一镜头/下一镜头必须保持的事实。
- `negative_constraints`：禁止出现的元素。
- `provider_parameters`：模型参数，不作为自然语言提示词的一部分。

保存结构化 Prompt Manifest，包含每个块的内容和来源资产 ID。这样提示词出了问题时，可以查到冲突来自哪一项设定。

### 11.5 视频合成

MVP 的动态漫剧以静态镜头素材为主，使用 FFmpeg 进行：

- 统一画幅和分辨率。
- 镜头持续时间控制。
- 轻量推拉、平移或裁切效果。
- 转场（如淡入淡出、硬切）。
- 配音、环境音和背景音乐混音。
- 字幕烧录或输出独立字幕文件。
- 视频编码与最终封装。

FFmpeg 命令必须由后端根据经过校验的渲染计划生成。不得接受前端直接传来的任意命令、过滤器字符串或输出路径。对于用户上传的媒体文件，在进入 FFmpeg 前需限制文件格式、分辨率、时长和资源消耗，必要时在独立容器或受限 Worker 中处理。

### 11.6 渲染清单（Render Manifest）

每次渲染都生成不可变清单，至少记录：

- 时间轴版本 ID。
- 镜头顺序、片段资产版本和片段时长。
- 配音、音效、音乐和字幕版本。
- 输出配置（宽、高、帧率、编码、码率、音频设置）。
- FFmpeg 版本、渲染器版本、创建时间。
- 最终产物对象 key、哈希、时长、文件大小和检查报告。

相同清单与相同软件版本能提高重现性，但由于编码、硬件、外部素材或模型输出可能带来差异，不能无条件承诺字节级完全相同。

---

## 12. 质量控制与连续性管理

### 12.1 质量门禁

质量控制分为确定性检查、模型辅助检查和人工审核三层。

| 层级 | 检查示例 | 处理结果 |
|---|---|---|
| L1：确定性检查 | Schema、角色 ID、文件解码、尺寸、时长、字幕越界、音轨存在 | 自动阻断或通过 |
| L2：Continuity Reviewer 连续性审查 | 每张候选镜头图是否表达分镜动作、角色/服装/场景/道具是否与锁定参考一致、相邻镜头是否存在明显矛盾 | 必须执行；输出结构化结论、证据和风险，低置信度或高风险问题转人工审核 |
| L3：人工审核 | 角色主参考图、关键剧情镜头、最终成片、版权/内容敏感项 | 批准、驳回或要求修订 |

任何单一视觉模型的相似度分数都不能被视为绝对真值。视觉连续性分数应与固定角色参考、人工审查和实际失败案例一起使用。

### 12.2 连续性检查规则

**角色连续性**

- 角色 ID 和当前服装版本是否与镜头设定一致。
- 是否违反发色、发型、显著面部特征等锁定属性。
- 同一场景连续镜头是否出现无解释的服装或配饰变化。

**空间与道具连续性**

- 场景是否保持应有的空间布局与时间条件。
- 关键道具的持有者和状态是否与剧本一致。
- 连续镜头中的动作和视线是否存在明显矛盾。

**叙事连续性**

- 角色是否在本集已出场或已建立。
- 镜头是否引用了不存在的对白或场景。
- 台词、字幕、旁白和画面中的人物是否一致。

**媒体完整性**

- 图片可以解码、分辨率符合最低要求。
- 音频可播放、时长非零，且未意外截断。
- 视频具备有效视频流和预期音轨。
- 时间轴没有无意的空洞、负时长或超出成片范围的字幕。

### 12.3 失败动作策略

- 确定性错误：自动阻断，并提供具体错误字段。
- 可通过调整参数处理的问题：允许在预算范围内重新生成。
- 低置信度视觉问题：进入审核队列，避免无休止地自动重试。
- 角色参考资产问题：回退到已批准参考图，或要求用户批准新版本。
- 多次生成不达标：标记为 `NEEDS_REVIEW`，不要继续消耗预算。

### 12.4 交付验收报告

每次正式导出都生成一份验收报告：

```json
{
  "delivery_id": "delivery-uuid",
  "timeline_version": 4,
  "render_status": "passed",
  "checks": {
    "video_decodable": true,
    "audio_stream_present": true,
    "subtitle_range_valid": true,
    "missing_required_shots": 0,
    "missing_audio_lines": 0,
    "stale_dependencies": 0
  },
  "warnings": [],
  "reviewed_by_user_id": "user-uuid"
}
```

正式交付应能从该报告追溯回时间轴、镜头、配音与素材版本。

---

## 13. 前端信息架构

### 13.1 主导航

1. 工作台 Dashboard
2. 我的项目 Projects
3. 剧集 Episodes
4. 资产中心 Assets
5. 生成任务 Generation Jobs
6. 审核中心 Reviews
7. 时间轴与成片 Timeline / Deliveries
8. 运行日志与成本 Observability
9. 设置 Settings

### 13.2 项目总览页

展示项目基本信息、剧集列表、整体进度、最近生成任务、待审核资产和累计成本。用户可以快速进入某一集的生产工作区。

### 13.3 剧集工作区

建议使用横向阶段导航：

`剧本 → 角色与场景 → 分镜 → 画面/视频 → 配音/字幕 → 时间轴 → 导出`

每个阶段显示状态和阻塞原因。未满足前置条件的阶段允许查看，但不能无提示地启动生产。

### 13.4 分镜工作台布局

推荐采用三栏布局：

- 左侧：场景列表与镜头列表。
- 中间：当前镜头预览、备选生成结果和画面批注。
- 右侧：镜头属性、角色/场景引用、提示词组成、任务状态和连续性报告。

支持快捷审核、比较不同版本、选择候选图和局部重生成。批量操作前显示涉及的镜头数和预算估算。

### 13.5 生产任务页面

通过时间线或事件列表展示 Agent 节点、媒体任务、审核节点、重试和失败事件。注意区分面向用户的摘要与内部调试信息，避免向普通用户暴露系统提示词、密钥或内部堆栈。

### 13.6 时间轴编辑器

MVP 可以采用简化的轨道编辑器，不必追求专业非线性剪辑软件的所有功能。至少支持：

- 镜头片段顺序、时长和裁切。
- 配音、音乐、音效轨道。
- 字幕文本、出现和消失时间。
- 预览播放、渲染设置、保存新版本和重新渲染。

时间轴数据以结构化 JSON 保存，不能把前端状态作为唯一数据来源。保存时需进行版本检查，避免多人或多个标签页覆盖彼此修改。

---

## 14. 安全、权限与成本治理

### 14.1 身份与权限

- API 使用标准认证机制，密码不以明文储存。
- 每个资源操作校验当前用户是否属于对应项目及具备所需角色。
- Viewer 只能读取；Editor 可编辑草稿和创建生成任务；Owner 可批准关键资产、变更预算和删除/归档项目；Admin 管理系统配置。
- 生成任务在创建时记录请求用户和所属项目，Worker 执行时仍需依据任务记录确认作用域。
- 对象存储通过短期预签名 URL 上传/下载，避免公开桶和永久可访问链接。

### 14.2 文件上传安全

- 校验允许的 MIME、扩展名、文件头和文件大小。
- 不信任客户端文件名，服务端生成存储 key。
- 限制图像像素、视频时长、音频长度和压缩炸弹风险。
- 对上传文档做大小、页数和解析时长限制。
- 使用 ffprobe 或图像解码库检查媒体，不直接执行用户提供的命令或脚本。
- 生产环境可为媒体解析与渲染 Worker 设置 CPU、内存、磁盘和执行时间限制。

### 14.3 Prompt Injection 与不可信内容

小说、上传文件、角色描述和外部资料都属于不可信内容。文档或故事中出现“忽略之前规则”“读取环境变量”等文字时，系统应把它当作待处理文本，而不是系统指令。

- Agent 工具权限通过服务端能力白名单控制。
- 资产内容和系统提示词分区传递，并明确将用户提供的文本标记为数据。
- 不向内容生成 Agent 暴露环境变量、数据库凭据或云存储密钥。
- 对用户提供的文字和图片进行内容策略校验（具体策略按使用场景配置）。
- 模型生成的 JSON 在进入任何业务操作前都要经过 Schema 和业务规则验证。

### 14.4 模型预算治理

每个项目可设置：

- 单次任务最大预算。
- 单集预算。
- 项目累计预算。
- 每个镜头最大候选数量。
- 最大自动重试次数。
- 单个 Agent 的最大调用次数。
- 单个任务最大执行时长。

提交批量任务前，系统应给出估算成本或明确说明无法准确估算。预算预留与成本结算应通过数据库事务或幂等记录处理，避免并发任务同时超过预算。

### 14.5 审计和数据保留

审计事件包括创建、修改、批准、驳回、重生成、导出、删除、预算变更和模型配置变更。审计日志应具备追加写入的应用约束，普通业务编辑接口不得随意修改历史事件。

对用户故事文本、配音和图片的存储时长应提供清晰说明。删除项目时要区分逻辑归档、撤销公开访问和实际物理删除；还应考虑备份、任务队列和生成供应商侧数据的保留情况。

---

## 15. 可观测性与评测体系

### 15.1 Trace 关联

贯穿以下链路的统一关联 ID：

`HTTP Request → Workflow Run → Agent Node → Model Call → Generation Job → Worker Attempt → Asset Version → Render Job`

推荐将 `request_id`、`run_id`、`generation_job_id`、`asset_id`、`episode_id` 作为结构化日志字段。Trace 记录应避免默认存储完整的私密内容；可用内容摘要、哈希、受控采样和显式调试开关降低数据泄露风险。

### 15.2 核心运营指标

| 指标 | 定义 | 用途 |
|---|---|---|
| Workflow Completion Rate | 成功完成的工作流 / 已结束工作流 | 观察总体可靠性 |
| Generation Success Rate | 成功生成任务 / 已结束生成任务 | 监测模型与任务管线稳定性 |
| First Attempt Success Rate | 首次尝试成功的任务 / 总任务 | 了解重试依赖程度 |
| Retry Rate | 至少发生一次重试的任务比例 | 定位供应商或任务配置问题 |
| Human Review Rejection Rate | 被审核驳回的结果 / 已审核结果 | 识别生成质量问题 |
| Stale Asset Count | 当前被标记为过期的下游资产数量 | 监控变更影响处理情况 |
| Render Failure Rate | 渲染失败任务 / 总渲染任务 | 评估媒体管线稳定性 |
| p50/p95 Task Latency | 任务耗时分位数 | 观察性能与队列积压 |
| Cost per Episode | 单集模型与计算成本 | 控制生产成本 |
| Cost per Accepted Shot | 总生成成本 / 被接受的镜头数 | 估算有效素材成本 |
| Budget Overrun Rate | 超预算任务比例 | 验证预算治理机制 |

### 15.3 评测维度

评测不只关注模型“写得好不好”，还要衡量结构、流程和最终产物。

**结构化输出**

- Pydantic Schema 通过率。
- 引用 ID 有效率。
- 必需字段完整率。
- 重复角色/场景标识率。

**创作与连续性**

- 人工接受的剧本/分镜比例。
- 镜头描述与剧本内容的一致性抽样评分。
- 角色外观/服装连续性审核通过率。
- 返工率与平均每个通过镜头的生成次数。

**媒体交付**

- 可解码产物比例。
- 音轨/字幕缺失率。
- 交付完整性检查通过率。
- 成片时长与目标时长偏差。

**运行与成本**

- 平均工作流耗时、p95 耗时。
- 每集总成本与每个被接受镜头成本。
- 任务重试率、取消率和人工介入率。

### 15.4 固定数据集与回归测试

构建版本化的离线测试集，每条样本包括：输入故事、项目视觉规范、目标时长、预期角色集合、必需场景、必须出现的事件和验收规则。可以为同一批输入运行不同的 Prompt、模型配置或工作流版本，并比较结果。

- 结构、引用、状态和媒体完整性优先用代码评分。
- 叙事质量、画面一致性等主观指标通过人工标注和辅助模型评分结合。
- 对评测输出记录样本 ID、模型版本、Prompt 版本、工作流版本、成本和分数。
- 当关键结构有效率或媒体通过率下降超过阈值时，阻止相关版本进入默认配置。

Langfuse 的 Datasets/Experiments 可以用于保存输入与期望输出、执行固定数据集上的实验并比较分数；业务数据和资产仍应保存在自己的数据库中。参考：[Datasets](https://langfuse.com/docs/evaluation/experiments/datasets)、[Evaluation Overview](https://langfuse.com/docs/evaluation/overview)。

---

## 16. 测试策略

### 16.1 单元测试

重点测试：

- Pydantic Schema 与业务规则。
- 项目/剧集状态转换。
- 资产依赖失效传播。
- 版本冲突与乐观锁。
- 预算预留、结算和并发扣减。
- 生成任务重试分类和退避计算。
- 时间轴时长计算、字幕边界和媒体输出参数验证。
- Provider Adapter 的响应归一化。

### 16.2 集成测试

使用独立测试数据库和对象存储测试环境，验证：

- 创建剧集、保存剧本版本、创建分镜。
- 生成任务从入队、执行、注册资产到通知工作流的完整链路。
- 失败事件进入重试或死信处理。
- outbox 事件被可靠发布与去重消费。
- 人工审核暂停后可以恢复工作流。
- 资产被更新后只让正确的下游依赖失效。

### 16.3 媒体管线测试

- 使用固定测试图片、音频和字幕片段进行离线渲染。
- 使用 ffprobe 检查视频宽高、帧率、时长、音视频流。
- 使用抽样解码确保产物实际可读。
- 验证时间轴中没有负时长、重叠错误或超出范围的字幕。
- 验证同一时间轴配置重复渲染不会修改输入资产或旧版成片。

### 16.4 Agent 工作流测试

模型调用通过 Fake Provider 或录制的结构化响应进行确定性测试，以避免所有测试都产生真实模型费用。重点覆盖：

- Agent 输出不符合 Schema。
- Agent 返回不存在的资产 ID。
- 上游资产在工作流运行中被修改。
- 用户在审核暂停期间拒绝或修改内容。
- 连续性检查不通过。
- 达到调用预算或重试上限。
- 多镜头并行生成时某一部分失败。
- 模型供应商不可用或超时。

### 16.5 端到端验收用例

至少准备以下演示级用例：

1. 输入故事并生成剧本、角色和分镜。
2. 修改剧本中的一句对白，系统仅使对应配音与字幕失效。
3. 批量生成 10 个镜头，模拟 2 个任务失败，验证其他镜头不被重复生成。
4. 锁定角色主参考图，验证批量生成不能自动覆盖该参考资产。
5. 渲染时故意提供无效媒体文件，验证渲染失败被记录且不会误标记为完成。
6. 在工作流等待人工审核时重启应用，验证状态可以恢复。
7. 触发项目预算上限，验证系统阻止继续创建生成任务。

---

## 17. 部署方案与配置

### 17.1 本地 Docker Compose 服务

开发环境建议至少包含：

- `api`：FastAPI 服务。
- `worker`：通用媒体和生成任务 Worker。
- `scheduler`（可选）：周期性任务、过期任务巡检和 outbox 派发。
- `postgres`：业务数据库与持久化状态。
- `redis`：Celery Broker 和短期缓存。
- `minio`：媒体对象存储。
- `frontend`：Web 应用开发或预览服务。
- `langfuse`：可以先作为外部云服务或可选 profile，不要求 MVP 默认启动全套观测平台。

### 17.2 配置与环境变量

`.env.example` 只列变量名和安全的示例占位符，不提交真实密钥。建议包括：

```dotenv
APP_ENV=development
APP_LOG_LEVEL=INFO
APP_SECRET_KEY=replace-me
DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/Arclight
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
S3_ENDPOINT_URL=http://minio:9000
S3_BUCKET=Arclight-assets
S3_ACCESS_KEY=replace-me
S3_SECRET_KEY=replace-me
LLM_PROVIDER=your-provider
LLM_API_KEY=replace-me
IMAGE_PROVIDER=your-image-provider
IMAGE_API_KEY=replace-me
VIDEO_PROVIDER=optional-provider
VIDEO_API_KEY=replace-me
TTS_PROVIDER=your-tts-provider
TTS_API_KEY=replace-me
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=
```

生产部署应使用密钥管理系统、容器 Secret 或环境注入机制，不应把 `.env` 打进镜像，也不应把密钥输出到日志。

### 17.3 开发命令设计

建议在 `pyproject.toml`、Makefile 或 `justfile` 中统一命令：

```bash
# 安装依赖
uv sync

# 启动基础设施
 docker compose up -d postgres redis minio

# 执行数据库迁移
uv run alembic upgrade head

# 启动 API
uv run uvicorn serviceops.api.main:app --reload --host 0.0.0.0 --port 8000

# 启动 Worker（按实际 Celery app 路径调整）
uv run celery -A serviceops.workers.celery_app worker --loglevel=INFO

# 运行测试
uv run pytest

# 运行代码检查
uv run ruff check .
uv run ruff format --check .
```

最终命令路径应以项目实际模块为准；README 必须给出在全新环境中可复制执行的完整流程。

### 17.4 备份与恢复

- PostgreSQL 定期备份，并实际演练恢复流程。
- 对象存储启用版本化或应用层不可变版本策略。
- 数据库只存对象 key，恢复时需要确保对应对象仍存在。
- 对象清理任务必须检查资产引用和保留期，不能仅依赖文件名或时间删除。
- 提供开发环境重置命令，但生产环境禁止使用无确认的全量删除操作。

---

## 18. 代码仓库结构

建议采用以下结构，按模块渐进实现：

```text
Arclight/
├── README.md
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── .env.example
├── alembic.ini
│
├── apps/
│   ├── api/
│   │   └── main.py
│   └── worker/
│       └── main.py
│
├── src/Arclight/
│   ├── api/
│   │   ├── routes/
│   │   ├── dependencies.py
│   │   ├── schemas/
│   │   └── errors.py
│   ├── application/
│   │   ├── projects.py
│   │   ├── episodes.py
│   │   ├── production_runs.py
│   │   └── deliveries.py
│   ├── domain/
│   │   ├── projects/
│   │   ├── scripts/
│   │   ├── storyboards/
│   │   ├── assets/
│   │   ├── generation/
│   │   ├── timeline/
│   │   └── policies/
│   ├── agents/
│   │   ├── producer.py
│   │   ├── screenwriter.py
│   │   ├── director.py
│   │   ├── art_director.py
│   │   ├── generation_agent.py
│   │   ├── continuity.py
│   │   ├── editor.py
│   │   └── prompts/
│   ├── workflows/
│   │   ├── state.py
│   │   ├── graph_factory.py
│   │   ├── episode_production.py
│   │   ├── review_interrupts.py
│   │   └── transitions.py
│   ├── services/
│   │   ├── asset_service.py
│   │   ├── dependency_service.py
│   │   ├── generation_service.py
│   │   ├── render_service.py
│   │   ├── budget_service.py
│   │   └── review_service.py
│   ├── providers/
│   │   ├── llm/
│   │   ├── image/
│   │   ├── video/
│   │   └── tts/
│   ├── workers/
│   │   ├── celery_app.py
│   │   ├── generation_tasks.py
│   │   ├── render_tasks.py
│   │   └── maintenance_tasks.py
│   ├── persistence/
│   │   ├── models/
│   │   ├── repositories/
│   │   ├── session.py
│   │   └── unit_of_work.py
│   ├── storage/
│   │   ├── object_store.py
│   │   └── media_validation.py
│   ├── observability/
│   └── core/
│       ├── config.py
│       ├── logging.py
│       └── security.py
│
├── frontend/
│   ├── package.json
│   └── src/
│       ├── app/
│       ├── components/
│       ├── features/
│       ├── hooks/
│       └── lib/api-client.ts
│
├── migrations/
├── evals/
│   ├── datasets/
│   ├── evaluators/
│   └── run_eval.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── workflow/
│   ├── media/
│   └── e2e/
└── docs/
    ├── architecture.md
    ├── api.md
    ├── threat-model.md
    ├── runbook.md
    └── adr/
```

不必在第一天创建所有空目录。目录应随着模块实际出现而增加，且应避免 `utils.py` 逐渐变成没有边界的万能模块。

---

## 19. 开发里程碑与验收标准

### Phase 0：项目骨架与领域设计

**实现内容**

- 初始化 Python/uv、FastAPI、前端和 Docker Compose。
- 建立配置、日志、错误响应、数据库连接、迁移和基础 CI。
- 实现项目、剧集和资产的基本 CRUD。

**验收标准**

- 新环境按照 README 可以启动。
- 数据库迁移可从空库执行。
- CI 运行格式检查、静态检查和测试。

### Phase 1：剧本和分镜 MVP

**实现内容**

- 故事输入和剧本结构化生成。
- 角色/场景定义。
- 分集剧本版本与人工审核。
- 分镜结构化生成、列表和编辑界面。

**验收标准**

- 一段测试故事可以生成符合 Schema 的剧本和分镜。
- 用户能修改并保存新版本。
- 不存在的角色/场景引用会被阻断。

### Phase 2：资产中心与图像生成

**实现内容**

- 对象存储与资产版本系统。
- Provider Adapter 和 Model Gateway。
- 单镜头生成、批量生成、候选结果选择。
- 任务队列、重试、任务状态展示。

**验收标准**

- 至少接入一个真实图像生成 Provider。
- 生成失败时保留错误和 attempt 记录。
- 一个镜头失败不会导致整批其他镜头被清空或重新执行。

### Phase 3：Harness 与连续性检查

**实现内容**

- 资产依赖 DAG。
- stale 传播与局部重建。
- 关键参考资产锁定。
- 确定性 QA、Continuity Agent、人工审核和 LangGraph 中断恢复。

**验收标准**

- 修改对白会使相关配音/字幕 stale，但不会错误地使无关场景图片失效。
- 用户拒绝审核后，后续阶段不会继续执行。
- 应用重启后，可从持久化状态恢复暂停的工作流。

### Phase 4：配音、字幕、时间轴和导出

**实现内容**

- TTS Provider。
- 结构化对白与字幕时间。
- 时间轴编辑器。
- FFmpeg 渲染、验收报告和成片下载。

**验收标准**

- 成功导出一段 1–3 分钟竖屏 MP4。
- 视频包含画面、可听音轨和字幕。
- 产物通过 ffprobe/解码及输出规格检查。

### Phase 5：生产运营化

**实现内容**

- 任务中心、日志和成本面板。
- 限流、预算、超时、死信与清理任务。
- 版本对比、审计和可复现评测。
- 完善端到端测试、文档和演示数据。

**验收标准**

- 可以从前端启动一集制作、审核关键资产并导出成片。
- 可以追踪任意成片使用的剧本、分镜、角色参考图、配音与时间轴版本。
- 关键异常有可读错误、任务状态和恢复路径。

### 推荐优先级

| 优先级 | 能力 |
|---|---|
| P0 | 项目/剧集、剧本、分镜、角色/场景资产、生成任务、Continuity Reviewer 连续性审查、质量门禁、FFmpeg 成片、任务追踪 |
| P1 | 资产依赖图、精细化局部重建、审核暂停/恢复增强、成本治理与运营指标 |
| P2 | 复杂时间轴、多供应商自动路由、视频生成、自动化角色一致性量化评分、团队协作 |
| P3 | 口型同步、复杂动画、批量多集自动化、商业化计费、多租户部署 |

---

## 20. 演示方案与简历呈现

### 20.1 演示故事线

准备一段原创、长度适中的故事，目标制作 1–3 分钟竖屏动态漫剧。演示时按以下顺序展示：

1. 输入故事和制作规格。
2. 展示 Screenwriter Agent 生成剧本，以及人工修改和版本差异。
3. 展示角色设定、视觉规范和分镜表。
4. 批量生成镜头，其中人为制造一个失败任务，展示 Worker 重试与部分结果保留。
5. 修改一句台词，展示资产依赖图只让相关音频和字幕 stale。
6. 展示 Continuity Agent 的问题标注，以及人工审核后继续执行。
7. 在时间轴组装画面、配音、字幕和背景音乐。
8. 导出 MP4 并展示自动验收报告。
9. 打开任务 Trace，查看模型调用、执行耗时、重试次数和成本摘要。

演示的重点不是每一个模型结果都完美，而是系统面对修改、失败和审核时仍然有清晰状态、可控预算和可追踪结果。

### 20.2 简历项目描述模板

以下文字仅适用于相关能力实际实现后使用，未完成的功能不应写为已完成。

**Arclight｜多智能体 AI 漫剧生产与工作流编排平台**

- 基于 Python、FastAPI、LangGraph、PostgreSQL、Redis、Celery 和 FFmpeg，构建面向 2D 竖屏动态漫剧的生产工作台，覆盖剧本改编、结构化分镜、角色/场景资产管理、图像与配音生成、时间轴组装及 MP4 导出。
- 设计 Screenwriter、Director、Art Director 和 Continuity Reviewer 等专业 Agent，通过结构化任务合同和状态图协调跨阶段生产流程；生成、媒体检查、渲染、状态迁移和恢复由确定性服务/Worker 承担，并将 LLM 判断与业务状态控制分离。
- 构建带版本管理的内容资产依赖图，追踪剧本、分镜、视觉参考、语音、字幕、时间轴和成片之间的依赖关系，实现变更影响分析、stale 标记与局部重建，减少无关素材重复生成。
- 实现异步媒体任务调度、有限重试、任务状态持久化、人工审核暂停/恢复、成本预算及失败追踪，支持单镜头失败后的局部恢复而非整条生产链重跑。
- 使用 FFmpeg 建立自动化合成与交付检查流程，并通过结构化日志、Trace、生成任务记录和回归评测追踪生成质量、任务耗时、重试率与单集成本。

### 20.3 有说服力的项目证据

建议在 README 中提供真实的、可复核的数据，而不是编造“效率提升 80%”之类的结果：

- 完整演示视频和成片样例。
- 单集生产耗时与每个镜头平均生成次数。
- 真实跑出的任务成功率、任务重试率、渲染成功率。
- 单集模型费用估算和不同生成配置的成本对比。
- 资产变更影响的测试案例。
- 端到端自动化测试结果与架构图。

---

## 21. 风险与技术决策

### 21.1 多智能体的调用成本与协调复杂度

多个 Agent 不一定比一个 Agent 更好。只在工作内容确实不同、输入输出可定义、可以单独验证或并行化时拆分 Agent。简单的 JSON 格式整理、状态更新、文件检查和成本计算应直接用确定性函数实现。

### 21.2 生成模型不稳定

模型的图像和视频输出具有随机性，不能假设重试会生成相同结果。系统应保留所有候选资产和提示词版本，让用户挑选结果；稳定性依赖流程控制与版本管理，而不是期待模型永远一次成功。

### 21.3 资产图复杂化

如果一开始对所有字段都建立细粒度依赖关系，开发成本会迅速升高。MVP 先实现明确的依赖类型和几个重要的失效规则，等出现真实变更场景再扩展。依赖图必须支持校验循环、批量查询和审计。

### 21.4 视频生成费用与时长

长视频模型调用可能耗时较长、费用较高且不同提供商能力差异明显。第一版用静态画面、轻量运动和音频完成动态漫剧闭环，把图生视频作为可选 Provider，不把它设为系统唯一生成路径。

### 21.5 数据一致性

工作流 checkpoint、数据库资产状态和后台任务状态是三套不同的状态。必须定义它们之间的权威关系：数据库保存业务事实；工作流 checkpoint 保存编排恢复信息；队列保存待执行工作。通过业务事件、幂等消费者和状态核对任务保持一致。

### 21.6 供应商锁定

使用统一 Provider Adapter 和内部规范化结果模型，但不要为了抽象而掩盖各供应商能力差异。Provider 能力应显式声明，并在运行时校验任务参数是否支持。

### 21.7 版权与内容授权

用户上传的故事、角色图片、音乐和字体可能涉及第三方权利。产品应要求用户确认拥有使用权；记录素材来源和授权说明；默认避免未经许可复用受保护角色、真人声音或商用素材。正式商业化前应补充内容审核、数据保留和版权政策。

### 21.8 关键架构决策记录（ADR）建议

建议为以下决策分别写 ADR：

- ADR-001：为什么采用模块化单体 + 独立 Worker。
- ADR-002：为什么使用 LangGraph 编排，而不是全部采用自由 Agent Loop。
- ADR-003：为什么媒体文件存对象存储，数据库只保存元数据。
- ADR-004：为什么采用不可变资产版本和 stale 传播。
- ADR-005：为什么以 FFmpeg 静态镜头合成为 MVP 交付路径。
- ADR-006：为什么将确定性 QA 与模型辅助 QA 分离。
- ADR-007：为什么采用 outbox 事件模式连接数据库事务与后台队列。

---

## 22. 官方参考资料

以下资料用于实现时核对具体 API 和工程行为。文档可能更新，开发时应以所锁定版本对应的官方文档为准。

1. **LangGraph — Interrupts / Human-in-the-loop**  
   https://docs.langchain.com/oss/python/langgraph/interrupts  
   用于设计需要等待用户审核、随后恢复执行的工作流。

2. **LangGraph — Persistence**  
   https://docs.langchain.com/oss/python/langgraph/persistence  
   用于理解 checkpoint、持久化工作流状态与恢复机制。

3. **Celery — Tasks and Retries**  
   https://docs.celeryq.dev/en/stable/userguide/tasks.html  
   用于设计后台任务、幂等处理、重试和 Worker 行为。

4. **Langfuse — Datasets**  
   https://langfuse.com/docs/evaluation/experiments/datasets  
   用于建立固定输入数据集和实验评测。

5. **Langfuse — Evaluation Overview**  
   https://langfuse.com/docs/evaluation/overview  
   用于了解离线评测、线上 Trace 评分和回归检查。

6. **FFmpeg Documentation**  
   https://ffmpeg.org/documentation.html  
   用于视频合成、字幕滤镜、音频混合、编解码及媒体处理。

7. **FastAPI Documentation**  
   https://fastapi.tiangolo.com/  
   用于 API 设计、依赖注入、请求校验及后台集成。

8. **Pydantic Documentation**  
   https://docs.pydantic.dev/latest/  
   用于结构化 Agent 输出、API Schema 和配置校验。

---

## 附录 A：MVP 最小交付清单

上线演示版本之前，至少逐项确认：

- [ ] 用户可以创建项目与剧集。
- [ ] 用户可以输入故事并生成结构化剧本。
- [ ] 用户可以编辑剧本并确认版本。
- [ ] 系统可以管理角色、场景和视觉风格资产。
- [ ] 系统可以生成并编辑分镜列表。
- [ ] 系统可以调用至少一个图像生成 Provider。
- [ ] 图片结果先注册为候选版本并通过文件完整性检查。
- [ ] 每张候选镜头图片都由 Continuity Reviewer 完成连续性审查，保存结构化问题、结论、风险等级和置信度。
- [ ] Continuity Reviewer 发现问题或置信度不足时，系统可以修订/重生成或暂停等待人工审核；未审查的候选图片不能进入正式选用状态。
- [ ] 生成任务具备状态、重试、错误记录和取消请求能力。
- [ ] 镜头素材可以关联角色、场景、提示词和模型版本。
- [ ] 系统可以生成配音或导入音频，并创建字幕。
- [ ] 用户可以创建/修改时间轴并提交渲染。
- [ ] FFmpeg 可以输出目标格式的 MP4。
- [ ] 成片具备机器可检查的验收报告。
- [ ] 修改上游资产后，系统可以将相关下游标记为 stale。
- [ ] 关键审核节点支持暂停与恢复。
- [ ] 数据库、队列和对象存储状态可在应用重启后恢复。
- [ ] 有基础单元测试、集成测试、端到端测试和 CI。
- [ ] README 包含架构图、配置方式、启动命令、演示流程和已知限制。
- [ ] `.env`、密钥、用户原始素材和真实生成凭据不会被提交到 Git。

## 附录 B：建议的第一条可运行垂直切片

不必在第一天实现所有扩展型 Agent，但核心生产链路必须包含 Continuity Reviewer。最适合验证架构的第一条垂直切片是：

1. 创建一个项目和剧集。
2. 输入一段短故事。
3. Screenwriter Agent 生成结构化剧本。
4. 用户确认剧本。
5. Director Agent 生成 5 个镜头。
6. 用户确认分镜。
7. GenerationService 为 5 个镜头生成图片候选版本。
8. 对每张候选图片运行确定性媒体检查，再由 Continuity Reviewer 检查角色、场景、道具和分镜意图的一致性。
9. 对 `REVISION_REQUIRED` 的结果执行一次受预算约束的局部修订/重生成；对 `HUMAN_REVIEW` 的结果暂停等待用户处理；未完成审查的图片不能进入正式选用状态。
10. 用户选择并确认通过审查的镜头素材。
11. 系统创建一个简单时间轴，给每个镜头分配默认时长。
12. 通过 FFmpeg 导出 MP4。
13. 打开任务中心查看从剧本、图片生成、连续性审查到成片的所有运行记录。

先让这条包含连续性审查的链路端到端可靠运行，再逐步增加更复杂的多集连续性分析、自动角色一致性评分、配音、复杂时间轴和成本优化。这样可以尽早发现数据模型、任务状态和资产依赖设计上的问题，而不是在大量功能完成后才发现需要重构。

---

**文档结束。**
