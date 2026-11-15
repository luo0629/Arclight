import { writeFile } from "node:fs/promises";
import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("ui02-result.json"),
    JSON.stringify(
      {
        title: info.title,
        status: info.status,
        expectedStatus: info.expectedStatus,
        retry: info.retry,
        errors: info.errors,
      },
      null,
      2,
    ),
  );
});

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `table-${crypto.randomUUID()}@example.com`,
      password: "storyboard-table-test",
      display_name: "分镜表格验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "星光分镜表" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: { episode_no: 1, title: "提灯过桥", target_duration_ms: 30000 },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const sceneIds = [crypto.randomUUID(), crypto.randomUUID()];
  const lineIds = [crypto.randomUUID(), crypto.randomUUID()];
  const scriptResponse = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "提灯过桥",
        summary: "女孩提灯寻找星光。",
        estimated_duration_ms: 30000,
        characters: ["女孩"],
        scenes: [
          {
            scene_no: 1,
            scene_id: sceneIds[0],
            title: "林间小桥",
            action: "女孩提灯走向桥头。",
            dialogue: [
              {
                line_id: lineIds[0],
                speaker: "女孩",
                text: "星光就在前面。",
                line_type: "dialogue",
              },
            ],
          },
          {
            scene_no: 2,
            scene_id: sceneIds[1],
            title: "星光湖边",
            action: "星光落在女孩手心。",
            dialogue: [
              {
                line_id: lineIds[1],
                speaker: "旁白",
                text: "灯火照亮了夜色。",
                line_type: "narration",
              },
            ],
          },
        ],
      },
    },
  });
  expect(scriptResponse.status()).toBe(201);
  const source = await scriptResponse.json();
  expect(
    (
      await page.request.post(prefix + `/scripts/${source.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  const boardResponse = await page.request.post(prefix + "/storyboards", {
    headers,
    data: {
      expected_version_no: 0,
      script_version_id: source.id,
      content: {
        shots: ["提灯走入森林", "在桥边回头", "星光落在掌心"].map(
          (description, index) => ({
            shot_no: index + 1,
            scene_no: index === 2 ? 2 : 1,
            scene_id: sceneIds[index === 2 ? 1 : 0],
            duration_ms: 10000,
            shot_size: "medium",
            visual_description: description,
            characters: ["女孩"],
            camera_movement: "static",
            subtitle: `字幕 ${index + 1}`,
            dialogue_line_ids: [lineIds[index === 2 ? 1 : 0]],
            notes: `原备注 ${index + 1}`,
          }),
        ),
      },
    },
  });
  expect(boardResponse.status()).toBe(201);
  const board = await boardResponse.json();
  expect(
    (
      await page.request.post(prefix + `/storyboards/${board.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  async function versions() {
    return (
      await page.request.get(prefix + "/storyboards", { headers })
    ).json();
  }
  async function open(edit = true) {
    await page.goto(`/projects/${project.id}/episodes/${episode.id}`);
    await openWorkspacePanels(page);
    await page.getByRole("radio", { name: "版本列表", exact: true }).click();
    const versionRow = page
      .locator(".version-row")
      .filter({ hasText: board.id });
    await expect(versionRow).toHaveCount(1);
    await versionRow.click();
    await expect(page.locator(".inspector .section-heading h3")).toHaveText(
      "分镜 v1",
    );
    await page.getByRole("button", { name: "分镜表格", exact: true }).click();
    await expect(
      page.getByRole("table", { name: "分镜镜头表" }).getByRole("row"),
    ).toHaveCount(4);
    if (edit)
      await page
        .getByRole("button", { name: "编辑并另存新版本", exact: true })
        .click();
  }
  return {
    session,
    headers,
    project,
    episode,
    prefix,
    source,
    board,
    versions,
    open,
  };
}

test("UI-02：列表打开真实表格，多选批改和单行编辑另存精确新版本", async ({
  page,
}) => {
  const env = await setup(page);
  await env.open();
  const table = page.getByRole("table", { name: "分镜镜头表" });
  await expect(
    page.getByRole("button", { name: "应用到 0 个镜头" }),
  ).toBeDisabled();
  await page.getByLabel("选择镜头 1", { exact: true }).check();
  await page.getByLabel("选择镜头 3", { exact: true }).check();
  await page
    .getByLabel("批量修改字段", { exact: true })
    .selectOption("camera_movement");
  await page.getByLabel("批量修改值", { exact: true }).selectOption("push_in");
  await page.getByRole("button", { name: "应用到 2 个镜头" }).click();
  await expect(page.getByLabel("镜头 1 运镜", { exact: true })).toHaveValue(
    "push_in",
  );
  await expect(page.getByLabel("镜头 2 运镜", { exact: true })).toHaveValue(
    "static",
  );
  await expect(page.getByLabel("镜头 3 运镜", { exact: true })).toHaveValue(
    "push_in",
  );
  await page
    .getByLabel("镜头 2 画面描述", { exact: true })
    .fill("女孩站在桥边回望灯火");
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().endsWith(env.prefix + "/storyboards"),
  );
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  expect((await submitted).postDataJSON()).toMatchObject({
    expected_version_no: 1,
    base_version_id: env.board.id,
    script_version_id: env.source.id,
  });
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v2",
  );
  const versions = await env.versions();
  expect(versions).toHaveLength(2);
  const expected = structuredClone(env.board.content_json);
  expected.shots[0].camera_movement = expected.shots[2].camera_movement =
    "push_in";
  expected.shots[1].visual_description = "女孩站在桥边回望灯火";
  expect(versions[0].content_json).toEqual(expected);
  expect(versions[1].content_json).toEqual(env.board.content_json);
  expect(versions[0].status).toBe("READY_FOR_REVIEW");
  await page.getByRole("button", { name: "分镜表格", exact: true }).click();
  await expect(table.getByRole("row")).toHaveCount(4);
  await expect(table).toContainText("女孩站在桥边回望灯火");
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.screenshot({ path: "test-results/ui02-desktop.png" });
});

test("UI-02：拖放重排保留精确引用和选择，键盘上移下移连续编号", async ({
  page,
}) => {
  const env = await setup(page);
  await env.open();
  await page.getByLabel("选择镜头 1", { exact: true }).check();
  await page
    .getByRole("button", { name: "拖动镜头 1", exact: true })
    .dragTo(page.locator('[data-shot-number="3"]'));
  await expect(page.getByLabel("镜头 1 画面描述", { exact: true })).toHaveValue(
    "在桥边回头",
  );
  await expect(page.getByLabel("镜头 2 画面描述", { exact: true })).toHaveValue(
    "星光落在掌心",
  );
  await expect(page.getByLabel("镜头 3 画面描述", { exact: true })).toHaveValue(
    "提灯走入森林",
  );
  await expect(page.getByLabel("选择镜头 3", { exact: true })).toBeChecked();
  await expect(
    page.getByLabel("选择镜头 1", { exact: true }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "上移镜头 1", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "下移镜头 3", exact: true }),
  ).toBeDisabled();
  const up = page.getByRole("button", { name: "上移镜头 3", exact: true });
  await up.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("选择镜头 2", { exact: true })).toBeChecked();
  await page.getByRole("button", { name: "下移镜头 2", exact: true }).click();
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v2",
  );
  const saved = (await env.versions())[0].content_json.shots;
  expect(saved).toEqual(
    [
      env.board.content_json.shots[1],
      env.board.content_json.shots[2],
      env.board.content_json.shots[0],
    ].map((shot, index) => ({ ...shot, shot_no: index + 1 })),
  );
});

test("UI-02：全选与非法批量值不改草稿，逐行错误阻止保存", async ({ page }) => {
  const env = await setup(page);
  await env.open();
  await page.getByLabel("选择全部镜头", { exact: true }).check();
  await expect(
    page.getByRole("button", { name: "应用到 3 个镜头" }),
  ).toBeEnabled();
  await page
    .getByLabel("批量修改字段", { exact: true })
    .selectOption("duration_ms");
  await page.getByLabel("批量修改值", { exact: true }).fill("0.499");
  await expect(
    page.getByRole("button", { name: "应用到 3 个镜头" }),
  ).toBeDisabled();
  await expect(
    page.getByLabel("镜头 1 时长（秒）", { exact: true }),
  ).toHaveValue("10");
  await page
    .getByLabel("批量修改字段", { exact: true })
    .selectOption("visual_description");
  await page.getByLabel("批量修改值", { exact: true }).fill("   ");
  await expect(
    page.getByRole("button", { name: "应用到 3 个镜头" }),
  ).toBeDisabled();
  await page.getByLabel("选择全部镜头", { exact: true }).uncheck();
  await expect(
    page.getByRole("button", { name: "应用到 0 个镜头" }),
  ).toBeDisabled();
  await page.getByLabel("镜头 3 画面描述", { exact: true }).fill("");
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled();
  expect(await env.versions()).toHaveLength(1);
});

test("UI-02：批改排序草稿刷新恢复，显式放弃还原原顺序", async ({ page }) => {
  const env = await setup(page);
  await env.open();
  await page.getByLabel("选择镜头 2", { exact: true }).check();
  await page.getByLabel("批量修改字段", { exact: true }).selectOption("notes");
  await page.getByLabel("批量修改值", { exact: true }).fill("待导演确认的备注");
  await page.getByRole("button", { name: "应用到 1 个镜头" }).click();
  await page.getByRole("button", { name: "上移镜头 2", exact: true }).click();
  await page.reload();
  await openWorkspacePanels(page);
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v1",
  );
  await expect(
    page.getByText("已恢复未保存草稿。", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "分镜表格", exact: true }).click();
  await expect(page.getByLabel("镜头 1 画面描述", { exact: true })).toHaveValue(
    "在桥边回头",
  );
  await page.getByRole("button", { name: "镜头详情", exact: true }).click();
  await page.locator(".shot-details").first().locator("summary").click();
  await expect(
    page.getByLabel("人工备注", { exact: true }).first(),
  ).toHaveValue("待导演确认的备注");
  await page.getByRole("button", { name: "放弃本次编辑", exact: true }).click();
  await page.getByRole("button", { name: "确认放弃草稿", exact: true }).click();
  await page.getByRole("button", { name: "分镜表格", exact: true }).click();
  await expect(page.getByRole("table")).toContainText("提灯走入森林");
  expect(await env.versions()).toHaveLength(1);
});

test("UI-02：并发另存冲突保留表格草稿和全部旧版本", async ({ page }) => {
  const env = await setup(page);
  await env.open();
  await page.getByLabel("选择全部镜头", { exact: true }).check();
  await page.getByLabel("批量修改值", { exact: true }).selectOption("close_up");
  await page.getByRole("button", { name: "应用到 3 个镜头" }).click();
  await page.route("**" + env.prefix + "/storyboards", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await page.unroute("**" + env.prefix + "/storyboards");
    const concurrent = await page.request.post(env.prefix + "/storyboards", {
      headers: env.headers,
      data: {
        expected_version_no: 1,
        base_version_id: env.board.id,
        script_version_id: env.source.id,
        content: env.board.content_json,
      },
    });
    expect(concurrent.status()).toBe(201);
    const response = await route.fetch();
    expect(response.status()).toBe(409);
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(page.getByLabel("镜头 1 景别", { exact: true })).toHaveValue(
    "close_up",
  );
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeEnabled();
  await expect(page.locator(".workspace-error")).toBeVisible();
  const versions = await env.versions();
  expect(versions).toHaveLength(2);
  for (const row of versions)
    expect(row.content_json).toEqual(env.board.content_json);
});

test("UI-02：Viewer 可以切换只读表格，不能选择或修改镜头", async ({ page }) => {
  const env = await setup(page);
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `viewer-${crypto.randomUUID()}@example.com`,
      password: "table-viewer-test",
      display_name: "分镜观察者",
    },
  });
  expect(registered.status()).toBe(201);
  const viewer = await registered.json();
  expect(
    (
      await page.request.post(`/api/v1/projects/${env.project.id}/members`, {
        headers: env.headers,
        data: { email: viewer.user.email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  await env.open(false);
  await expect(page.getByRole("table", { name: "分镜镜头表" })).toContainText(
    "星光落在掌心",
  );
  await expect(
    page.getByRole("button", { name: "编辑并另存新版本", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("button", { name: "镜头详情", exact: true }).click();
  await page.getByRole("button", { name: "分镜表格", exact: true }).click();
  expect(
    (
      await page.request.post(env.prefix + "/storyboards", {
        headers: { Authorization: `Bearer ${viewer.access_token}` },
        data: {
          expected_version_no: 1,
          script_version_id: env.source.id,
          content: env.board.content_json,
        },
      })
    ).status(),
  ).toBe(403);
  expect(await env.versions()).toHaveLength(1);
});

test("UI-02：活动工作流到达后表格停止编辑且保留草稿", async ({ page }) => {
  const env = await setup(page);
  await env.open();
  await page
    .getByLabel("镜头 1 字幕", { exact: true })
    .fill("等待工作流结束后保存");
  const started = await page.request.post(env.prefix + "/storyboard-runs", {
    headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
    data: { script_version_id: env.source.id },
  });
  expect(started.status()).toBe(202);
  await page.getByRole("button", { name: "刷新内容", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "拖动镜头 1", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("table")).toContainText("等待工作流结束后保存");
  await expect(
    page.getByText("未保存草稿已暂存。", { exact: false }),
  ).toBeVisible();
});

test("UI-02：390px 表格区域独立滚动且键盘排序可用", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await setup(page);
  await env.open();
  await page.getByRole("button", { name: "下移镜头 1", exact: true }).click();
  await expect(page.getByLabel("镜头 1 画面描述", { exact: true })).toHaveValue(
    "在桥边回头",
  );
  const region = page.getByRole("region", { name: "分镜表格滚动区域" });
  await expect(region).toBeVisible();
  await expect(page.getByRole("table").getByRole("row")).toHaveCount(4);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  expect(
    await region.evaluate(
      (element) => element.scrollWidth > element.clientWidth,
    ),
  ).toBeTruthy();
  await region.screenshot({ path: "test-results/ui02-mobile.png" });
});

test("UI-02：批量景别、时长、转场、描述、字幕和备注保留精确来源", async ({
  page,
}) => {
  const env = await setup(page);
  await env.open();
  await page.getByLabel("选择镜头 2", { exact: true }).check();
  await page.getByLabel("选择镜头 3", { exact: true }).check();
  for (const [field, value] of [
    ["shot_size", "wide"],
    ["duration_ms", "9"],
    ["transition_out", "fade"],
    ["visual_description", "女孩沿着星光走向前方"],
    ["subtitle", "新的字幕"],
    ["notes", "确认灯光方向"],
  ]) {
    await page.getByLabel("批量修改字段", { exact: true }).selectOption(field);
    const input = page.getByLabel("批量修改值", { exact: true });
    if (["shot_size", "transition_out"].includes(field))
      await input.selectOption(value);
    else await input.fill(value);
    await page.getByRole("button", { name: "应用到 2 个镜头" }).click();
  }
  await page
    .getByLabel("批量修改字段", { exact: true })
    .selectOption("subtitle");
  await page.getByLabel("批量修改值", { exact: true }).fill("");
  await page.getByRole("button", { name: "应用到 2 个镜头" }).click();
  await page.getByLabel("镜头 1 时长（秒）", { exact: true }).fill("12");
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v2",
  );
  const shots = (await env.versions())[0].content_json.shots;
  expect(shots[0]).toEqual({
    ...env.board.content_json.shots[0],
    duration_ms: 12000,
  });
  for (const index of [1, 2])
    expect(shots[index]).toEqual({
      ...env.board.content_json.shots[index],
      shot_size: "wide",
      duration_ms: 9000,
      transition_out: "fade",
      visual_description: "女孩沿着星光走向前方",
      subtitle: "",
      notes: "确认灯光方向",
    });
});
