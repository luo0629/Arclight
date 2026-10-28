import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `analysis-${crypto.randomUUID()}@example.com`,
      password: "analysis-test-password",
      display_name: "故事提炼验收",
    },
  });
  expect(registration.status()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "故事提炼验收" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "雨夜拾信" },
    })
  ).json();
  const path = `/api/v1/episodes/${episode.id}`;
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openAnalysis(page);
  return { headers, project, episode, path };
}
async function openAnalysis(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "故事提炼", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "故事提炼", exact: true }),
  ).toBeVisible();
}
async function generate(
  page: Page,
  story = "雨夜，阿青在书店门口找到一封遗失的信。",
) {
  await page.getByLabel("待提炼故事", { exact: true }).fill(story);
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(page.getByRole("button", { name: /^提炼 v1/ })).toBeVisible();
}

test("manual extraction survives refresh and does not generate scripts or boards", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  const story =
    "  雨夜，阿青捡到信。\n<script>window.storyExecuted=true</script>";
  await generate(page, story);
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  const detail = page.getByRole("region", { name: "提炼结果详情" });
  await expect(
    detail.getByRole("heading", { name: "摘要", exact: true }),
  ).toBeVisible();
  await expect(detail.getByRole("heading", { name: "主要冲突" })).toBeVisible();
  await expect(detail.getByRole("heading", { name: "关键事件" })).toBeVisible();
  await expect(detail.getByRole("heading", { name: "角色表" })).toBeVisible();
  await expect(detail.getByText("未提取到明确角色。")).toBeVisible();
  await detail.getByText("查看原文快照与来源校验").click();
  expect(await detail.locator("pre").textContent()).toBe(story);
  expect(await page.evaluate(() => "storyExecuted" in window)).toBe(false);
  expect(
    await (await page.request.get(path + "/scripts", { headers })).json(),
  ).toEqual([]);
  expect(
    await (await page.request.get(path + "/storyboards", { headers })).json(),
  ).toEqual([]);
  await page.screenshot({
    path: "test-results/story-analysis-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "查看任务与审核" }).click();
  await expect(
    page.locator(".task-heading").filter({ hasText: "故事提炼" }),
  ).toBeVisible();
  await page.reload();
  await openAnalysis(page);
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  await expect(
    page
      .getByRole("region", { name: "提炼结果详情" })
      .getByText("演示提炼", { exact: false }),
  ).toBeVisible();
});

test("selects exact imported source and appends independent versions", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  const raw = "# 原稿\r\n阿青决定归还信件。";
  const source = await (
    await page.request.post(path + "/story-sources", {
      headers,
      data: {
        import_key: crypto.randomUUID(),
        original_name: "雨夜.md",
        media_type: "text/markdown",
        encoding: "utf-8",
        content_base64: Buffer.from(raw).toString("base64"),
        source_note: "本人原创",
        rights_note: "本人所有",
        rights_confirmed: true,
      },
    })
  ).json();
  await page.getByRole("radio", { name: "故事来源", exact: true }).click();
  await openAnalysis(page);
  await page.getByRole("combobox", { name: "故事输入方式" }).click();
  await page.getByRole("option", { name: "已导入来源", exact: true }).click();
  await page.getByRole("combobox", { name: "选择故事来源" }).click();
  await page.getByRole("option", { name: "#1 雨夜.md", exact: true }).click();
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  await page.getByText("查看原文快照与来源校验").click();
  expect(
    await page
      .getByRole("region", { name: "提炼结果详情" })
      .locator("pre")
      .textContent(),
  ).toBe(raw);
  const rows = await (
    await page.request.get(path + "/story-analyses", { headers })
  ).json();
  expect(rows[0].source_id).toBe(source.id);
  await page.getByRole("combobox", { name: "故事输入方式" }).click();
  await page.getByRole("option", { name: "手动输入", exact: true }).click();
  await page.getByLabel("待提炼故事", { exact: true }).fill("第二份独立文本");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(page.getByRole("button", { name: /^提炼 v2/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^提炼 v1/ })).toBeVisible();
});

test("lost submission receipt reuses its idempotency key and prevents duplicate clicks", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  let lose = true;
  const keys: string[] = [];
  await page.route("**/story-analysis-runs", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    if (lose) {
      lose = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page
    .getByLabel("待提炼故事", { exact: true })
    .fill("收到回执前断网的故事");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toBeVisible();
  await expect
    .poll(async () => {
      const runs = await (
        await page.request.get(path + "/runs", { headers })
      ).json();
      return runs[0]?.status;
    })
    .toBe("SUCCEEDED");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: /提炼任务已提交|提炼已完成/ })
      .first(),
  ).toBeVisible();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(path + "/story-analyses", { headers })
          ).json()
        ).length,
    )
    .toBe(1);
  await expect(page.getByRole("button", { name: /^提炼 v1/ })).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(
    await (await page.request.get(path + "/runs", { headers })).json(),
  ).toHaveLength(1);
});

test("directory refresh failure preserves accepted task and can be recovered", async ({
  page,
}) => {
  await setup(page);
  await expect(
    page.getByRole("region", { name: "提炼版本分页", exact: true }),
  ).toHaveAttribute("aria-busy", "false");
  let fail = true;
  await page.route("**/story-analyses?*", async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        json: { error: { message: "目录暂不可用" } },
      });
    else await route.continue();
  });
  await page
    .getByLabel("待提炼故事", { exact: true })
    .fill("保存之后目录刷新失败");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(
    page.getByText("故事提炼已完成，请从版本记录查看结果。"),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "提炼版本分页", exact: true }),
  ).toContainText("目录暂不可用");
  fail = false;
  await page
    .getByRole("region", { name: "提炼版本分页", exact: true })
    .getByRole("button", { name: "刷新提炼版本" })
    .click();
  await expect(page.getByRole("button", { name: /^提炼 v1/ })).toBeVisible();
});

test("late detail responses cannot replace a newer selected result", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await generate(page, "第一个故事");
  await page.getByLabel("待提炼故事", { exact: true }).fill("第二个故事");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(page.getByRole("button", { name: /^提炼 v2/ })).toBeVisible();
  const rows = await (
    await page.request.get(path + "/story-analyses", { headers })
  ).json();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(`**/story-analyses/${rows[1].id}`, async (route) => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  await expect(page.getByText("正在加载提炼结果…")).toBeVisible();
  await page.getByRole("button", { name: /^提炼 v2/ }).click();
  const detail = page.getByRole("region", { name: "提炼结果详情" });
  await expect(
    detail.getByText("演示提炼（Mock，不代表模型分析）：第二个故事", {
      exact: true,
    }),
  ).toBeVisible();
  release();
  await expect(
    detail.getByRole("heading", { name: "提炼 v2 · 手动输入" }),
  ).toBeVisible();
});

test("archived project retains readable extraction and disables new runs", async ({
  page,
}) => {
  const { project, headers } = await setup(page);
  await generate(page);
  expect(
    (
      await page.request.patch(`/api/v1/projects/${project.id}`, {
        headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openAnalysis(page);
  await expect(
    page.getByText("当前为只读模式，可查看历史提炼结果。"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "开始提炼", exact: true }),
  ).toBeDisabled();
  await expect(page.getByLabel("待提炼故事", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  await expect(
    page
      .getByRole("region", { name: "提炼结果详情" })
      .getByRole("heading", { name: "摘要", exact: true }),
  ).toBeVisible();
});

test("mobile extraction and character rendering remain within viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { path, headers } = await setup(page);
  await generate(page);
  const rows = await (
    await page.request.get(path + "/story-analyses", { headers })
  ).json();
  await page.route(`**/story-analyses/${rows[0].id}`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    detail.content.conflicts = ["阿青希望归还信件，信封上的地址却已消失。"];
    detail.content.characters = [
      {
        name: "阿青",
        aliases: ["小青"],
        role: "雨夜拾信者",
        motivation: "找到失主",
      },
    ];
    detail.content.key_events[0].character_names = ["阿青"];
    await route.fulfill({ response, json: detail });
  });
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  const detail = page.getByRole("region", { name: "提炼结果详情" });
  await expect(detail.getByText("身份：雨夜拾信者")).toBeVisible();
  await detail.getByText("查看原文快照与来源校验").click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/story-analysis-mobile.png",
    fullPage: true,
  });
});
