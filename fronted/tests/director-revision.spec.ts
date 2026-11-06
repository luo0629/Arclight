import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page, manual = true) {
  const auth = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `director-check-${crypto.randomUUID()}@example.test`,
      password: "director-revision-test",
      display_name: "导演检查验收",
    },
  });
  expect(auth.status()).toBe(201);
  const session = await auth.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "纸船检查" },
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
    const saved = await page.request.post(prefix + "/scripts", {
      headers,
      data: {
        expected_version_no: 0,
        content: {
          schema_version: "2.0",
          title: "待检查的三个场景",
          summary: "寻找失主",
          characters: ["主角"],
          estimated_duration_ms: 30000,
          scenes: [1, 2, 3].map((number) => ({
            scene_id: crypto.randomUUID(),
            scene_no: number,
            title: `河岸${number}`,
            action: "主角沿河岸送信。",
            notes: number < 3 ? "导演检查遗漏" : "",
            dialogue: [
              {
                line_id: crypto.randomUUID(),
                speaker: "主角",
                text: `第${number}处线索。`,
              },
            ],
          })),
        },
      },
    });
    expect(saved.status()).toBe(201);
    const source = await saved.json();
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
  return { prefix, headers };
}

async function standalone(page: Page, rounds: number) {
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "待检查的三个场景" })
    .click();
  await page
    .getByRole("radio", { name: "拆镜、检查与局部修订", exact: true })
    .click();
  await page
    .getByLabel("最多导演自动修订轮数", { exact: true })
    .fill(String(rounds));
  await page
    .getByRole("button", { name: "从此版本生成分镜", exact: true })
    .click();
}

test("checked director repairs two local ranges and shows actual checks across refresh and review", async ({
  page,
}, testInfo) => {
  const { prefix, headers } = await setup(page);
  await standalone(page, 2);
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 25000 });
  const working = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await working
    .locator("summary")
    .filter({ hasText: "导演检查与修订 1 · 已保存" })
    .click();
  const round = working.getByRole("region", {
    name: "导演检查第 1 轮",
    exact: true,
  });
  await expect(round.getByText("剩余导演自动修订：2 轮")).toBeVisible();
  await expect(
    round.getByText("原剧本对白尚未被镜头覆盖").first(),
  ).toBeVisible();
  await round
    .locator("summary")
    .filter({ hasText: "实际工具检查 1：仍有问题" })
    .click();
  await expect(
    round.getByText("对白覆盖 1 / 3 条", { exact: false }).first(),
  ).toBeVisible();
  await round
    .locator("summary")
    .filter({ hasText: "局部方案 1 · 已采用" })
    .click();
  await expect(
    round.getByText("场景 1 · 替换场景内镜头 1–1 · 新镜头 1 个"),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("director-checks-and-local-patch.png"),
    fullPage: true,
  });
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  expect(runs[0].workflow_version).toBe("director/2.0");
  const progressUrl = `/api/v1/runs/${runs[0].id}/director`;
  const before = await (
    await page.request.get(progressUrl, { headers })
  ).json();
  const reports = before.operations.filter(
    (op: { kind: string }) => op.kind === "director_check",
  );
  expect(
    reports.map((op: { result: { decision: string } }) => op.result.decision),
  ).toEqual(["REVISE", "REVISE", "ACCEPT"]);
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await working
    .locator("summary")
    .filter({ hasText: "导演检查与修订 3 · 已保存" })
    .click();
  await expect(
    working.getByText("工具检查通过，交完整分镜审核。", { exact: false }),
  ).toBeVisible();
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
    boards[0].content_json.shots.flatMap(
      (shot: { dialogue_line_ids: string[] }) => shot.dialogue_line_ids,
    ),
  ).toHaveLength(3);
  expect(
    (
      await (await page.request.get(progressUrl, { headers })).json()
    ).operations.map((op: { loop_id: string }) => op.loop_id),
  ).toEqual(before.operations.map((op: { loop_id: string }) => op.loop_id));
});

test("exhausted director preserves located issues and answers cannot reset rounds", async ({
  page,
}, testInfo) => {
  const { prefix, headers } = await setup(page);
  await standalone(page, 0);
  const working = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await expect(
    working.getByText("导演需要你的决定：", { exact: false }),
  ).toBeVisible({ timeout: 25000 });
  await expect(working.getByText("剩余导演自动修订：0 轮")).toBeVisible();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await working
    .getByLabel("创作问题回答", { exact: true })
    .fill("继续核验但保持原限额");
  await working
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(
    working.getByText("问题仍需修订，原额度已耗尽；请停止后重新发起。", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 20000 });
  await expect(working.getByText("剩余导演自动修订：0 轮")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("director-exhaustion-question.png"),
    fullPage: true,
  });
  await working
    .getByRole("button", { name: "停止本次创作", exact: true })
    .click();
  await expect(page.locator(".task .status")).toHaveText("已取消");
  expect(
    await (await page.request.get(prefix + "/storyboards", { headers })).json(),
  ).toHaveLength(0);
});

test("combined checked director shares creation controls and keeps both reviews", async ({
  page,
}) => {
  const { prefix, headers } = await setup(page, false);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill("雨夜发现纸船，沿河寻找失主。");
  await page
    .getByRole("radio", { name: "调查、规划与分段创作", exact: true })
    .click();
  await page
    .getByRole("radio", { name: "拆镜、检查与局部修订", exact: true })
    .click();
  await page.getByLabel("最多导演自动修订轮数", { exact: true }).fill("2");
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
    .filter({ hasText: "导演检查与修订 1 · 已保存" })
    .click();
  await expect(
    working.getByText("工具检查通过，交完整分镜审核。", { exact: false }),
  ).toBeVisible();
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  expect(runs[0].workflow_version).toBe("production/3.0");
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
});
