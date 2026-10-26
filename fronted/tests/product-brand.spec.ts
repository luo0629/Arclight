import { expect, test, type Page } from "@playwright/test";

async function noOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
}

test("产品首页：响应式故事拼贴、真实入口与能力说明", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  for (const width of [1440, 1024, 901, 900, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: /让一个小灵感，\s*长成好故事。/ }),
    ).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await noOverflow(page);
    const start = page.getByRole("button", {
      name: "开始我的故事",
      exact: true,
    });
    await expect(start).toBeVisible();
    await start.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByLabel("怎么称呼你")).toBeVisible();
    await noOverflow(page);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(start).toBeFocused();
    await page.screenshot({
      path: `test-results/product-home-${width}.png`,
      fullPage: true,
    });
  }
  await page.getByRole("link", { name: "让故事发生" }).click();
  await expect(page).toHaveURL(/#how-it-works$/);
  await expect(
    page.getByText("现在，你可以创作剧本与文字分镜。"),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("产品登录：错误提示、忙碌保护、登录注册切换与焦点恢复", async ({
  page,
}) => {
  await page.goto("/");
  const login = page.getByRole("button", { name: "登录", exact: true });
  await login.click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", { name: "欢迎回来，继续你的故事。" }),
  ).toBeVisible();
  await page.getByLabel("邮箱", { exact: true }).fill("reader@example.com");
  await page.getByLabel("密码", { exact: true }).fill("not-the-password");
  let finishRequest: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    finishRequest = resolve;
  });
  await page.route("**/api/v1/auth/login", async (route) => {
    await pending;
    await route.fulfill({ status: 401, json: { detail: "邮箱或密码不正确" } });
  });
  await page.getByRole("button", { name: "登录并继续创作" }).click();
  await expect(
    page.getByRole("button", { name: "正在打开你的故事…" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "关闭登录窗口" }),
  ).toBeDisabled();
  await expect(dialog.locator("[data-slot=spinner]")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  finishRequest!();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "第一次来？创建账号" }).click();
  await expect(page.getByLabel("怎么称呼你")).toBeVisible();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await expect(page.getByLabel("密码", { exact: true })).toHaveAttribute(
    "autocomplete",
    "new-password",
  );
  await page.getByRole("button", { name: "已有账号？去登录" }).click();
  await expect(page.getByLabel("密码", { exact: true })).toHaveAttribute(
    "autocomplete",
    "current-password",
  );
  await page.getByRole("button", { name: "关闭登录窗口" }).click();
  await expect(login).toBeFocused();
});

test("统一视觉：创作、版本、任务、伙伴、设置和服务页面", async ({ page }) => {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `${crypto.randomUUID()}@example.com`,
      password: "product-visual-test",
      display_name: "月亮收信人",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "月亮邮局" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: {
        episode_no: 1,
        title: "一封来自远方的信",
        target_duration_ms: 30000,
      },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await expect(page.locator(".canvas-wrap")).toBeVisible();
  await page.screenshot({
    path: "test-results/product-canvas.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await expect(page.getByLabel("故事灵感", { exact: true })).toBeVisible();
  await expect(page.locator(".inspector-drawing")).toBeVisible();
  await page.screenshot({
    path: "test-results/product-create.png",
    fullPage: true,
  });
  for (const [name, screenshot] of [
    ["版本列表", "versions"],
    ["任务与审核", "reviews"],
  ]) {
    await page.getByRole("radio", { name, exact: true }).click();
    await noOverflow(page);
    await page.screenshot({
      path: `test-results/product-${screenshot}.png`,
      fullPage: true,
    });
  }
  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "给故事找几个好伙伴" }),
  ).toBeVisible();
  await expect(page.locator(".panel-drawing")).toBeVisible();
  await page.screenshot({
    path: "test-results/product-members.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "保存设置", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/product-settings.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page);
  await page.screenshot({
    path: "test-results/product-settings-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(page.getByRole("heading", { name: "模型与服务" })).toBeVisible();
  await noOverflow(page);
  await page.screenshot({
    path: "test-results/product-services-mobile.png",
    fullPage: true,
  });
});

test("现代组件：项目键盘选择、账号菜单、提示与小屏弹层边界", async ({
  page,
}) => {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `${crypto.randomUUID()}@example.com`,
      password: "modern-ui-test-password",
      display_name: "森林来信",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  for (const name of ["森林里的第一封信", "月亮与星星的第二个故事"]) {
    const created = await page.request.post("/api/v1/projects", {
      headers,
      data: { name },
    });
    expect(created.status()).toBe(201);
  }
  await page.goto("/");
  const picker = page.getByRole("combobox", { name: "选择项目", exact: true });
  await expect(picker).toContainText("月亮与星星的第二个故事");
  await picker.focus();
  await page.keyboard.press("Space");
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(
    page.getByRole("option", { name: "月亮与星星的第二个故事", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("option", { name: "森林里的第一封信", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(picker).toContainText("森林里的第一封信");
  await expect(picker).toBeFocused();
  const account = page.getByRole("button", { name: "账号菜单", exact: true });
  await account.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menu")).toBeVisible();
  await expect(page.getByRole("menu")).toContainText(session.user.email);
  await page.keyboard.press("Escape");
  await expect(account).toBeFocused();
  await page.getByRole("button", { name: "刷新项目", exact: true }).focus();
  await expect(page.getByRole("tooltip")).toContainText("重新获取项目列表");
  await page.keyboard.press("Escape");
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await picker.click();
    const popup = page.getByRole("listbox");
    await expect(popup).toBeVisible();
    const box = await popup.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    await noOverflow(page);
    await page.keyboard.press("Escape");
    await account.click();
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    const menuBox = await menu.boundingBox();
    expect(menuBox!.x).toBeGreaterThanOrEqual(0);
    expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: `test-results/product-account-${width}.png`,
    });
    await page.keyboard.press("Escape");
  }
});
