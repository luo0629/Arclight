import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page } from "@playwright/test";

const scriptContent = (title: string) => ({
  title,
  summary: "女孩在森林中找到了星星。",
  characters: ["女孩", "星星"],
  scenes: [
    {
      scene_no: 1,
      title: "森林入口",
      action: "女孩提灯走进森林。",
      dialogue: [],
    },
    {
      scene_no: 2,
      title: "星光湖边",
      action: "星星在湖边闪烁。",
      dialogue: [],
    },
  ],
});
const boardContent = {
  shots: [
    {
      shot_no: 1,
      scene_no: 1,
      duration_ms: 3000,
      shot_size: "medium",
      visual_description: "另一位编辑保存的画面",
      characters: [],
      camera_movement: "static",
      subtitle: "",
    },
  ],
};

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `board-${crypto.randomUUID()}@example.com`,
      password: "manual-storyboard-test",
      display_name: "人工分镜师",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: `Bearer ${owner.access_token}` };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "人工分镜验收" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "森林的星光" } },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  async function script(
    expected: number,
    title: string,
    approve = false,
    path = prefix,
  ) {
    const response = await page.request.post(path + "/scripts", {
      headers,
      data: { expected_version_no: expected, content: scriptContent(title) },
    });
    expect(response.status()).toBe(201);
    const row = await response.json();
    if (approve)
      expect(
        (
          await page.request.post(path + "/scripts/" + row.id + "/review", {
            headers,
            data: { decision: "APPROVE" },
          })
        ).ok(),
      ).toBeTruthy();
    return row;
  }
  return { headers, project, episode, prefix, script };
}

async function begin(page: Page, option: string) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
  await page.getByRole("button", { name: "使用此剧本起稿" }).click();
}

test("TXT-01：选择旧的精确已批准剧本，空白起稿、镜头增删、独立审核及不可变另存", async ({
  page,
}) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { headers, prefix, script, project } = await setup(page);
  const source = await script(0, "旧稿星光", true);
  await script(1, "新版星光", true);
  await script(2, "未批准的星光");
  const otherResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 2, title: "另一集" } },
  );
  expect(otherResponse.status()).toBe(201);
  const other = await otherResponse.json();
  await script(0, "另一集的已批准剧本", true, `/api/v1/episodes/${other.id}`);
  await page.goto("/");
  await openWorkspacePanels(page);
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "使用此剧本起稿" }),
  ).toBeDisabled();
  const selector = page.getByRole("combobox", { name: "已批准的剧本版本" });
  await selector.focus();
  await page.keyboard.press("Space");
  await expect(page.getByRole("option")).toHaveCount(2);
  await expect(
    page.getByRole("option", { name: "未批准的星光", exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("option", { name: "另一集的已批准剧本", exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("option", { name: "v2 · 新版星光", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("option", { name: "v1 · 旧稿星光", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(selector).toHaveText("v1 · 旧稿星光");
  await expect(selector).toBeFocused();
  await page.getByRole("button", { name: "使用此剧本起稿" }).click();
  await expect(
    page.getByText("精确版本：" + source.id, { exact: false }),
  ).toBeVisible();
  const save = page.getByRole("button", { name: "保存人工分镜", exact: true });
  await expect(save).toBeDisabled();
  await expect(page.getByRole("button", { name: "移除此镜头" })).toBeDisabled();
  await page
    .getByLabel("画面描述", { exact: true })
    .fill("女孩拨开树枝，看见湖边的星星。");
  await page.getByLabel("场景编号", { exact: true }).selectOption("2");
  await page.getByLabel("时长（秒）").fill("0.4");
  await expect(save).toBeDisabled();
  await page.getByLabel("时长（秒）").fill("4.5");
  await page.getByLabel("出场角色（用中文逗号分隔）").fill("女孩，星星");
  await page.getByLabel("字幕", { exact: true }).fill("找到你啦。");
  await page.getByRole("button", { name: "添加镜头", exact: true }).click();
  await page
    .getByLabel("画面描述", { exact: true })
    .nth(1)
    .fill("暂时移除的镜头");
  await page.getByRole("button", { name: "添加镜头", exact: true }).click();
  await page
    .getByLabel("画面描述", { exact: true })
    .nth(2)
    .fill("星星在女孩手心轻轻闪烁。");
  await page.getByRole("button", { name: "移除此镜头" }).nth(1).click();
  await expect(
    page.getByRole("heading", { name: "镜头 02", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "镜头 03", exact: true }),
  ).toHaveCount(0);
  await page.screenshot({
    path: "test-results/manual-storyboard-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(save).toBeEnabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/manual-storyboard-mobile.png",
    fullPage: true,
  });
  await save.click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "人工分镜已保存为新版本 v1 · 待审核" }),
  ).toBeVisible();
  const boards = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(boards).toHaveLength(1);
  expect(boards[0]).toMatchObject({
    script_version_id: source.id,
    run_id: null,
    status: "READY_FOR_REVIEW",
    version_no: 1,
  });
  expect(boards[0].content_json.shots).toMatchObject([
    {
      shot_no: 1,
      scene_no: 2,
      duration_ms: 4500,
      characters: ["女孩", "星星"],
      subtitle: "找到你啦。",
    },
    { shot_no: 2, visual_description: "星星在女孩手心轻轻闪烁。" },
  ]);
  expect(
    await (await page.request.get(prefix + "/runs", { headers })).json(),
  ).toEqual([]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(page.locator(".version-row.active")).toContainText("版本 1");
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v1",
  );
  await expect(page.locator(".inspector .section-heading .status")).toHaveText(
    "待审核",
  );
  await page.getByRole("button", { name: "批准该版本" }).click();
  await expect(page.locator(".inspector .section-heading .status")).toHaveText(
    "已批准",
  );
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page
    .getByLabel("画面描述", { exact: true })
    .first()
    .fill("修改后的湖边画面");
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "人工分镜已保存为新版本 v2 · 待审核" }),
  ).toBeVisible();
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v2",
  );
  await expect(page.locator(".inspector .section-heading .status")).toHaveText(
    "待审核",
  );
  const versions = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(versions).toHaveLength(2);
  expect(versions[0]).toMatchObject({
    version_no: 2,
    script_version_id: source.id,
    status: "READY_FOR_REVIEW",
  });
  expect(versions[1].content_json).toEqual(boards[0].content_json);
  expect(versions[1].status).toBe("APPROVED");
  expect(crashes).toEqual([]);
});

test("TXT-01：没有已批准版本时提示前置条件，选择器读取第 101 个历史版本", async ({
  page,
}) => {
  test.setTimeout(120000);
  const { headers, prefix, script } = await setup(page);
  const first = await script(0, "历史批准稿");
  await page.goto("/");
  await openWorkspacePanels(page);
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await expect(
    page.getByText("本剧集还没有已批准的剧本", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存人工分镜", exact: true }),
  ).toHaveCount(0);
  expect(
    (
      await page.request.post(prefix + "/storyboards", {
        headers,
        data: {
          expected_version_no: 0,
          script_version_id: first.id,
          content: boardContent,
        },
      })
    ).status(),
  ).toBe(409);
  expect(
    (
      await page.request.post(prefix + "/scripts/" + first.id + "/review", {
        headers,
        data: { decision: "APPROVE" },
      })
    ).ok(),
  ).toBeTruthy();
  for (let index = 1; index <= 100; index++)
    await script(index, "尚未批准 " + index);
  await page.reload();
  await openWorkspacePanels(page);
  await begin(page, "v1 · 历史批准稿");
  await page
    .getByLabel("画面描述", { exact: true })
    .fill("从第 101 条历史记录中选中的来源");
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(
    page.getByText("人工分镜已保存为新版本 v1 · 待审核", { exact: false }),
  ).toBeVisible();
  const boards = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(boards[0].script_version_id).toBe(first.id);
});

test("TXT-01：无效引用与真实版本冲突保留草稿，刷新后显式重试", async ({
  page,
}) => {
  const { headers, prefix, script } = await setup(page);
  const source = await script(0, "冲突验收稿", true);
  await page.goto("/");
  await openWorkspacePanels(page);
  await begin(page, "v1 · 冲突验收稿");
  await page.getByLabel("画面描述", { exact: true }).fill("不能丢失的人工草稿");
  await page.getByLabel("出场角色（用中文逗号分隔）").fill("不存在的角色");
  const save = page.getByRole("button", { name: "保存人工分镜", exact: true });
  await save.click();
  await expect(page.getByRole("alert")).toContainText(
    "分镜引用了无效的场景或角色",
  );
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "不能丢失的人工草稿",
  );
  await page.getByLabel("出场角色（用中文逗号分隔）").fill("女孩");
  expect(
    (
      await page.request.post(prefix + "/storyboards", {
        headers,
        data: {
          expected_version_no: 0,
          script_version_id: source.id,
          content: boardContent,
        },
      })
    ).status(),
  ).toBe(201);
  await save.click();
  await expect(page.getByRole("alert")).toContainText(
    "内容已有新版本，请刷新后再保存",
  );
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "不能丢失的人工草稿",
  );
  await page.getByRole("button", { name: "刷新内容", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "刷新内容", exact: true }),
  ).toBeEnabled();
  await save.click();
  await expect(
    page.getByText("人工分镜已保存为新版本 v2 · 待审核", { exact: false }),
  ).toBeVisible();
  const boards = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(boards).toHaveLength(2);
  expect(boards[0].content_json.shots[0].visual_description).toBe(
    "不能丢失的人工草稿",
  );
  expect(boards[1].content_json).toMatchObject(boardContent);
});

test("TXT-01：剧集归档与活动任务禁止人工起稿，加载失败可以重试", async ({
  page,
}) => {
  const { headers, prefix, script } = await setup(page);
  await script(0, "限制验收稿", true);
  await page.goto("/");
  await openWorkspacePanels(page);
  // 先等待工作台首批读取完成，再只模拟人工起稿选择器的失败。
  await expect(
    page.getByRole("button", { name: "自己写分镜", exact: true }),
  ).toBeVisible();
  await page.route("**/scripts?limit=100&offset=0", (route) => route.abort());
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重试加载剧本" }),
  ).toBeVisible();
  await page.unroute("**/scripts?limit=100&offset=0");
  await page.getByRole("button", { name: "重试加载剧本" }).click();
  await expect(
    page.getByRole("combobox", { name: "已批准的剧本版本" }),
  ).toBeVisible();
  expect(
    (
      await page.request.patch(prefix, { headers, data: { archived: true } })
    ).ok(),
  ).toBeTruthy();
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page.getByText("当前为只读模式", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "自己写分镜", exact: true }),
  ).toHaveCount(0);
  expect(
    (
      await page.request.patch(prefix, { headers, data: { archived: false } })
    ).ok(),
  ).toBeTruthy();
  const run = await page.request.post(prefix + "/script-runs", {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: { story: "一只猫在雨夜找到一把伞。" },
  });
  expect(run.status()).toBe(202);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "自己写分镜", exact: true }),
  ).toBeDisabled();
});
