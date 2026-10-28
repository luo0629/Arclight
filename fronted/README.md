# Arclight 前端

前端使用 Next.js 16 App Router、React 19、TypeScript、shadcn/ui（Radix）、Tailwind CSS v4 和 React Flow。UI 组件、视觉样式及业务交互沿用原实现。

## 开发与验证

在 `fronted/` 执行，Node.js 使用 24：

```powershell
npm ci
npm run dev
```

访问 `http://127.0.0.1:3000`。FastAPI 默认位于 `http://127.0.0.1:8000`，可用 `ARCLIGHT_API_PROXY` 更换地址；API 服务与 Worker 仍按仓库开发指南独立启动。

```powershell
npm run typecheck
npm run format:check
npm run build
npm run start
```

浏览器验收自动启动临时 SQLite + Mock API，不使用业务库或收费模型：

```powershell
# 开发模式
npm run test:e2e
# 正式构建模式（先运行 npm run build；CI 默认覆盖此模式）
$env:ARCLIGHT_E2E_PRODUCTION = "1"
npm run test:e2e
```

## 目录与职责

```text
src/
├── app/                         # App Router：页面、布局、加载、错误与路由处理
│   ├── layout.tsx               # HTML、元信息、全局样式
│   ├── (workbench)/
│   │   ├── layout.tsx           # 会话、草稿及共享工作台外壳
│   │   ├── page.tsx             # 首页与上次项目入口
│   │   └── projects/[projectId]/
│   │       ├── layout.tsx       # 项目目录、权限与共享项目外壳
│   │       ├── page.tsx         # 项目功能页
│   │       └── episodes/[episodeId]/page.tsx  # 独立剧集页面
│   ├── api/[...path]/route.ts    # 开发/直接运行 Next.js 时的同源 API 代理
│   └── health/[...path]/route.ts
├── features/                    # 按业务功能组织，组件归属本业务
│   ├── auth/                    # 会话边界、登录、账号菜单
│   ├── workbench/               # 共享项目目录、首页、工作台外壳与上下文
│   ├── projects/                # 项目布局、上下文、概览、设置、成员、回收站
│   ├── episodes/                # 剧集页面、URL 导航、编辑与复制
│   ├── canvas/components/       # React Flow 创作画布
│   ├── editor/components/       # 剧本/分镜编辑、版本对比、镜头、局部改写
│   ├── story/                   # 素材、修订、提炼、大纲、故事设定与类型
│   ├── assets/components/       # 视觉资产、资料、角色及版本
│   ├── tasks/components/        # 任务与审核
│   └── settings/components/     # 账号级模型服务配置
├── components/                  # 跨业务的展示组件
│   ├── ui/                      # shadcn/ui 基础组件
│   ├── shared/                  # 业务通用弹窗、分页
│   └── feedback/                # 应用加载反馈
├── hooks/                       # 跨业务 hooks，例如分页
├── providers/                   # 跨业务客户端上下文，例如本机草稿
├── lib/
│   ├── api/client.ts            # 仅浏览器使用的 HTTP、令牌刷新、错误处理
│   ├── server/backend-proxy.ts  # 仅服务端使用的流式后端代理
│   ├── routes.ts                # 路由构造、资源 ID 与查询参数白名单
│   ├── status.ts                # 通用状态文案
│   └── utils.ts                 # 通用工具
├── types/api.ts                 # 多业务共享的 API 数据合同
├── styles/                      # 全局主题与既有产品/工作室样式
└── proxy.ts                     # 文档 CSP nonce 与安全响应头
tests/                           # Playwright 功能、权限、异常、移动端回归
```

新增业务放进对应 `features/<feature>/`；业务专用类型与逻辑就近存放。`app` 负责路由和组合，`components` 存放跨业务展示组件，`lib`、`hooks`、`providers` 不依赖具体业务组件。跨目录使用 `@/` 别名，避免随着目录移动堆积相对路径。无需为每个目录添加重复导出的 `index.ts`。

## 渲染与数据边界

根布局、页面入口和嵌套布局采用 Next.js Server Components，登录、编辑器、草稿和 React Flow 使用 Client Components。页面按路由组合，没有使用 `ssr: false` 将整个业务应用挂载到单一入口。共享布局承载会话与草稿，切换项目、剧集和查询参数时不重新初始化登录。

| 地址 | 用途 |
| --- | --- |
| `/` | 产品首页、登录；登录后进入上次有权限访问的项目 |
| `/projects/[projectId]` | 项目入口，有剧集时进入首集，没有剧集时显示创建入口 |
| `/projects/[projectId]?section=overview` | 项目总览；还支持 `members`、`story`、`assets`、`settings` |
| `/projects/[projectId]/episodes/[episodeId]` | 指定剧集的创作画布 |
| `…/episodes/[episodeId]?view=list` | 版本列表；还支持 `library`、`tasks`、`sources`、`analysis`、`outline`、`create` |
| `…/episodes/[episodeId]?view=list&node=script%3A[versionId]` | 打开精确的历史剧本版本；分镜使用 `storyboard:[versionId]` |

项目、剧集导航使用 Next.js Router；工作区视图与节点使用与 Next.js `useSearchParams` 集成的 History API，避免切换视图时卸载编辑器。刷新、复制链接和浏览器前进后退以 URL 为准，localStorage 只保留首页的上次项目偏好。直接访问会校验剧集所属项目，目录首批之外的资源通过详情接口精确读取。未登录时保留原链接，登录成功后打开原项目、剧集和视图。

弹窗开关、画布缩放、专注模式与未保存的表单内容保持组件状态；草稿同步保存在原有本机草稿存储，不写入 URL。Next.js 路由切换时保留共享草稿上下文，文档刷新继续保留原有确认和恢复流程。

FastAPI 仍负责认证和业务数据。浏览器保持访问同源 `/api/v1`，令牌只保存在浏览器内存中。`lib/api/client.ts` 使用 `client-only`，服务端代理使用 `server-only`，防止导入跨越执行环境。代理保留 Host、Origin、多个 Set-Cookie 和二进制响应，支持原有会话刷新及资产读取。

## 部署

本机正式运行使用 `npm run build` 和 `npm run start`。Docker 构建设置 `ARCLIGHT_STANDALONE=1`，生成 Next.js standalone 服务，并复制 `_next/static`；Compose 中 `frontend` 提供 Node.js 服务，`web` 保留 Nginx 网关和 8080 端口。Nginx 继续处理 API/健康检查代理、请求大小和账户限流，将页面与 Next.js 静态资源转发至 `frontend:3000`。

Next.js 的 `proxy.ts` 为每个文档生成 CSP nonce，布局按请求渲染，让内联启动脚本在原有安全策略下运行；不通过全局放开内联脚本来绕过限制。Next.js 路由播报器也有 `alert` 角色，因此浏览器测试中的全局业务错误断言同时限定 `[data-slot="alert"]`。

## 导航回归

`tests/next-navigation.spec.ts` 专门验证资源直达、URL 优先级、视图刷新、前进后退、共享会话、历史版本定位、跨路由草稿、登录后返回原链接、无效及跨项目链接、目录首批之外的资源读取。原有功能、异常、权限和移动端测试继续保留。
