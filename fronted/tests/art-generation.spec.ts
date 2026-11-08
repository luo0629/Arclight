import { expect, test, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function open(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page.getByText("美术提案与审核", { exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "生成美术提案", exact: true }),
  ).toBeVisible();
}

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `art-run-${crypto.randomUUID()}@example.test`,
      password: "art-generation-browser",
      display_name: "美术生成验收",
    },
  });
  expect(registered.status()).toBe(201);
  const auth = await registered.json();
  const headers = { Authorization: `Bearer ${auth.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "纸船美术方向" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "雨夜纸船" },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const created = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        schema_version: "2.0",
        title: "雨夜送信",
        summary: "主角沿河送信",
        characters: ["主角"],
        estimated_duration_ms: 30000,
        scenes: [
          {
            scene_id: crypto.randomUUID(),
            scene_no: 1,
            title: "河岸",
            action: "主角拾起纸船，沿河寻找失主。",
            dialogue: [
              {
                line_id: crypto.randomUUID(),
                speaker: "主角",
                text: "把信送回去。",
              },
            ],
          },
        ],
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const script = await created.json();
  expect(
    (
      await page.request.post(prefix + `/scripts/${script.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await open(page);
  return { headers, project, episode, script, prefix };
}

async function selectEpisode(page: Page) {
  await page
    .getByRole("combobox", { name: "美术任务剧集", exact: true })
    .click();
  await page
    .getByRole("option", { name: "第 1 集 · 雨夜纸船", exact: true })
    .click();
}

async function fill(page: Page, goal: string, minimum = 0) {
  await selectEpisode(page);
  await page
    .getByRole("combobox", { name: "美术输入剧本", exact: true })
    .click();
  await page
    .getByRole("option", { name: "v1 · 雨夜送信", exact: true })
    .click();
  await page.getByLabel("生成视觉目标", { exact: true }).fill(goal);
  await page
    .getByLabel("视觉约束（每行一项）", { exact: true })
    .fill("保留朴素服装\n只提交方案");
  await page.getByLabel("最少参考图片", { exact: true }).fill(String(minimum));
}

async function concepts(page: Page, goal: string) {
  await fill(page, goal, 2);
  await page.getByLabel("生成概念图并比较修订", { exact: true }).check();
  await page.getByLabel("概念图宽度（像素）", { exact: true }).fill("8");
  await page.getByLabel("概念图高度（像素）", { exact: true }).fill("12");
}

const current = (page: Page) =>
  page.getByLabel("当前美术生成任务", { exact: true });
const progress = (page: Page) =>
  current(page).getByRole("region", {
    name: "美术调查与方案进度",
    exact: true,
  });

test("账号图片配置用于概念图，原生数量限制与模型保持一致", async ({ page }) => {
  const { project } = await setup(page);
  await fill(page, "查看账号原生图片配置", 1);
  await page.getByLabel("生成概念图并比较修订", { exact: true }).check();
  let configurationReads = 0;
  await page.route(
    `**/api/v1/projects/${project.id}/generation-jobs/services/image/configuration`,
    async (route) => {
      configurationReads += 1;
      await route.fulfill({
        json: {
          provider: "cogview",
          enabled: true,
          model: "selected-cogview-id",
          configuration_version: "account-media-config:1",
          capabilities: { max_batch_size: 1 },
        },
      });
    },
  );
  await page.getByRole("combobox", { name: "概念图服务", exact: true }).click();
  await page
    .getByRole("option", { name: "我的图片服务（或服务器默认）", exact: true })
    .click();
  await expect(page.getByLabel("概念图模型", { exact: true })).toHaveValue(
    "selected-cogview-id",
  );
  await expect(page.getByLabel("概念图模型", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("每轮候选数量", { exact: true })).toHaveValue(
    "1",
  );
  await expect(
    page.getByLabel("每轮候选数量", { exact: true }),
  ).toHaveAttribute("max", "1");
  expect(configurationReads).toBe(1);
});

for (const verdict of ["approve", "reject"] as const) {
  test(`ART-05: clarification survives refresh and ${verdict} resumes the exact saved art run`, async ({
    page,
  }, info) => {
    const { headers, project, script, prefix } = await setup(page);
    await fill(page, "澄清雨夜角色的冷暖视觉方向");
    await page
      .getByRole("button", { name: "开始美术生成", exact: true })
      .click();
    await expect(
      progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
    ).toBeVisible({ timeout: 20000 });
    const before = await (
      await page.request.get(prefix + "/runs", { headers })
    ).json();
    expect(before).toHaveLength(1);
    const runId = before[0].id;
    const saved = await (
      await page.request.get(`/api/v1/runs/${runId}/art`, { headers })
    ).json();
    expect(saved.delivery).toBeNull();
    await page.reload();
    await open(page);
    await expect(
      progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
    ).toBeVisible();
    await progress(page)
      .getByRole("radio", { name: "冷色", exact: true })
      .click();
    await progress(page)
      .getByRole("button", { name: "提交回答并继续", exact: true })
      .click();
    await expect(
      progress(page).getByRole("button", { name: "查看生成提案", exact: true }),
    ).toBeVisible({ timeout: 20000 });
    const completed = await (
      await page.request.get(`/api/v1/runs/${runId}/art`, { headers })
    ).json();
    expect(
      completed.operations.map((row: { loop_id: string }) => row.loop_id),
    ).toEqual(saved.operations.map((row: { loop_id: string }) => row.loop_id));
    await progress(page)
      .locator("summary")
      .filter({ hasText: "查看方案比较依据" })
      .click();
    await expect(
      progress(page).getByText("优势：以颜色和轮廓区分叙事情绪").first(),
    ).toBeVisible();
    await progress(page)
      .getByRole("button", { name: "查看生成提案", exact: true })
      .click();
    const detail = page.getByLabel("美术提案详情", { exact: true });
    await expect(
      detail.getByRole("heading", {
        name: "澄清雨夜角色的冷暖视觉方向",
        exact: true,
      }),
    ).toBeVisible();
    const row = await (
      await page.request.get(
        `/api/v1/projects/${project.id}/art-proposals/${completed.delivery.proposal_id}`,
        { headers },
      )
    ).json();
    expect(row.version.id).toBe(completed.delivery.version_id);
    expect(row.review.snapshot.thread_id).toBe(runId);
    expect(
      row.version.proposal_json.sources.some(
        (source: { resource_id: string }) => source.resource_id === script.id,
      ),
    ).toBe(true);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: info.outputPath(`art-${verdict}-mobile.png`),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if (verdict === "approve") {
      const button = detail.getByRole("button", {
        name: "批准并生成正式版本",
        exact: true,
      });
      await expect(button).toBeDisabled();
      for (const checkbox of await detail.getByRole("checkbox").all())
        await checkbox.check();
      await button.click();
      await expect(detail.getByText("已批准", { exact: true })).toBeVisible();
    } else {
      await detail
        .getByLabel("美术审核意见", { exact: true })
        .fill("保留目前方向");
      await detail
        .getByRole("button", { name: "拒绝美术提案", exact: true })
        .click();
      await expect(detail.getByText("已拒绝", { exact: true })).toBeVisible();
    }
    await expect
      .poll(
        async () =>
          (
            await (
              await page.request.get(`/api/v1/runs/${runId}`, { headers })
            ).json()
          ).status,
      )
      .toBe(verdict === "approve" ? "SUCCEEDED" : "REJECTED");
    await page.reload();
    await open(page);
    const after = await (
      await page.request.get(`/api/v1/runs/${runId}/art`, { headers })
    ).json();
    expect(after.delivery).toEqual(completed.delivery);
    expect(after.operations).toHaveLength(1);
    const assets = await (
      await page.request.get(`/api/v1/projects/${project.id}/assets`, {
        headers,
      })
    ).json();
    expect(assets).toHaveLength(verdict === "approve" ? 1 : 0);
    expect(
      await (
        await page.request.get(prefix + "/storyboards", { headers })
      ).json(),
    ).toEqual([]);
  });
}

test("ART-05: lost 202 response restores the original request and returns one run", async ({
  page,
}) => {
  const { prefix, headers } = await setup(page);
  await fill(page, "澄清丢失回执的视觉目标");
  let dropped = false;
  const keys: string[] = [];
  await page.route(new RegExp(prefix + "/art-runs$"), async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    if (!dropped) {
      dropped = true;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    page.getByLabel("生成美术提案", { exact: true }).getByRole("alert"),
  ).toBeVisible();
  await page.reload();
  await open(page);
  await selectEpisode(page);
  await expect(
    page.getByText("已恢复尚未取得运行回执的请求，可用原请求重试。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByLabel("生成视觉目标", { exact: true })).toHaveValue(
    "澄清丢失回执的视觉目标",
  );
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (await page.request.get(prefix + "/runs", { headers })).json(),
  ).toHaveLength(1);
});

test("ART-05: model failure and stopping a question create no formal candidate", async ({
  page,
}) => {
  const { headers, project, prefix } = await setup(page);
  await fill(page, "失败的视觉任务");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect
    .poll(
      async () =>
        (
          await (await page.request.get(prefix + "/runs", { headers })).json()
        )[0]?.status,
    )
    .toBe("FAILED");
  await page.reload();
  await open(page);
  await expect(progress(page).getByRole("alert").first()).toBeVisible();
  await expect(
    progress(page).getByRole("button", { name: "恢复美术任务", exact: true }),
  ).toHaveCount(0);
  await fill(page, "需要参考素材的视觉任务", 2);
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  await progress(page)
    .getByRole("button", { name: "停止本次创作", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (
          await (await page.request.get(prefix + "/runs", { headers })).json()
        )[0]?.status,
    )
    .toBe("CANCELLED");
  await page.reload();
  await open(page);
  await expect(
    progress(page).getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toBeDisabled();
  expect(
    await (
      await page.request.get(`/api/v1/projects/${project.id}/art-proposals`, {
        headers,
      })
    ).json(),
  ).toEqual([]);
  expect(
    await (
      await page.request.get(`/api/v1/projects/${project.id}/assets`, {
        headers,
      })
    ).json(),
  ).toEqual([]);
});

test("ART-05: viewer reads saved art history without generation, answers or approval", async ({
  page,
}) => {
  const { headers, project, prefix } = await setup(page);
  await fill(page, "澄清只读成员可查看的任务");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const viewer = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `viewer-${crypto.randomUUID()}@example.test`,
      password: "art-generation-browser",
      display_name: "只读美术成员",
    },
  });
  expect(viewer.status()).toBe(201);
  const user = await viewer.json();
  expect(
    (
      await page.request.post(`/api/v1/projects/${project.id}/members`, {
        headers,
        data: { email: user.user.email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  await page.reload();
  await open(page);
  await selectEpisode(page);
  await page
    .getByLabel("生成美术提案", { exact: true })
    .getByRole("button", { name: /创作中/ })
    .click();
  await expect(
    progress(page).getByText("美术方案采用冷色还是暖色？", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "开始美术生成", exact: true }),
  ).toHaveCount(0);
  await expect(
    progress(page).getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "新建美术提案", exact: true }),
  ).toHaveCount(0);
  const denied = await page.request.post(prefix + "/art-runs", {
    headers: {
      Authorization: `Bearer ${user.access_token}`,
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: {
      script_version_id: crypto.randomUUID(),
      task: { visual_goal: "越权请求" },
    },
  });
  expect(denied.status()).toBe(403);
});

test("AART-03: two actual concept rounds survive refresh and exact images require owner approval", async ({
  page,
}, info) => {
  const { headers, project, prefix } = await setup(page);
  await concepts(page, "概念图比较雨夜轮廓并修订");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByRole("button", { name: "查看生成提案", exact: true }),
  ).toBeVisible({ timeout: 30000 });
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  expect(runs).toHaveLength(1);
  expect(runs[0].workflow_version).toBe("art-proposal/2.0");
  const saved = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/art`, { headers })
  ).json();
  expect(saved.concept_rounds).toHaveLength(2);
  expect(
    saved.concept_rounds.every(
      (round: { candidates: unknown[] }) => round.candidates.length === 2,
    ),
  ).toBe(true);
  await expect(
    progress(page).getByRole("img", { name: "精确资产版本原图" }),
  ).toHaveCount(4);
  await expect
    .poll(async () =>
      progress(page)
        .getByRole("img")
        .evaluateAll((images) =>
          images.every(
            (image) => (image as HTMLImageElement).naturalWidth === 8,
          ),
        ),
    )
    .toBe(true);
  await page.reload();
  await open(page);
  await expect(
    progress(page).getByText("第 2 轮概念图", { exact: true }),
  ).toBeVisible();
  const after = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/art`, { headers })
  ).json();
  expect(after.operations[0].loop_id).toBe(saved.operations[0].loop_id);
  expect(after.delivery).toEqual(saved.delivery);
  await progress(page)
    .getByRole("button", { name: "查看生成提案", exact: true })
    .click();
  const detail = page.getByLabel("美术提案详情", { exact: true });
  await expect(
    detail.getByRole("img", { name: "精确资产版本原图" }),
  ).toHaveCount(4);
  await expect(
    detail.getByRole("checkbox", { name: /批准精确候选并确认使用授权/ }),
  ).toHaveCount(4);
  const approve = detail.getByRole("button", {
    name: "批准并生成正式版本",
    exact: true,
  });
  await expect(approve).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("concept-rounds-mobile.png"),
    fullPage: true,
  });
  const exactReference = detail
    .getByRole("checkbox", { name: /批准精确候选并确认使用授权/ })
    .first();
  await exactReference.scrollIntoViewIfNeeded();
  await expect(exactReference).toBeInViewport();
  await page.screenshot({
    path: info.outputPath("concept-review-mobile.png"),
    fullPage: true,
  });
  for (const checkbox of await detail.getByRole("checkbox").all())
    await checkbox.check();
  await approve.click();
  await expect(detail.getByText("已批准", { exact: true })).toBeVisible();
  for (const round of after.concept_rounds) {
    for (const candidate of round.candidates) {
      const asset = await (
        await page.request.get(
          `/api/v1/projects/${project.id}/assets/${candidate.asset_id}/versions/${candidate.asset_version_id}`,
          { headers },
        )
      ).json();
      expect(asset.status).toBe("APPROVED");
    }
  }
});

test("AART-03: cancellation during external waiting keeps the original job and no media attempt", async ({
  page,
}) => {
  const { headers, project, prefix } = await setup(page);
  await concepts(page, "取消概念图等待任务");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText(/等待概念图生成与媒体检查/),
  ).toBeVisible({ timeout: 20000 });
  await page.reload();
  await open(page);
  await progress(page)
    .getByRole("button", { name: "取消美术生成", exact: true })
    .click();
  await expect(
    current(page).locator("> div").getByText("已取消", { exact: true }),
  ).toBeVisible();
  await expect(
    progress(page)
      .getByRole("article", { name: "第 1 轮概念图", exact: true })
      .getByText("已取消", { exact: true }),
  ).toBeVisible();
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  const saved = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/art`, { headers })
  ).json();
  expect(saved.delivery).toBeNull();
  expect(saved.concept_rounds).toHaveLength(1);
  const job = await (
    await page.request.get(
      `/api/v1/projects/${project.id}/generation-jobs/${saved.concept_rounds[0].job_id}`,
      { headers },
    )
  ).json();
  expect(job.status).toBe("CANCELLED");
  expect(job.attempt_count).toBe(0);
});

test("AART-03: unknown concept result is visible after refresh and never starts another attempt", async ({
  page,
}) => {
  const { headers, project, prefix } = await setup(page);
  await concepts(page, "未知概念图结果需要核验");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText("原概念图任务需要核验，请先处理原结果。", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 20000 });
  await expect(progress(page).getByText(/实际费用：未知/)).toBeVisible();
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  await page.reload();
  await open(page);
  await expect(
    progress(page).getByText("原概念图任务需要核验，请先处理原结果。", {
      exact: true,
    }),
  ).toBeVisible();
  const saved = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/art`, { headers })
  ).json();
  expect(saved.concept_rounds).toHaveLength(1);
  expect(saved.concept_rounds[0].actual_cost).toBeNull();
  const attempts = await (
    await page.request.get(
      `/api/v1/projects/${project.id}/generation-jobs/${saved.concept_rounds[0].job_id}/attempts`,
      { headers },
    )
  ).json();
  expect(attempts).toHaveLength(1);
  expect(saved.delivery).toBeNull();
});

test("AART-03: lost receipt restores the frozen concept plan with the same logical key", async ({
  page,
}) => {
  const { headers, prefix } = await setup(page);
  await concepts(page, "取消概念图回执恢复");
  await page.getByLabel("概念图总预算（USD）", { exact: true }).fill("3");
  const keys: string[] = [];
  let dropped = false;
  await page.route(new RegExp(prefix + "/art-runs$"), async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    if (!dropped) {
      dropped = true;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    page.getByLabel("生成美术提案", { exact: true }).getByRole("alert"),
  ).toBeVisible();
  await page.reload();
  await open(page);
  await selectEpisode(page);
  await expect(
    page.getByLabel("生成概念图并比较修订", { exact: true }),
  ).toBeChecked();
  await expect(
    page.getByLabel("概念图总预算（USD）", { exact: true }),
  ).toHaveValue("3");
  await expect(
    page.getByLabel("概念图宽度（像素）", { exact: true }),
  ).toHaveValue("8");
  await page.getByRole("button", { name: "开始美术生成", exact: true }).click();
  await expect(
    progress(page).getByText(/等待概念图生成与媒体检查/),
  ).toBeVisible({ timeout: 20000 });
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (await page.request.get(prefix + "/runs", { headers })).json(),
  ).toHaveLength(1);
  // End the synthetic wait before later files reuse the same browser server.
  await progress(page)
    .getByRole("button", { name: "取消美术生成", exact: true })
    .click();
  await expect(
    current(page).locator("> div").getByText("已取消", { exact: true }),
  ).toBeVisible();
});
