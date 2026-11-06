import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page, manual = false) {
  const auth = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `director-${crypto.randomUUID()}@example.test`,
      password: "director-creation-test",
      display_name: "导演验收",
    },
  });
  expect(auth.status()).toBe(201);
  const session = await auth.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "纸船导演" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "归途" },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  if (manual) {
    const created = await page.request.post(prefix + "/scripts", {
      headers,
      data: {
        expected_version_no: 0,
        content: {
          schema_version: "2.0",
          title: "三个场景的纸船",
          summary: "寻找失主",
          characters: ["主角"],
          estimated_duration_ms: 30000,
          scenes: [1, 2, 3].map((number) => ({
            scene_id: crypto.randomUUID(),
            scene_no: number,
            title: `河岸${number}`,
            action:
              number === 2
                ? "导演澄清：主角走向河岸。"
                : "主角发现线索，决定送信。",
            dialogue: [
              {
                line_id: crypto.randomUUID(),
                speaker: "主角",
                text: "把信送回去。",
              },
            ],
          })),
        },
      },
    });
    expect(created.status()).toBe(201);
    const source = await created.json();
    expect(
      (
        await page.request.post(prefix + `/scripts/${source.id}/review`, {
          headers,
          data: { decision: "APPROVE" },
        })
      ).status(),
    ).toBe(200);
  }
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  return { episode, headers, prefix };
}

async function startStandalone(page: Page) {
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "三个场景的纸船" })
    .click();
  await page
    .getByRole("radio", { name: "调查、规划与逐场景拆镜", exact: true })
    .click();
  await page
    .getByRole("button", { name: "从此版本生成分镜", exact: true })
    .click();
}

test("standalone director preserves multi-scene work through refresh, answer and original review", async ({
  page,
}, testInfo) => {
  const { prefix, headers } = await setup(page, true);
  await startStandalone(page);
  const working = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await expect(
    working.getByText("这一场先观察还是先行动？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await working
    .locator("summary")
    .filter({ hasText: "场景调度与节奏计划 · 已保存" })
    .click();
  await expect(
    working.getByText("按场景目标规划纸船归途的镜头", { exact: true }),
  ).toBeVisible();
  await working
    .locator("summary")
    .filter({ hasText: "调查与工具反馈（3）" })
    .click();
  await expect(
    working.getByText("生产能力声明", { exact: true }),
  ).toBeVisible();
  await working
    .locator("summary")
    .filter({ hasText: "场景 1 · 已保存" })
    .click();
  await expect(
    working.getByText("场景内镜头 1", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("director-saved-shots.png"),
    fullPage: true,
  });
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  expect(runs[0].workflow_version).toBe("director/1.0");
  const before = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/director`, { headers })
  ).json();
  expect(before.operations).toHaveLength(3);
  expect(
    await (await page.request.get(prefix + "/storyboards", { headers })).json(),
  ).toHaveLength(0);
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    working.getByText("这一场先观察还是先行动？", { exact: true }),
  ).toBeVisible();
  await working.getByRole("radio", { name: "先观察", exact: true }).click();
  await working
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  const boards = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(boards).toHaveLength(1);
  expect(boards[0].status).toBe("APPROVED");
  expect(
    boards[0].content_json.shots.map(
      (shot: { shot_no: number }) => shot.shot_no,
    ),
  ).toEqual(Array.from({ length: 15 }, (_, i) => i + 1));
  expect(
    new Set(
      boards[0].content_json.shots.map(
        (shot: { scene_no: number }) => shot.scene_no,
      ),
    ),
  ).toEqual(new Set([1, 2, 3]));
  const after = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/director`, { headers })
  ).json();
  expect(
    after.operations.slice(0, 3).map((row: { loop_id: string }) => row.loop_id),
  ).toEqual(before.operations.map((row: { loop_id: string }) => row.loop_id));
});

test("combined professional creation selects director and keeps both human reviews", async ({
  page,
}) => {
  const { prefix, headers } = await setup(page);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill("雨夜主角发现纸船，沿河寻找失主。");
  await page
    .getByRole("radio", { name: "调查、规划与分段创作", exact: true })
    .click();
  await page
    .getByRole("radio", { name: "调查、规划与逐场景拆镜", exact: true })
    .click();
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const working = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await working
    .locator("summary")
    .filter({ hasText: "场景 2 · 已保存" })
    .click();
  await expect(
    working
      .locator("details")
      .filter({
        has: page.locator("summary", { hasText: "场景 2 · 已保存" }),
      })
      .getByText("场景内镜头 1", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  expect(runs[0].workflow_version).toBe("production/1.0");
  const writer = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/screenwriter`, {
      headers,
    })
  ).json();
  const director = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/director`, { headers })
  ).json();
  expect(writer.operations).toHaveLength(3);
  expect(director.operations).toHaveLength(3);
  expect(
    (
      await (
        await page.request.get(prefix + "/storyboards", { headers })
      ).json()
    )[0].content_json.shots,
  ).toHaveLength(10);
});

test("stopping an unanswered director question retains completed work and creates no candidate", async ({
  page,
}) => {
  const { prefix, headers } = await setup(page, true);
  await startStandalone(page);
  const working = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await expect(
    working.getByText("这一场先观察还是先行动？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await working
    .getByRole("button", { name: "停止本次创作", exact: true })
    .click();
  await expect(page.locator(".task .status")).toHaveText("已取消");
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    working.getByText("场景 1 · 已保存", { exact: true }),
  ).toBeVisible();
  await expect(
    working.getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toBeDisabled();
  expect(
    await (await page.request.get(prefix + "/storyboards", { headers })).json(),
  ).toHaveLength(0);
});
