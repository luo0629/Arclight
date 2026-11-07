import { expect, test, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page, note = "") {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `director-art-${crypto.randomUUID()}@example.test`,
      password: "director-art-browser",
      display_name: "导演美术协作",
    },
  });
  expect(response.status()).toBe(201);
  const auth = await response.json();
  const headers = { Authorization: `Bearer ${auth.access_token}` };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "纸船视觉协作" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "雨夜" },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const saved = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        schema_version: "2.0",
        title: "纸船协作稿",
        summary: "沿河寻找失主",
        characters: ["主角"],
        estimated_duration_ms: 30000,
        scenes: [
          {
            scene_id: crypto.randomUUID(),
            scene_no: 1,
            title: "河岸",
            action: "主角拾起纸船，沿河寻找失主。",
            notes: note,
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
  expect(saved.status(), await saved.text()).toBe(201);
  const script = await saved.json();
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
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "纸船协作稿" }).click();
  await page
    .getByRole("radio", { name: "拆镜、检查与局部修订", exact: true })
    .click();
  await page.getByRole("radio", { name: "按需征询美术", exact: true }).click();
  await page
    .getByRole("button", { name: "从此版本生成分镜", exact: true })
    .click();
  return { prefix, headers, script };
}

for (const decline of [false, true]) {
  test(`DIRECT-04: ${decline ? "declined" : "adopted"} art advice retains the working snapshot and original storyboard review`, async ({
    page,
  }, info) => {
    const { prefix, headers, script } = await setup(
      page,
      decline ? "拒绝美术" : "",
    );
    await expect(
      page.getByText("等待审核：分镜版本", { exact: false }),
    ).toBeVisible({ timeout: 30000 });
    const work = page.getByRole("region", { name: "导演计划与镜头工作稿" });
    await expect(
      work.getByText("美术意见基于工作中镜头方案", { exact: false }),
    ).toBeVisible();
    await work
      .locator("summary")
      .filter({ hasText: "美术意见（工作中镜头）" })
      .click();
    await expect(
      work.getByText("导演的雨夜构图意见", { exact: true }),
    ).toBeVisible();
    await work
      .locator("summary")
      .filter({ hasText: "导演检查与修订 1 · 已保存" })
      .click();
    await expect(
      work.getByText(decline ? "美术意见：未采纳" : "美术意见：采纳", {
        exact: false,
      }),
    ).toBeVisible();
    const runs = await (
      await page.request.get(prefix + "/runs", { headers })
    ).json();
    expect(runs).toHaveLength(1);
    expect(runs[0].workflow_version).toBe("director/3.0");
    const url = `/api/v1/runs/${runs[0].id}/director`;
    const before = await (await page.request.get(url, { headers })).json();
    expect(before.art_opinion_request.script_version_id).toBe(script.id);
    expect(before.art_opinion_request.storyboard_status).toBe(
      "WORK_IN_PROGRESS",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: info.outputPath("director-art-mobile.png"),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await page.reload();
    await openWorkspacePanels(page);
    await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
    const after = await (await page.request.get(url, { headers })).json();
    expect(
      after.operations.map((item: { loop_id: string }) => item.loop_id),
    ).toEqual(
      before.operations.map((item: { loop_id: string }) => item.loop_id),
    );
    await page.getByRole("button", { name: "批准并继续", exact: true }).click();
    await expect(page.locator(".task .status")).toHaveText("已完成", {
      timeout: 20000,
    });
    const boards = await (
      await page.request.get(prefix + "/storyboards", { headers })
    ).json();
    expect(boards).toHaveLength(1);
    expect(boards[0].status).toBe("APPROVED");
  });
}

test("DIRECT-04: art clarification answers resume the same director run after refresh", async ({
  page,
}) => {
  const { prefix, headers } = await setup(page, "美术澄清");
  const work = page.getByRole("region", { name: "导演计划与镜头工作稿" });
  await expect(
    work.getByText("美术方案采用冷色还是暖色？", { exact: true }),
  ).toBeVisible({ timeout: 30000 });
  const runs = await (
    await page.request.get(prefix + "/runs", { headers })
  ).json();
  const url = `/api/v1/runs/${runs[0].id}/director`;
  const before = await (await page.request.get(url, { headers })).json();
  const opinion = before.operations.find(
    (item: { kind: string }) => item.kind === "art_opinion",
  );
  expect(opinion.status).toBe("WAITING_HUMAN");
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await work.getByRole("radio", { name: "冷色", exact: true }).click();
  await work
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 30000 });
  const after = await (await page.request.get(url, { headers })).json();
  const completed = after.operations.find(
    (item: { kind: string }) => item.kind === "art_opinion",
  );
  expect(completed.loop_id).toBe(opinion.loop_id);
  expect(completed.status).toBe("COMPLETED");
  expect(
    await (await page.request.get(prefix + "/runs", { headers })).json(),
  ).toHaveLength(1);
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
});
