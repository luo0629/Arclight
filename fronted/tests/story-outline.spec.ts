import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "outline-" + crypto.randomUUID() + "@example.com",
      password: "outline-test-password",
      display_name: "分集策划验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: {
        name: "分集大纲验收",
        story_settings: {
          target_episode_count: 2,
          default_episode_duration_ms: 30000,
        },
      },
    })
  ).json();
  const episode = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 1, title: "雨夜拾信" },
    })
  ).json();
  const path = "/api/v1/episodes/" + episode.id;
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openOutline(page);
  return { project, episode, path, headers };
}
async function openOutline(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "分集大纲", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "分集大纲", exact: true }),
  ).toBeVisible();
}
async function generate(
  page: Page,
  story = "雨夜，阿青捡到一封信，并决定寻找失主。",
) {
  await page.getByLabel("待规划故事", { exact: true }).fill(story);
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v1/ })).toBeVisible();
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByLabel("第1集目标", { exact: true })).toBeVisible();
}

test("generates project defaults, edits an independent version, confirms and derives exact script", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await generate(
    page,
    "雨夜的信件<script>window.outlineExecuted=true</script>",
  );
  await expect(page.getByLabel("第2集结尾钩子", { exact: true })).toBeVisible();
  await page.getByText("查看大纲来源与原文快照", { exact: true }).click();
  await expect(page.locator("pre")).toContainText("<script>");
  expect(
    await page.evaluate(
      () => (window as unknown as Record<string, unknown>).outlineExecuted,
    ),
  ).toBeUndefined();
  expect(
    await (await page.request.get(path + "/scripts", { headers })).json(),
  ).toEqual([]);
  const original = (
    await (await page.request.get(path + "/story-outlines", { headers })).json()
  )[0];
  await page
    .getByLabel("第1集目标", { exact: true })
    .fill("先寻找失主，再揭开雨夜的秘密");
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v2", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "确认此大纲版本", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "从此大纲编写第1集剧本", exact: true }),
  ).toBeEnabled();
  await page.screenshot({
    path: "test-results/story-outline-desktop.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "从此大纲编写第1集剧本", exact: true })
    .click();
  await expect(
    page.getByText("已从大纲 v2 的第1集启动编剧，请到任务与审核查看。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(async () => {
    const scripts = await (
      await page.request.get(path + "/scripts", { headers })
    ).json();
    expect(scripts).toHaveLength(1);
    expect(scripts[0].content_json.summary).toBe(
      "先寻找失主，再揭开雨夜的秘密",
    );
    const history = await (
      await page.request.get(
        path + "/scripts/" + scripts[0].id + "/comparison",
        { headers },
      )
    ).json();
    expect(history.target.outline_version.version_no).toBe(2);
  }).toPass();
  const old = await (
    await page.request.get(path + "/story-outlines/" + original.id, { headers })
  ).json();
  expect(old.status).toBe("DRAFT");
  expect(old.content.episodes[0].goal).toContain("演示目标");
});

test("uses an exact imported source and exact extraction version", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  const raw = "# 原文\r\n阿青归还信件。";
  const source = await (
    await page.request.post(path + "/story-sources", {
      headers,
      data: {
        import_key: crypto.randomUUID(),
        original_name: "信件.md",
        media_type: "text/markdown",
        encoding: "utf-8",
        content_base64: Buffer.from(raw).toString("base64"),
        source_note: "本人原创",
        rights_note: "本人所有",
        rights_confirmed: true,
      },
    })
  ).json();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await openOutline(page);
  await page
    .getByRole("combobox", { name: "大纲输入方式", exact: true })
    .click();
  await page.getByRole("option", { name: "已导入来源", exact: true }).click();
  await page
    .getByRole("combobox", { name: "大纲故事来源", exact: true })
    .click();
  await page
    .getByRole("option", { name: "来源 1：信件.md", exact: true })
    .click();
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v1/ })).toBeVisible();
  const analysis = await (
    await page.request.post(path + "/story-analysis-runs", {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: { source_id: source.id },
    })
  ).json();
  await expect(async () =>
    expect(
      (
        await (
          await page.request.get("/api/v1/runs/" + analysis.run_id, { headers })
        ).json()
      ).status,
    ).toBe("SUCCEEDED"),
  ).toPass();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await openOutline(page);
  await page
    .getByRole("combobox", { name: "大纲输入方式", exact: true })
    .click();
  await page.getByRole("option", { name: "精确提炼版本", exact: true }).click();
  await page
    .getByRole("combobox", { name: "大纲提炼版本", exact: true })
    .click();
  await page
    .getByRole("option", { name: "提炼 v1：信件.md", exact: true })
    .click();
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v2/ })).toBeVisible();
  await page.getByRole("button", { name: /^大纲 v2/ }).click();
  await page.getByText("查看大纲来源与原文快照", { exact: true }).click();
  await expect(page.locator("pre")).toHaveText(raw);
  const rows = await (
    await page.request.get(path + "/story-outlines", { headers })
  ).json();
  const detail = await (
    await page.request.get(path + "/story-outlines/" + rows[0].id, { headers })
  ).json();
  expect(detail.source_snapshot.source_id).toBe(source.id);
  expect(detail.source_snapshot.source_sha256).toBe(source.sha256);
  expect(detail.source_snapshot.analysis_version_no).toBe(1);
});

test("lost generation receipt retries the same key and shows the committed result", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  let once = true;
  const keys: string[] = [];
  await page.route("**/story-outline-runs", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    if (once) {
      once = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page.getByLabel("待规划故事", { exact: true }).fill("丢回执的大纲");
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(async () =>
    expect(
      (await (await page.request.get(path + "/runs", { headers })).json())[0]
        .status,
    ).toBe("SUCCEEDED"),
  ).toPass();
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v1/ })).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (
      await page.request.get(path + "/story-outlines", { headers })
    ).json(),
  ).toHaveLength(1);
});

test("recovers a version-specific draft and retries a lost save receipt without duplicate versions", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await generate(page);
  await page
    .getByLabel("第1集转折", { exact: true })
    .fill("本机草稿中的新转折");
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await openOutline(page);
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByLabel("第1集转折", { exact: true })).toHaveValue(
    "本机草稿中的新转折",
  );
  const keys: string[] = [];
  let once = true;
  await page.route("**/story-outlines", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    if (once) {
      once = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("第1集转折", { exact: true })).toHaveValue(
    "本机草稿中的新转折",
  );
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v2", exact: true }),
  ).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (
      await page.request.get(path + "/story-outlines", { headers })
    ).json(),
  ).toHaveLength(2);
});

test("late detail cannot replace a newer selected outline and saved receipt survives list failure", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await generate(page);
  await page.getByLabel("第1集目标", { exact: true }).fill("第二版目标");
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v2", exact: true }),
  ).toBeVisible();
  const rows = await (
    await page.request.get(path + "/story-outlines", { headers })
  ).json();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/story-outlines/" + rows[1].id, async (route) => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByText("正在读取大纲…", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^大纲 v2/ }).click();
  await expect(page.getByLabel("第1集目标", { exact: true })).toHaveValue(
    "第二版目标",
  );
  release();
  await expect(
    page.getByRole("heading", { name: "大纲 v2", exact: true }),
  ).toBeVisible();
  await page.route("**/story-outlines?*", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: "UNAVAILABLE", message: "目录暂不可用" } },
    }),
  );
  await page.getByLabel("第1集目标", { exact: true }).fill("第三版目标");
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v3", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("操作已保存；目录刷新失败，请重试刷新，无需再次提交。", {
      exact: true,
    }),
  ).toBeVisible();
});

test("archived outline stays readable and mobile layout fits the viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { project, headers } = await setup(page);
  await generate(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/story-outline-mobile.png",
    fullPage: true,
  });
  expect(
    (
      await page.request.patch("/api/v1/projects/" + project.id, {
        headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openOutline(page);
  await expect(
    page.getByRole("button", { name: "生成分集大纲", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByLabel("第1集目标", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "确认此大纲版本", exact: true }),
  ).toBeDisabled();
});

test("malformed local outline content warns and loads the saved version", async ({
  page,
}) => {
  const { path, headers, episode } = await setup(page);
  await generate(page);
  const rows = await (
    await page.request.get(path + "/story-outlines", { headers })
  ).json();
  await page.getByLabel("第1集目标", { exact: true }).fill("未保存的草稿目标");
  await page.evaluate(
    ({ episodeId, versionId }) => {
      const key = Object.keys(localStorage).find((item) =>
        item.startsWith("arclight:drafts:v1:"),
      )!;
      const drafts = JSON.parse(localStorage.getItem(key)!);
      drafts[
        JSON.stringify([episodeId, "outline", versionId])
      ].value.content.episodes = [null];
      localStorage.setItem(key, JSON.stringify(drafts));
    },
    { episodeId: episode.id, versionId: rows[0].id },
  );
  page.once("dialog", (dialog) => void dialog.accept());
  await page.reload();
  await openOutline(page);
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByRole("alert")).toContainText("本机草稿已损坏");
  await expect(page.getByLabel("第1集目标", { exact: true })).not.toHaveValue(
    "未保存的草稿目标",
  );
  await page
    .getByLabel("第1集目标", { exact: true })
    .fill("重新编辑的有效目标");
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v2", exact: true }),
  ).toBeVisible();
});
