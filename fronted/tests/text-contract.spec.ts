import { expect, test, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function openShotDetails(page: Page) {
  const details = page.locator("details.shot-details");
  if ((await details.getAttribute("open")) === null) {
    await details.locator("summary").click();
  }
  await expect(details).toHaveAttribute("open", "");
}

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `contract-${crypto.randomUUID()}@example.com`,
      password: "text-contract-test",
      display_name: "合同验收员",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "文本合同验收" },
    })
  ).json();
  const episode = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 1, title: "雨夜" },
    })
  ).json();
  const prefix = "/api/v1/episodes/" + episode.id;
  const response = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "稳定台词稿",
        summary: "女孩在雨夜找到猫。",
        characters: ["女孩", "猫"],
        scenes: [
          {
            scene_no: 1,
            title: "巷口",
            action: "女孩驻足",
            dialogue: [
              { speaker: "女孩", text: "找到你了。" },
              { speaker: "旁白", text: "雨停了。" },
            ],
          },
          { scene_no: 2, title: "屋檐", action: "猫抬头", dialogue: [] },
        ],
      },
    },
  });
  expect(response.status()).toBe(201);
  const script = await response.json();
  return { headers, prefix, script };
}

async function beginBoard(page: Page, env: Awaited<ReturnType<typeof setup>>) {
  expect(
    (
      await page.request.post(
        env.prefix + "/scripts/" + env.script.id + "/review",
        { headers: env.headers, data: { decision: "APPROVE" } },
      )
    ).status(),
  ).toBe(200);
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page
    .getByRole("option", { name: "v1 · 稳定台词稿", exact: true })
    .click();
  await page.getByRole("button", { name: "使用此剧本起稿" }).click();
}

test("TXT-09：情绪另存、类别联动与台词稳定身份，旧版本不变", async ({
  page,
}) => {
  const env = await setup(page);
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "稳定台词稿" }).click();
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("台词情绪", { exact: true }).first().fill("释然");
  await page
    .getByLabel("说话角色", { exact: true })
    .first()
    .selectOption("旁白");
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("剧本 v2");
  const versions = await (
    await page.request.get(env.prefix + "/scripts", { headers: env.headers })
  ).json();
  const line = versions[0].content_json.scenes[0].dialogue[0];
  expect(line).toMatchObject({
    line_id: env.script.content_json.scenes[0].dialogue[0].line_id,
    line_type: "narration",
    speaker: "旁白",
    emotion: "释然",
  });
  expect(versions[1].content_json).toEqual(env.script.content_json);
});

test("TXT-09：制作字段、精确台词引用及草稿恢复，角色移除不残留表演记录", async ({
  page,
}) => {
  page.on("dialog", (dialog) => dialog.accept());
  await page.setViewportSize({ width: 1440, height: 1100 });
  const env = await setup(page);
  await beginBoard(page, env);
  await page.getByLabel("画面描述", { exact: true }).fill("女孩在巷口抱起猫");
  const characters = page.getByLabel("出场角色（用中文逗号分隔）");
  await characters.fill("女孩，猫");
  await openShotDetails(page);
  await page.getByLabel("画面构图", { exact: true }).fill("人物偏左，屋檐留白");
  await page.getByLabel("女孩 · 动作", { exact: true }).fill("俯身抱猫");
  await page.getByLabel("女孩 · 表情", { exact: true }).fill("微笑");
  await page.getByLabel("女孩 · 视线", { exact: true }).fill("看向猫");
  await page.getByLabel("猫 · 动作", { exact: true }).fill("抬头");
  await characters.fill("女孩");
  await page
    .getByLabel("引用台词（可多选）", { exact: true })
    .selectOption(
      env.script.content_json.scenes[0].dialogue.map(
        (line: { line_id: string }) => line.line_id,
      ),
    );
  await page.getByLabel("镜尾转场", { exact: true }).selectOption("dissolve");
  await page.getByRole("button", { name: "添加音效", exact: true }).click();
  await page.getByLabel("音效 1", { exact: true }).fill("雨声渐弱");
  await page.getByLabel("人工备注", { exact: true }).fill("保留屋檐滴水声");
  await page.screenshot({ path: "test-results/text-contract-desktop.png" });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page
    .getByRole("option", { name: "v1 · 稳定台词稿", exact: true })
    .click();
  await page.getByRole("button", { name: "使用此剧本起稿" }).click();
  await openShotDetails(page);
  await expect(page.getByLabel("画面构图", { exact: true })).toHaveValue(
    "人物偏左，屋檐留白",
  );
  await expect(page.getByLabel("猫 · 动作", { exact: true })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByLabel("引用台词（可多选）", { exact: true }),
  ).toHaveValues(
    env.script.content_json.scenes[0].dialogue.map(
      (line: { line_id: string }) => line.line_id,
    ),
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/text-contract-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(
    page.getByText("人工分镜已保存为新版本 v1 · 待审核", { exact: false }),
  ).toBeVisible();
  const boards = await (
    await page.request.get(env.prefix + "/storyboards", {
      headers: env.headers,
    })
  ).json();
  expect(boards[0].script_version_id).toBe(env.script.id);
  expect(boards[0].content_json.shots[0]).toMatchObject({
    scene_id: env.script.content_json.scenes[0].scene_id,
    composition: "人物偏左，屋檐留白",
    dialogue_line_ids: env.script.content_json.scenes[0].dialogue.map(
      (line: { line_id: string }) => line.line_id,
    ),
    character_details: [
      {
        character: "女孩",
        action: "俯身抱猫",
        expression: "微笑",
        gaze: "看向猫",
      },
    ],
    sound_effects: ["雨声渐弱"],
    transition_out: "dissolve",
    notes: "保留屋檐滴水声",
  });
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await openShotDetails(page);
  await page.getByLabel("人工备注", { exact: true }).fill("另存备注");
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("分镜 v2");
  const after = await (
    await page.request.get(env.prefix + "/storyboards", {
      headers: env.headers,
    })
  ).json();
  expect(after[1].content_json).toEqual(boards[0].content_json);
  expect(after[0].content_json.shots[0].dialogue_line_ids).toEqual(
    boards[0].content_json.shots[0].dialogue_line_ids,
  );
});

test("TXT-09：切换场景清空旧台词，归档只读展示元数据", async ({ page }) => {
  const env = await setup(page);
  await beginBoard(page, env);
  await page.getByLabel("画面描述", { exact: true }).fill("屋檐下的空镜");
  await openShotDetails(page);
  await page
    .getByLabel("引用台词（可多选）", { exact: true })
    .selectOption(env.script.content_json.scenes[0].dialogue[1].line_id);
  await page.getByLabel("场景编号", { exact: true }).selectOption("2");
  await expect(
    page.getByLabel("引用台词（可多选）", { exact: true }),
  ).toHaveValues([]);
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(
    page.getByText("人工分镜已保存为新版本 v1 · 待审核", { exact: false }),
  ).toBeVisible();
  expect(
    (
      await page.request.patch(env.prefix, {
        headers: env.headers,
        data: { archived: true },
      })
    ).ok(),
  ).toBeTruthy();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "分镜版本 1" }).click();
  await expect(
    page.getByRole("button", { name: "编辑并另存新版本" }),
  ).toHaveCount(0);
  await page.getByText("稳定标识与精确引用（只读）", { exact: true }).click();
  await expect(page.locator(".contract-metadata pre")).toContainText(
    env.script.content_json.scenes[1].scene_id,
  );
});
