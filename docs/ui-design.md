# 创作工作台界面约定

## 设计方向

- 方向：可爱但可工作的「贴纸创作本」，不是营销落地页。
- 调色：暖纸底、莓果 #a63267、淡紫、薄荷、墨梅和柔和描边；统一通过语义主题变量管理。
- 字体：Geist Variable 随构建本地分发，中文回退到微软雅黑等系统字体，不请求远程字体。
- 布局：左侧项目剧集导航；中间以节点画布串联故事、剧本与分镜；右侧阅读、编辑和审核。
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
- “创作画布／版本列表／任务与审核”使用单选 ToggleGroup；其可访问角色是 radiogroup / radio，键盘和测试使用真实语义。
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
