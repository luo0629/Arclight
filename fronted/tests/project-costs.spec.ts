import { test, expect, type Page } from "@playwright/test";
import { closeDirectory } from "./workspace-helpers";

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `costs-${crypto.randomUUID()}@example.com`,
      password: "costs-test-password",
      display_name: "成本验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "成本验收项目", budget_limit: "20" },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const added = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "成本剧集" } },
  );
  expect(added.status()).toBe(201);
  const episode = await added.json();
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  return {
    headers,
    project,
    episode,
    url: `/api/v1/projects/${project.id}/costs`,
  };
}
async function open(page: Page) {
  await page.goto("/");
  await closeDirectory(page);
  await page.getByRole("button", { name: "项目总览", exact: true }).click();
  await page.getByRole("button", { name: "查看成本明细", exact: true }).click();
  await expect(surface(page)).toHaveAttribute("aria-busy", "false");
}
const surface = (page: Page) =>
  page.getByLabel("项目成本数据", { exact: true });
async function media(
  page: Page,
  env: Awaited<ReturnType<typeof setup>>,
  assigned = true,
) {
  const response = await page.request.post(
    `/api/v1/projects/${env.project.id}/generation-jobs`,
    {
      headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
      data: {
        target_type: "SHOT_IMAGE",
        task_type: "IMAGE",
        episode_id: assigned ? env.episode.id : null,
        spec: {
          model: "mock-image-v1",
          model_config_version: "preset:7",
          prompt: "合成测试画面",
          prompt_version: "template:3",
          output_spec: { width: 960, height: 540, format: "png" },
        },
      },
    },
  );
  expect(response.status()).toBe(201);
  return response.json();
}

test("未报价文字调用显示费用未统计", async ({ page }) => {
  const env = await setup(page);
  const amounts = {
    entry_count: 1,
    confirmed_count: 0,
    estimated_count: 0,
    unpriced_count: 1,
    actual_cost: null,
    estimated_cost: null,
    projected_cost: null,
    pending_estimate: "0.000000",
    cost_is_estimate: true,
  };
  await page.route(`**${env.url}?*`, (route) =>
    route.fulfill({
      json: {
        summary: amounts,
        groups: [{ key: env.project.id, label: env.project.name, ...amounts }],
        total_groups: 1,
        generated_at: new Date().toISOString(),
      },
    }),
  );
  await page.route(`**${env.url}/entries?*`, (route) =>
    route.fulfill({
      json: {
        total: 1,
        items: [
          {
            id: crypto.randomUUID(),
            task_id: crypto.randomUUID(),
            source: "TEXT",
            task_type: "script",
            provider: "custom",
            model: "personal-model",
            attempt_no: 1,
            call_id: crypto.randomUUID(),
            ...amounts,
          },
        ],
      },
    }),
  );
  await open(page);
  await expect(
    surface(page).locator("dl").first().getByText("未统计", { exact: true }),
  ).toHaveCount(2);
  await expect(surface(page)).toContainText("1 条费用未统计");
  await surface(page).getByRole("button", { name: "查看该组明细" }).click();
  const details = page.getByLabel("费用明细", { exact: true });
  await expect(details.getByText("费用未统计", { exact: true })).toBeVisible();
  await expect(details).toContainText("报价：未统计 · 实际：未统计");
});

test("shows empty coverage, actual and estimated costs, four dimensions and exact trace references", async ({
  page,
}, info) => {
  const env = await setup(page);
  await open(page);
  await expect(
    surface(page).getByText("尚无费用记录", { exact: true }),
  ).toBeVisible();
  const job = await media(page, env);
  const unassigned = await media(page, env, false);
  const started = await page.request.post(
    `/api/v1/episodes/${env.episode.id}/script-runs`,
    {
      headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
      data: { story: "女孩走进雨夜的书店，找到一张地图。" },
    },
  );
  expect(started.status()).toBe(202);
  await expect(async () => {
    const result = await (
      await page.request.get(env.url, { headers: env.headers })
    ).json();
    expect(result.summary.confirmed_count).toBe(1);
    expect(result.summary.estimated_count).toBe(2);
  }).toPass();
  await surface(page)
    .getByRole("button", { name: "刷新成本", exact: true })
    .click();
  await expect(
    surface(page).getByText("已确认费用小计", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    surface(page).getByText("预计累计（估算）", { exact: true }).first(),
  ).toBeVisible();
  await surface(page).getByText("按剧集", { exact: true }).click();
  await expect(
    surface(page).getByText("第 1 集 · 成本剧集", { exact: true }),
  ).toBeVisible();
  await expect(
    surface(page).getByText("项目级任务（未指定剧集）", { exact: true }),
  ).toBeVisible();
  // The unassigned group must not leak another episode's records.
  await surface(page)
    .getByText("项目级任务（未指定剧集）", { exact: true })
    .locator("../../..")
    .getByRole("button", { name: "查看该组明细", exact: true })
    .click();
  const details = page.getByLabel("费用明细", { exact: true });
  await expect(details.getByText(unassigned.id, { exact: true })).toBeVisible();
  await expect(details.getByText(job.id, { exact: true })).toHaveCount(0);
  await surface(page).getByText("按模型", { exact: true }).click();
  await expect(
    surface(page).getByText("mock/mock-image-v1", { exact: true }),
  ).toBeVisible();
  await surface(page).getByText("按任务", { exact: true }).click();
  await expect(surface(page).getByText(job.id, { exact: true })).toBeVisible();
  await surface(page)
    .getByText(job.id, { exact: true })
    .locator("../../..")
    .getByRole("button", { name: "查看该组明细", exact: true })
    .click();
  await expect(
    details.getByText(job.input_manifest_id, { exact: true }),
  ).toBeVisible();
  await expect(
    details.getByText("估算 · 待核账", { exact: true }),
  ).toBeVisible();
  await details.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("costs-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await details.scrollIntoViewIfNeeded();
  await expect(details).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("costs-mobile.png"),
    fullPage: true,
  });
});

test("cost failure is visible and retry loads the real ledger", async ({
  page,
}) => {
  await setup(page);
  let failed = false;
  await page.route("**/costs?**", async (route) => {
    if (!failed) {
      failed = true;
      await route.fulfill({
        status: 503,
        json: { error: { code: "UNAVAILABLE", message: "成本暂不可用" } },
      });
    } else await route.continue();
  });
  await open(page);
  await expect(surface(page).getByRole("alert")).toContainText("成本暂不可用");
  await surface(page)
    .getByRole("button", { name: "刷新成本", exact: true })
    .click();
  await expect(
    surface(page).getByText("尚无费用记录", { exact: true }),
  ).toBeVisible();
  await expect(surface(page).getByRole("alert")).toHaveCount(0);
});

test("task pagination keeps project totals and dimensions switch without stale rows", async ({
  page,
}) => {
  const env = await setup(page);
  for (let index = 0; index < 11; index++) await media(page, env);
  await open(page);
  await surface(page).getByText("按任务", { exact: true }).click();
  await expect(
    surface(page).getByText("共 11 条 · 第 1–10 条", { exact: true }),
  ).toBeVisible();
  await expect(
    surface(page).getByText(
      "11 条待核账，0 条费用未统计。最终费用以服务商账单为准。",
      { exact: true },
    ),
  ).toBeVisible();
  await surface(page)
    .getByRole("button", { name: "下一页", exact: true })
    .click();
  await expect(
    surface(page).getByText("共 11 条 · 第 11–11 条", { exact: true }),
  ).toBeVisible();
  await expect(
    surface(page).getByText(
      "11 条待核账，0 条费用未统计。最终费用以服务商账单为准。",
      { exact: true },
    ),
  ).toBeVisible();
  await surface(page).getByText("按模型", { exact: true }).click();
  await expect(
    surface(page).getByText("共 1 条 · 第 1–1 条", { exact: true }),
  ).toBeVisible();
  await expect(
    surface(page).getByText("mock/mock-image-v1", { exact: true }),
  ).toBeVisible();
});
