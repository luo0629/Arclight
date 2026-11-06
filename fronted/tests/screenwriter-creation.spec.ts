import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const auth = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `writer-${crypto.randomUUID()}@example.test`,
      password: "professional-writer-test",
      display_name: "编剧验收",
    },
  });
  expect(auth.status()).toBe(201);
  const session = await auth.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: {
        name: "纸船分段创作",
        story_settings: {
          target_episode_count: 2,
          default_episode_duration_ms: 30000,
        },
      },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: {
        episode_no: 1,
        title: "纸船归途",
      },
    })
  ).json();
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  return { episode, headers };
}

test("starts segmented creation, views saved work, answers after refresh and uses both original reviews", async ({
  page,
}, testInfo) => {
  const { episode, headers } = await setup(page);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill("需要澄清：雨夜发现纸船，主角要寻找失主。");
  await page
    .getByRole("radio", { name: "调查、规划与分段创作", exact: true })
    .click();
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  const working = page.getByRole("region", { name: "编剧计划与工作稿" });
  await expect(
    working.getByText("纸船是否送回失主？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await working
    .locator("summary")
    .filter({ hasText: "故事计划 · 已保存" })
    .click();
  await expect(
    working.getByText("沿冻结故事规划纸船的归途", { exact: true }),
  ).toBeVisible();
  await working
    .locator("summary")
    .filter({ hasText: "场景 1 · 已保存" })
    .click();
  await expect(
    working.getByText("主角循着河岸的线索寻找失主。", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("saved-plan-question.png"),
    fullPage: true,
  });
  const runs = await (
    await page.request.get(`/api/v1/episodes/${episode.id}/runs`, { headers })
  ).json();
  expect(runs[0].workflow_version).toBe("screenwriter/1.0");
  const firstProgress = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/screenwriter`, {
      headers,
    })
  ).json();
  expect(firstProgress.operations).toHaveLength(3);
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    working.getByText("纸船是否送回失主？", { exact: true }),
  ).toBeVisible();
  await working.getByRole("radio", { name: "送回失主", exact: true }).click();
  await working
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  const scripts = await (
    await page.request.get(`/api/v1/episodes/${episode.id}/scripts`, {
      headers,
    })
  ).json();
  expect(scripts).toHaveLength(1);
  expect(scripts[0].content_json.scenes).toHaveLength(2);
  expect(scripts[0].status).toBe("APPROVED");
  const finalProgress = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/screenwriter`, {
      headers,
    })
  ).json();
  expect(
    finalProgress.operations.map((o: { loop_id: string }) => o.loop_id),
  ).toEqual(
    firstProgress.operations.map((o: { loop_id: string }) => o.loop_id),
  );
  await page.screenshot({
    path: testInfo.outputPath("completed-candidate.png"),
    fullPage: true,
  });
});

test("delivers professional season outline and derives a professional episode from the confirmed version", async ({
  page,
}) => {
  const { episode, headers } = await setup(page);
  await page.getByRole("radio", { name: "分集大纲", exact: true }).click();
  await page
    .getByLabel("待规划故事", { exact: true })
    .fill("雨夜，纸船沿河来到桥下，主角开始寻找失主。");
  await page
    .getByRole("radio", { name: "调查、规划与分段创作", exact: true })
    .click();
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v1/ })).toBeVisible({
    timeout: 20000,
  });
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByLabel("第2集结尾钩子", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "确认此大纲版本", exact: true })
    .click();
  await page
    .getByRole("button", { name: "从此大纲编写第1集剧本", exact: true })
    .click();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const runs = await (
    await page.request.get(`/api/v1/episodes/${episode.id}/runs`, { headers })
  ).json();
  expect(
    runs
      .map((run: { workflow_version: string }) => run.workflow_version)
      .sort(),
  ).toEqual(["screenwriter-outline/1.0", "screenwriter/1.0"].sort());
});
