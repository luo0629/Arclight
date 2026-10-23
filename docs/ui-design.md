# 创作工作台界面约定

## 设计方向

- 方向：现代、清新、轻快的「清风创作室」，不是营销落地页。
- 调色：冷白底 #f5f9fa、湖青 #087f83、浅湖蓝、薄荷绿与少量柠檬黄；不使用粉色；统一通过语义主题变量管理。
- 字体：Geist Variable 随构建本地分发，中文回退到微软雅黑等系统字体，不请求远程字体。
- 布局：显式项目功能导航，项目／剧集目录可收起；桌面默认全宽依赖画布，内容面板默认关闭，选中节点后按需展开浮层。独立起稿区、版本列表和任务审核视图，不将表单堆在画布旁。
- 移动端默认列表模式，画布不是访问业务对象的唯一入口；390px 页面不得横向溢出。
- 节点坐标、视角保存到后端；依赖来自真实业务版本，不允许连线伪造已批准关系。
- 错误、空态、载入和权限状态明确展示，禁止用假数据冒充后端结果。

## 组件与样式

前端统一使用 **shadcn/ui（Radix）+ Tailwind CSS v4**。不同时引入第二套组件系统，也不在业务页面重新实现相同控件。

- `src/components/ui/`：本地维护的 shadcn 基础组件，包括 Button、Card、Field、Input、Textarea、Select、Dialog、ToggleGroup、Badge、Alert、Empty、Skeleton 等。
- `src/components/ui-kit.tsx`：业务共享的 PagePanel、FormLabel、Notice、StatusBadge、EmptyState、LoadingState，集中处理标题结构、表单标签和状态语义。
- `src/lib/utils.ts`：统一导出 `cn`，处理条件类与 Tailwind 类冲突。
- `src/style.css`：仅保留 Tailwind 导入、主题 token、基础样式、React Flow 第三方适配和 reduced-motion。布局与响应式使用组件上的 Tailwind 工具类，不恢复页面级大样式表。
- `src/components/ui/shadcn-variants.css`：维护基础组件使用的 Radix 状态变体。无需将 shadcn CLI 作为运行时或长期依赖。
- 业务组件不自行定义一套按钮、提示框、徽章或加载动画；优先使用已有基础组件和业务封装，避免为抽象而抽象。

需要新增基础组件时，在 `fronted/` 执行 `npx shadcn@latest add <component>`，检查生成文件与本地主题、状态变体的兼容性，并只保留实际使用的组件和依赖。

## 页面职责

- `App.tsx`：恢复会话、登录与退出。
- `Workbench.tsx`：项目列表和创建。
- `ProjectWorkspace.tsx`：项目内剧集目录、页面切换及设置入口。
- `EpisodeWorkspace.tsx`：剧集版本、任务、选中状态和人工保存回执。
- `Canvas.tsx`：真实依赖图、布局 CAS 与新保存节点定位，按需懒加载。
- 编辑、审核、成员和配置组件各自维护其业务表单，不把所有状态重新堆回 App。

## 组合与可访问性

- 表单使用 FieldGroup / Field 和关联标签；共享 FormLabel 包装 Field / FieldLabel。下拉选项放在 SelectGroup 内。
- 弹窗使用同一套 Dialog，必须提供标题；复用 Radix 的焦点限制、Esc 和关闭后的焦点返回，不手写焦点循环。
- “创作画布／版本列表／任务与审核”使用单选 ToggleGroup；“开始创作”为独立起稿入口；返回工作区始终有明确按钮。视图选择的可访问角色是 radiogroup / radio，键盘和测试使用真实语义。
- 关键操作保留可见文字、focus-visible 与禁用状态。装饰不替代文字标签，尊重 prefers-reduced-motion。
- 人工新稿保存后精确打开 POST 返回的版本并聚焦属性区；画布只在真实节点出现时定位一次，不随轮询反复跳转。

## 验证与边界

在 `fronted/` 执行：

```powershell
npm.cmd run format:check
npm.cmd run build
npm.cmd run test:e2e
npm.cmd audit
```

浏览器测试覆盖实际组件、键盘切换、弹窗焦点、桌面／390px、权限与业务链路。截图和 trace 仅为本地忽略产物。

本次重构不改变后端权限、不可变内容、审核、分页或画布 CAS 协议。媒体生成、连续性审查、时间轴、渲染和预算等仍按技术设计继续实施，不能以文本工作台或组件化完成替代总目标。

## 模型服务配置

- 顶栏进入 shadcn/Radix 配置弹窗：文字、图片、视频、语音分区。各区未保存表单切换保留，关闭前确认放弃；错误时保留输入。
- 配置按登录账号隔离，GET 只提供 has_key，不回传密钥或密文；前端不写 localStorage/sessionStorage。服务端用独立 Fernet 密钥加密，未配置加密时禁用密钥输入，仍可保存无密钥预设。
- 文字服务必须显式启用，显示费用与素材外发提示。地址须为 HTTPS 且启用域名必须在管理员允许列表中。新任务保存启动时配置的加密快照，等待审核／重试不改用后来编辑的配置，快照不进入模型提示词或 checkpoint。
- 图片、视频、语音仅保存预设，不冒充已支持生成；无自动测试连接，不在保存时调用供应商。真实 Provider 联调继续待验证。
- 模型配置使用 revision 乐观锁，陈旧保存返回冲突而不是覆盖。
