import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect } from "@playwright/test";

test("组件化：登录使用 Card/Field/Input/Button，390px 无溢出且品牌主题生效", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await openWorkspacePanels(page);
  const login = page.locator(".login-card");
  await expect(
    login.getByRole("heading", { name: "欢迎回来，小创作家" }),
  ).toBeVisible();
  await expect(login).toHaveAttribute("data-slot", "card");
  await expect(login.locator('[data-slot="field"]')).toHaveCount(2);
  await expect(login.locator('[data-slot="input"]')).toHaveCount(2);
  const submit = login.getByRole("button", { name: "登录创作手帐" });
  await expect(submit).toHaveAttribute("data-slot", "button");
  await expect(submit).toHaveCSS("background-color", "rgb(8, 127, 131)");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/components-login-mobile.png",
    fullPage: true,
  });
});

test("组件化：视图单选支持键盘，Dialog 返回焦点，成员与设置复用表单组件", async ({
  page,
}) => {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "components-ui-test",
      display_name: "组件验收",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "组件化工作台" },
    })
  ).json();
  expect(
    (
      await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
        headers,
        data: { episode_no: 1, title: "组件验收集" },
      })
    ).status(),
  ).toBe(201);
  await page.goto("/");
  await openWorkspacePanels(page);
  const canvas = page.getByRole("radio", { name: "创作画布", exact: true });
  const list = page.getByRole("radio", { name: "版本列表", exact: true });
  await canvas.focus();
  await page.keyboard.press("ArrowRight");
  await expect(list).toBeFocused();
  await page.keyboard.press("Space");
  await expect(list).toBeChecked();
  await expect(page.locator(".version-list")).toBeVisible();
  const edit = page.getByRole("button", { name: "编辑剧集", exact: true });
  await edit.click();
  await expect(page.getByRole("dialog")).toHaveAttribute(
    "data-slot",
    "dialog-content",
  );
  await page.keyboard.press("Escape");
  await expect(edit).toBeFocused();
  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "给故事找几个好伙伴" }),
  ).toBeVisible();
  await expect(page.locator(".page-panel")).toHaveAttribute(
    "data-slot",
    "card",
  );
  await expect(page.getByLabel("伙伴的邮箱")).toHaveAttribute(
    "data-slot",
    "input",
  );
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("项目名称", { exact: true })).toHaveAttribute(
    "data-slot",
    "input",
  );
  await expect(page.getByLabel("视觉风格说明")).toHaveAttribute(
    "data-slot",
    "textarea",
  );
  await page.screenshot({
    path: "test-results/components-settings-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});
