import { test, expect, type Page } from "@playwright/test";

async function openSettings(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `${crypto.randomUUID()}@example.com`,
      password: "service-presets-test",
      display_name: "服务配置验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "我的故事" },
  });
  expect(project.status()).toBe(201);
  await page.goto("/");
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeVisible();
  return { dialog, headers };
}

test("预设自动填写、仅输入密钥即可启用，刷新保留且账号独立", async ({
  page,
  browser,
  baseURL,
}) => {
  const { dialog, headers } = await openSettings(page);
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  await dialog.getByRole("radio", { name: "DeepSeek", exact: true }).click();
  await expect(dialog.getByLabel("服务地址", { exact: true })).toHaveValue(
    "https://api.deepseek.com",
  );
  await expect(
    dialog.getByLabel("服务地址", { exact: true }),
  ).not.toBeEditable();
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue(
    "deepseek-flash",
  );
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("my-private-deepseek-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByText("已保存到当前账号。新建的文字任务将使用此服务。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  const fetched = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(await fetched.text()).not.toContain("my-private-deepseek-key");
  expect((await fetched.json()).services[0]).toMatchObject({
    supplier: "deepseek",
    mode: "custom",
    has_key: true,
  });
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  expect(
    await page.evaluate(() => JSON.stringify([localStorage, sessionStorage])),
  ).not.toContain("my-private-deepseek-key");
  await page.screenshot({
    path: "../artifacts/web/model-services-presets-desktop.png",
    fullPage: true,
    animations: "disabled",
  });
  const secondContext = await browser.newContext({ baseURL });
  try {
    const secondPage = await secondContext.newPage();
    const { dialog: secondDialog } = await openSettings(secondPage);
    await expect(
      secondDialog.getByRole("radio", { name: "服务器默认服务", exact: true }),
    ).toBeChecked();
    await secondDialog
      .getByRole("radio", { name: "DeepSeek", exact: true })
      .click();
    await expect(
      secondDialog.getByLabel("API Key", { exact: true }),
    ).toHaveAttribute("placeholder", "粘贴此服务商的 API Key");
    await expect(
      secondDialog.getByText("密钥已加密保存", { exact: true }),
    ).toHaveCount(0);
  } finally {
    await secondContext.close();
  }
  expect(crashes).toEqual([]);
});

test("切换供应商不复用旧密钥，自定义支持手填及兼容协议", async ({ page }) => {
  const { dialog } = await openSettings(page);
  await dialog.getByRole("radio", { name: "OpenAI", exact: true }).click();
  await dialog.getByLabel("API Key", { exact: true }).fill("openai-only-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByText("密钥已加密保存", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("radio", { name: "智谱 BigModel", exact: true })
    .click();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("请重新填写 API Key");
  await dialog.getByLabel("API Key", { exact: true }).fill("qwen-only-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await dialog
    .getByRole("radio", { name: "自定义服务商", exact: true })
    .click();
  await expect(dialog.getByLabel("服务地址", { exact: true })).toBeEditable();
  await dialog
    .getByLabel("服务地址", { exact: true })
    .fill("https://my-provider.example/v1");
  await dialog.getByLabel("模型名称", { exact: true }).fill("my-story-model");
  await dialog.getByLabel("API Key", { exact: true }).fill("custom-only-key");
  await dialog
    .getByRole("combobox", { name: "结构化输出格式", exact: true })
    .click();
  await page.getByRole("option", { name: "JSON 对象", exact: true }).click();
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "自定义服务商", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("服务地址", { exact: true })).toHaveValue(
    "https://my-provider.example/v1",
  );
  await expect(
    dialog.getByRole("combobox", { name: "结构化输出格式", exact: true }),
  ).toContainText("JSON 对象");
});

test("媒体配置单独保存，切分类保留草稿，手机无溢出且关闭保护可见", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { dialog } = await openSettings(page);
  await dialog
    .getByRole("radio", { name: "智谱 BigModel", exact: true })
    .click();
  await dialog.getByLabel("API Key", { exact: true }).fill("text-draft");
  await dialog.getByRole("radio", { name: "视频模型", exact: true }).click();
  await dialog
    .getByRole("radio", { name: "自定义服务商", exact: true })
    .click();
  await expect(
    dialog.getByText("仅保存预设 · 尚未接入生成", { exact: false }),
  ).toBeVisible();
  await dialog
    .getByLabel("服务地址", { exact: true })
    .fill("https://video-supplier.example/v1");
  await dialog.getByLabel("模型名称", { exact: true }).fill("my-video-model");
  await dialog.getByLabel("API Key", { exact: true }).fill("video-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("本次只保存预设");
  await dialog.getByRole("radio", { name: "文字模型", exact: true }).click();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue(
    "text-draft",
  );
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByRole("button", { name: "继续编辑配置" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑配置" }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await dialog.getByRole("radio", { name: "视频模型", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "视频模型", exact: true }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("radio", { name: "文字模型", exact: true }),
  ).not.toBeChecked();
  await page.screenshot({
    path: "../artifacts/web/model-services-presets-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
});
