import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page } from "@playwright/test";

const content = (title: string) => ({
  title,
  summary: "一份用于草稿保护验收的剧本。",
  characters: ["主角"],
  scenes: [
    { scene_no: 1, title: "森林", action: "主角向前走。", dialogue: [] },
  ],
});
async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "draft-protection-test",
      display_name: "草稿验收",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "草稿保护" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "第一集" },
    })
  ).json();
  const second = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 2, title: "第二集" },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  async function script(expected: number, title: string, approved = false) {
    const saved = await page.request.post(prefix + "/scripts", {
      headers,
      data: { expected_version_no: expected, content: content(title) },
    });
    expect(saved.status()).toBe(201);
    const row = await saved.json();
    if (approved)
      expect(
        (
          await page.request.post(prefix + `/scripts/${row.id}/review`, {
            headers,
            data: { decision: "APPROVE" },
          })
        ).ok(),
      ).toBeTruthy();
    return row;
  }
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 第一集", exact: true }),
  ).toBeVisible();
  return {
    user: session.user,
    headers,
    project,
    episode,
    second,
    prefix,
    script,
    storageKey: `arclight:drafts:v1:${session.user.id}`,
  };
}
async function list(page: Page) {
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
}
async function select(page: Page, title: string) {
  await page.locator(".version-row").filter({ hasText: title }).click();
}
async function newScript(page: Page, title: string) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill(title);
  await page.getByLabel("梗概", { exact: true }).fill("保留这份梗概");
  await page.getByLabel("场景 1", { exact: true }).fill("森林");
  await page.getByLabel("场景动作", { exact: true }).fill("主角走进森林");
}
async function drafts(
  page: Page,
  key: string,
): Promise<Record<string, { label: string; value: unknown }>> {
  return page.evaluate(
    (key) => JSON.parse(sessionStorage.getItem(key) ?? "{}"),
    key,
  );
}
async function beginBoard(page: Page, title: string) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page.getByRole("option").filter({ hasText: title }).click();
  await page
    .getByRole("button", { name: "使用此剧本起稿", exact: true })
    .click();
}

test("TXT-06：精确版本隔离，切换视图、任务、设置、剧集和项目后可恢复，刷新有确认", async ({
  page,
}) => {
  const { headers, project, prefix, script, storageKey } = await setup(page);
  await script(0, "原稿甲");
  await script(1, "原稿乙");
  const other = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "另一个创作本" },
    })
  ).json();
  await page.reload();
  await openWorkspacePanels(page);
  await page
    .getByRole("combobox", { name: "选择项目", exact: true })
    .selectOption(project.id);
  await list(page);
  await select(page, "原稿甲");
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("甲的未保存修改");
  await select(page, "原稿乙");
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("乙的未保存修改");
  await select(page, "原稿甲");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "甲的未保存修改",
  );
  await page.getByRole("radio", { name: "创作画布", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "甲的未保存修改",
  );
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await list(page);
  await select(page, "原稿甲");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "甲的未保存修改",
  );
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第二集" })
    .click();
  await expect(page.locator(".episode-drafts")).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第一集" })
    .click();
  await list(page);
  await select(page, "原稿乙");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "乙的未保存修改",
  );
  await page.getByRole("button", { name: "刷新内容", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "乙的未保存修改",
  );
  let warning = 0;
  page.on("dialog", async (dialog) => {
    expect(dialog.type()).toBe("beforeunload");
    warning++;
    await dialog.accept();
  });
  await page.reload();
  await openWorkspacePanels(page);
  await expect(page.locator(".episode-drafts")).toContainText("剧本 v1 的修改");
  await page
    .getByRole("combobox", { name: "选择项目", exact: true })
    .selectOption(other.id);
  await expect(page.locator(".episode-drafts")).toHaveCount(0);
  await page
    .getByRole("combobox", { name: "选择项目", exact: true })
    .selectOption(project.id);
  await list(page);
  await select(page, "原稿甲");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "甲的未保存修改",
  );
  await page.reload();
  await openWorkspacePanels(page);
  expect(warning).toBeGreaterThan(0);
  await list(page);
  await select(page, "原稿乙");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "乙的未保存修改",
  );
  expect(Object.keys(await drafts(page, storageKey))).toHaveLength(2);
  const rows = await (
    await page.request.get(prefix + "/scripts", { headers })
  ).json();
  expect(
    rows.map(
      (row: { content_json: { title: string } }) => row.content_json.title,
    ),
  ).toEqual(["原稿乙", "原稿甲"]);
});

test("TXT-06：手机人工起稿和灵感切集及刷新恢复，显式放弃需确认，成功保存清除", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { storageKey } = await setup(page);
  await page.getByLabel("故事灵感", { exact: true }).fill("雨夜的女孩和猫");
  await newScript(page, "手机未完成的剧本");
  await page.getByRole("button", { name: "收起人工起稿", exact: true }).click();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "手机未完成的剧本",
  );
  await page.getByRole("button", { name: "放弃草稿", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveAccessibleName("放弃未保存草稿？");
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "手机未完成的剧本",
  );
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第二集" })
    .click();
  await expect(page.getByLabel("故事灵感", { exact: true })).toHaveValue("");
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第一集" })
    .click();
  await expect(page.getByLabel("故事灵感", { exact: true })).toHaveValue(
    "雨夜的女孩和猫",
  );
  page.on("dialog", (dialog) => dialog.accept());
  await page.reload();
  await openWorkspacePanels(page);
  await expect(page.getByLabel("故事灵感", { exact: true })).toHaveValue(
    "雨夜的女孩和猫",
  );
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "手机未完成的剧本",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/draft-protection-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(page.locator(".saved-version-notice")).toContainText("已保存");
  expect(
    Object.values(await drafts(page, storageKey)).map((row) => row.label),
  ).toEqual(["故事灵感"]);
  await list(page);
  await page.locator(".version-row").filter({ hasText: "第一集" }).click();
  await page.getByLabel("故事灵感", { exact: true }).fill("");
  expect(await drafts(page, storageKey)).toEqual({});
});

test("TXT-06：分镜起稿按精确来源隔离，旧分镜另存草稿也可恢复", async ({
  page,
}) => {
  const { script, headers, prefix, storageKey } = await setup(page);
  const source = await script(0, "来源甲", true);
  await script(1, "来源乙", true);
  await page.reload();
  await openWorkspacePanels(page);
  await beginBoard(page, "来源甲");
  await page.getByLabel("画面描述", { exact: true }).fill("甲的镜头草稿");
  await page.getByRole("button", { name: "收起人工分镜", exact: true }).click();
  await beginBoard(page, "来源乙");
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue("");
  await page.getByLabel("画面描述", { exact: true }).fill("乙的镜头草稿");
  await page.getByRole("button", { name: "收起人工分镜", exact: true }).click();
  await beginBoard(page, "来源甲");
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "甲的镜头草稿",
  );
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(page.locator(".saved-version-notice")).toContainText(
    "人工分镜已保存",
  );
  const boards = await (
    await page.request.get(prefix + "/storyboards", { headers })
  ).json();
  expect(boards[0].script_version_id).toBe(source.id);
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page
    .getByLabel("画面描述", { exact: true })
    .fill("分镜 v1 的另一份修改");
  await list(page);
  await select(page, "来源乙");
  await page
    .locator(".version-row")
    .filter({ hasText: "分镜" })
    .filter({ hasText: "版本 1" })
    .click();
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "分镜 v1 的另一份修改",
  );
  await page.getByRole("button", { name: "放弃本次编辑", exact: true }).click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "分镜 v1 的另一份修改",
  );
  await page.getByRole("button", { name: "放弃本次编辑", exact: true }).click();
  await page.getByRole("button", { name: "确认放弃草稿", exact: true }).click();
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveCount(0);
  expect(
    Object.values(await drafts(page, storageKey)).map(
      (row) =>
        (row.value as { shots: { visual_description: string }[] }).shots[0]
          .visual_description,
    ),
  ).toEqual(["乙的镜头草稿"]);
});

test("TXT-06：保存冲突保留草稿，POST 成功即清除，后续 GET 失败不重新提交", async ({
  page,
}) => {
  const { prefix, storageKey } = await setup(page);
  await newScript(page, "失败后仍保留");
  await page.route("**" + prefix + "/scripts", async (route) => {
    if (route.request().method() === "POST")
      return route.fulfill({ status: 409, json: { detail: "版本冲突" } });
    await route.continue();
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "失败后仍保留",
  );
  expect(Object.keys(await drafts(page, storageKey))).toHaveLength(1);
  await page.unroute("**" + prefix + "/scripts");
  let posts = 0;
  let saved = false;
  await page.route("**" + prefix + "/scripts*", async (route) => {
    if (route.request().method() === "POST") {
      posts++;
      const response = await route.fetch();
      saved = true;
      return route.fulfill({ response });
    }
    if (saved)
      return route.fulfill({ status: 503, json: { detail: "暂时无法读取" } });
    await route.continue();
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重试刷新已保存版本", exact: true }),
  ).toBeVisible();
  expect(await drafts(page, storageKey)).toEqual({});
  await page.unroute("**" + prefix + "/scripts*");
  await page
    .getByRole("button", { name: "重试刷新已保存版本", exact: true })
    .click();
  await expect(page.locator(".saved-version-notice")).toContainText("已保存");
  expect(posts).toBe(1);
});

test("TXT-06：存储拒绝写入时明确警告，内存草稿跨剧集恢复且刷新可取消", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const set = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("arclight:drafts:"))
        throw new DOMException("Full", "QuotaExceededError");
      return set.call(this, key, value);
    };
  });
  await setup(page);
  await newScript(page, "内存里的草稿");
  await expect(
    page.getByRole("alert").filter({ hasText: "本标签页草稿存储不可用" }),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第二集" })
    .click();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第一集" })
    .click();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "内存里的草稿",
  );
  const warning = page.waitForEvent("dialog");
  await page.evaluate(() => {
    setTimeout(() => window.location.reload(), 0);
  });
  await (await warning).dismiss();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "内存里的草稿",
  );
});

test("TXT-06：损坏存储不崩溃，不读取其他账号草稿", async ({ page }) => {
  const { episode, storageKey } = await setup(page);
  await page.evaluate(
    ({ key, episodeId }) => {
      sessionStorage.setItem(key, "broken-json");
      sessionStorage.setItem(
        "arclight:drafts:v1:another-user",
        JSON.stringify({
          other: { episodeId, label: "其他账号机密", value: "绝不能显示" },
        }),
      );
    },
    { key: storageKey, episodeId: episode.id },
  );
  await page.reload();
  await openWorkspacePanels(page);
  await expect(page.getByRole("alert")).toContainText("本标签页草稿存储不可用");
  await expect(page.getByLabel("故事灵感", { exact: true })).toHaveValue("");
  await expect(page.locator(".episode-drafts")).toHaveCount(0);
  await page.evaluate(
    ({ key, episodeId }) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({
          [JSON.stringify([episodeId, "script", null, null])]: {
            episodeId,
            label: "损坏的人工草稿",
            value: { scenes: [null] },
          },
        }),
      );
    },
    { key: storageKey, episodeId: episode.id },
  );
  await page.reload();
  await openWorkspacePanels(page);
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("本地草稿格式损坏");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "放弃草稿", exact: true }).click();
  await page.getByRole("button", { name: "确认放弃草稿", exact: true }).click();
  expect(await drafts(page, storageKey)).toEqual({});
});

test("TXT-06：迟到的保存回执不能清除切回后继续输入的新草稿", async ({
  page,
}) => {
  const { prefix, storageKey } = await setup(page);
  await newScript(page, "已提交的旧输入");
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const entered = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route("**" + prefix + "/scripts", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    started();
    await wait;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await entered;
  await expect(page.getByLabel("剧本标题", { exact: true })).toBeDisabled();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第二集" })
    .click();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第一集" })
    .click();
  await list(page);
  await page.locator(".version-row").filter({ hasText: "第一集" }).click();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "已提交的旧输入",
  );
  await page
    .getByLabel("剧本标题", { exact: true })
    .fill("切回后继续写的新草稿");
  const completed = page.waitForResponse(
    (response) =>
      response.url().endsWith(prefix + "/scripts") &&
      response.request().method() === "POST",
  );
  release();
  await completed;
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "切回后继续写的新草稿",
  );
  expect(JSON.stringify(await drafts(page, storageKey))).toContain(
    "切回后继续写的新草稿",
  );
  page.on("dialog", (dialog) => dialog.accept());
  await page.reload();
  await openWorkspacePanels(page);
  await list(page);
  await page.locator(".version-row").filter({ hasText: "第一集" }).click();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "切回后继续写的新草稿",
  );
});

test("TXT-06：归档后恢复历史版本草稿不会绕过只读限制", async ({ page }) => {
  const { script, storageKey } = await setup(page);
  await script(0, "归档前的原稿");
  await page.reload();
  await openWorkspacePanels(page);
  await list(page);
  await select(page, "归档前的原稿");
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("归档前暂存的修改");
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "归档项目", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "恢复项目", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "第一集" })
    .click();
  await list(page);
  await select(page, "归档前的原稿");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "归档前暂存的修改",
  );
  await expect(page.getByLabel("剧本标题", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled();
  expect(Object.keys(await drafts(page, storageKey))).toHaveLength(1);
  await page.screenshot({
    path: "test-results/draft-protection-desktop.png",
    fullPage: true,
  });
});

test("TXT-06：同标签页退出再登录可恢复，换账号不展示前一账号的草稿", async ({
  page,
}) => {
  const { user, storageKey } = await setup(page);
  await newScript(page, "原账号的临时内容");
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "登录创作手帐", exact: true }),
  ).toBeVisible();
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "another-draft-test",
      display_name: "另一个账号",
    },
  });
  expect(registered.status()).toBe(201);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "退出登录", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".episode-drafts")).toHaveCount(0);
  await expect(page.getByText("原账号的临时内容", { exact: true })).toHaveCount(
    0,
  );
  expect(Object.keys(await drafts(page, storageKey))).toHaveLength(1);
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await page.getByLabel("邮箱", { exact: true }).fill(user.email);
  await page.getByLabel("密码", { exact: true }).fill("draft-protection-test");
  await page.getByRole("button", { name: "登录创作手帐", exact: true }).click();
  await expect(page.locator(".workspace-toolbar")).toBeVisible();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "原账号的临时内容",
  );
});
