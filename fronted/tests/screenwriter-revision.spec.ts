import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page, source = false) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `revision-${crypto.randomUUID()}@example.test`,
      password: "revision-browser-password",
      display_name: "评价修订验收",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "编剧评价修订" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "纸船归途", target_duration_ms: 30000 },
    })
  ).json();
  let original;
  if (source) {
    const saved = await page.request.post(
      `/api/v1/episodes/${episode.id}/scripts`,
      {
        headers,
        data: {
          expected_version_no: 0,
          content: {
            title: "局部改写来源",
            summary: "寻找失主",
            estimated_duration_ms: 30000,
            characters: ["阿青"],
            scenes: [1, 2, 3].map((i) => ({
              scene_no: i,
              title: `场景${i}`,
              action: "前缀🙂改写目标后缀",
              dialogue: [{ speaker: "阿青", text: "我会把信送回去。" }],
            })),
          },
        },
      },
    );
    expect(saved.status()).toBe(201);
    original = await saved.json();
  }
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  return { episode, headers, original };
}

test("creates a critiqued candidate, shows patch feedback and uses both original reviews", async ({
  page,
}, testInfo) => {
  const env = await setup(page);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill("雨夜发现纸船，主角决定寻找失主。");
  await page
    .getByRole("radio", { name: "创作、评价与局部修订", exact: true })
    .click();
  await page.getByLabel("最多自动修订轮数", { exact: true }).fill("2");
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const work = page.getByRole("region", { name: "编剧计划与工作稿" });
  await work
    .locator("summary")
    .filter({ hasText: "评价与修订 1 · 已保存" })
    .click();
  await expect(
    work.getByText("补丁检查 1：通过", { exact: true }),
  ).toBeVisible();
  await expect(
    work.getByText("人物动机：寻找失主的动机需要说明", { exact: true }),
  ).toBeVisible();
  await work
    .locator("summary")
    .filter({ hasText: "评价与修订 2 · 已保存" })
    .click();
  await expect(
    work.getByText("评价与规则检查通过，交完整稿审核。", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("revision-and-patch.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  const rows = await (
    await page.request.get(`/api/v1/episodes/${env.episode.id}/scripts`, {
      headers: env.headers,
    })
  ).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].content_json.scenes[0].action).toContain("失主的嘱托");
  expect(rows[0].content_json.scenes[1].action).not.toContain("失主的嘱托");
});

test("retains exhausted issues and work across reload, then resumes the same run", async ({
  page,
}, testInfo) => {
  const env = await setup(page);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill("修订耗尽：纸船归途存在剧情分歧。");
  await page
    .getByRole("radio", { name: "创作、评价与局部修订", exact: true })
    .click();
  await page.getByLabel("最多自动修订轮数", { exact: true }).fill("1");
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  const work = page.getByRole("region", { name: "编剧计划与工作稿" });
  await expect(work.getByText(/编剧需要你的决定/)).toBeVisible({
    timeout: 20000,
  });
  expect(
    await (
      await page.request.get(`/api/v1/episodes/${env.episode.id}/scripts`, {
        headers: env.headers,
      })
    ).json(),
  ).toEqual([]);
  const runs = await (
    await page.request.get(`/api/v1/episodes/${env.episode.id}/runs`, {
      headers: env.headers,
    })
  ).json();
  const endpoint = `/api/v1/runs/${runs[0].id}/screenwriter`;
  const before = await (
    await page.request.get(endpoint, { headers: env.headers })
  ).json();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(work.getByText(/编剧需要你的决定/)).toBeVisible();
  await expect(
    work.getByText("剩余自动修订：0 轮", { exact: true }).last(),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("exhausted-question.png"),
    fullPage: true,
  });
  await work
    .getByLabel("创作问题回答", { exact: true })
    .fill("失主的嘱托已在前集说明，接受当前剧情。");
  await work
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const after = await (
    await page.request.get(endpoint, { headers: env.headers })
  ).json();
  expect(
    after.operations.map((item: { loop_id: string }) => item.loop_id),
  ).toEqual(before.operations.map((item: { loop_id: string }) => item.loop_id));
  expect(after.operations.at(-1).revision.remaining_patches).toBe(0);
});

test("starts professional Unicode paragraph rewrite at 390px and approves once without a director", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await setup(page, true);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "局部改写来源" })
    .click();
  await page
    .getByRole("button", { name: "局部改写此版本", exact: true })
    .click();
  await page.getByRole("combobox", { name: "改写范围", exact: true }).click();
  await page.getByRole("option", { name: "选定段落", exact: true }).click();
  const original = page.getByLabel("原文（可选中要改写的段落）", {
    exact: true,
  });
  await original.click();
  await original.evaluate((input: HTMLTextAreaElement) => {
    input.setSelectionRange(4, 8);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await expect(page.getByLabel("选定改写原文", { exact: true })).toHaveText(
    "改写目标",
  );
  await page
    .getByRole("radio", { name: "调查、规划与评价改写", exact: true })
    .click();
  await page.getByLabel("改写指令", { exact: true }).fill("补充行动动机");
  await expect(
    page.getByLabel("最多自动修订轮数", { exact: true }),
  ).toHaveValue("3");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("professional-rewrite-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "提交局部改写", exact: true }).click();
  await page
    .getByRole("button", { name: "前往任务与审核", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const rows = await (
    await page.request.get(`/api/v1/episodes/${env.episode.id}/scripts`, {
      headers: env.headers,
    })
  ).json();
  expect(rows).toHaveLength(2);
  expect(rows[0].content_json.scenes[0].action).toBe(
    "前缀🙂因为记得失主的嘱托🙂，他决定送信。后缀",
  );
  expect(rows[0].content_json.scenes.slice(1)).toEqual(
    env.original.content_json.scenes.slice(1),
  );
  expect(rows[1].content_json).toEqual(env.original.content_json);
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  expect(
    await (
      await page.request.get(`/api/v1/episodes/${env.episode.id}/storyboards`, {
        headers: env.headers,
      })
    ).json(),
  ).toEqual([]);
});
