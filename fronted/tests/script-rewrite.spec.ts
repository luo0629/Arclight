import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `rewrite-${crypto.randomUUID()}@example.com`,
      password: "rewrite-test-password",
      display_name: "改写验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "精确局部改写" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "雨夜" },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const response = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "改写来源稿",
        summary: "阿青寻找失主。",
        estimated_duration_ms: 30000,
        characters: ["阿青"],
        scenes: [1, 2, 3].map((i) => ({
          scene_no: i,
          title: `场景${i}`,
          action: "前缀🙂改写目标后缀",
          dialogue: [
            { speaker: "阿青", text: "我想把这封信还给你。" },
            { speaker: "旁白", text: "雨仍在下。" },
          ],
        })),
      },
    },
  });
  expect(response.status()).toBe(201);
  const source = await response.json();
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await open(page);
  return { headers, prefix, source, project, episode, owner };
}

async function open(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "改写来源稿" })
    .filter({ hasText: "版本 1" })
    .click();
  await page
    .getByRole("button", { name: "局部改写此版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "局部改写 · 剧本 v1", exact: true }),
  ).toBeVisible();
}

async function choose(page: Page, label: string, option: string) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}

async function result(page: Page, env: Awaited<ReturnType<typeof setup>>) {
  await page.getByRole("button", { name: "提交局部改写", exact: true }).click();
  await expect(
    page.getByText("改写任务已提交，结果将保存为独立待审核版本。", {
      exact: true,
    }),
  ).toBeVisible();
  let rows: (typeof env.source)[] = [];
  await expect(async () => {
    rows = await (
      await page.request.get(env.prefix + "/scripts", { headers: env.headers })
    ).json();
    expect(rows).toHaveLength(2);
  }).toPass();
  return rows[0];
}

test("rewrites one exact scene, preserves original and independently reviews without a director", async ({
  page,
}) => {
  const env = await setup(page);
  await choose(page, "改写场景", "2 · 场景2");
  await page.getByLabel("语气要求", { exact: true }).fill("克制、悬疑");
  await page.getByLabel("单条对白字数上限（可选）", { exact: true }).fill("5");
  await page
    .getByLabel("旁白字数占比上限（%，可选）", { exact: true })
    .fill("0");
  await page.screenshot({
    path: "test-results/script-rewrite-desktop.png",
    fullPage: true,
  });
  const rewritten = await result(page, env);
  expect(rewritten.content_json.scenes[0]).toEqual(
    env.source.content_json.scenes[0],
  );
  expect(rewritten.content_json.scenes[2]).toEqual(
    env.source.content_json.scenes[2],
  );
  expect(rewritten.content_json.scenes[1].dialogue).toHaveLength(1);
  expect(
    rewritten.content_json.scenes[1].dialogue[0].text.length,
  ).toBeLessThanOrEqual(5);
  await page
    .getByRole("button", { name: "前往任务与审核", exact: true })
    .click();
  await expect(page.getByText("剧本局部改写", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "批准并继续", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(async () => {
    const runs = await (
      await page.request.get(env.prefix + "/runs", { headers: env.headers })
    ).json();
    expect(runs[0].status).toBe("SUCCEEDED");
  }).toPass();
  expect(
    await (
      await page.request.get(env.prefix + "/storyboards", {
        headers: env.headers,
      })
    ).json(),
  ).toEqual([]);
});

test("chapter scope edits consecutive scenes and records exact lineage", async ({
  page,
}) => {
  const env = await setup(page);
  await choose(page, "改写范围", "章节（连续场景）");
  await choose(page, "结束场景", "2 · 场景2");
  await page.getByLabel("改写指令", { exact: true }).fill("强化冲突");
  const rewritten = await result(page, env);
  expect(rewritten.content_json.scenes[0].action).toContain("演示改写");
  expect(rewritten.content_json.scenes[1].action).toContain("演示改写");
  expect(rewritten.content_json.scenes[2]).toEqual(
    env.source.content_json.scenes[2],
  );
  const history = await (
    await page.request.get(env.prefix + `/scripts/${rewritten.id}/comparison`, {
      headers: env.headers,
    })
  ).json();
  expect(history.target.base_version.id).toBe(env.source.id);
  expect(history.target.rewrite.target.scene_ids).toEqual(
    env.source.content_json.scenes
      .slice(0, 2)
      .map((row: { scene_id: string }) => row.scene_id),
  );
});

test("Unicode paragraph selection preserves prefix, suffix and every other field at 390px", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await setup(page);
  await choose(page, "改写范围", "选定段落");
  await expect(
    page.getByRole("combobox", { name: "改写范围", exact: true }),
  ).toBeFocused();
  const original = page.getByLabel("原文（可选中要改写的段落）", {
    exact: true,
  });
  await original.click();
  await expect(original).toBeFocused();
  await original.evaluate((input: HTMLTextAreaElement) => {
    input.setSelectionRange(4, 8); // UTF-16 offsets include the emoji surrogate pair.
    document.dispatchEvent(new Event("selectionchange"));
  });
  expect(
    await original.evaluate((input: HTMLTextAreaElement) => [
      input.selectionStart,
      input.selectionEnd,
    ]),
  ).toEqual([4, 8]);
  await expect(page.getByLabel("选定改写原文", { exact: true })).toHaveText(
    "改写目标",
  );
  await page
    .getByLabel("改写指令", { exact: true })
    .fill("更加紧张<script>window.rewriteExecuted=true</script>");
  await page.screenshot({
    path: "test-results/script-rewrite-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const rewritten = await result(page, env);
  expect(rewritten.content_json.scenes[0].action).toBe(
    "前缀🙂演示改写（Mock）：改写目标后缀",
  );
  expect(rewritten.content_json.scenes[0].dialogue).toEqual(
    env.source.content_json.scenes[0].dialogue,
  );
  expect(rewritten.content_json.scenes.slice(1)).toEqual(
    env.source.content_json.scenes.slice(1),
  );
  expect(
    await page.evaluate(
      () => (window as unknown as Record<string, unknown>).rewriteExecuted,
    ),
  ).toBeUndefined();
});

test("lost POST receipt retries the same key without duplicate versions and retains failed input", async ({
  page,
}) => {
  const env = await setup(page);
  await page.getByLabel("改写指令", { exact: true }).fill("保留人物动机");
  const keys: string[] = [];
  let first = true;
  await page.route(
    "**" + env.prefix + "/script-rewrite-runs",
    async (route) => {
      keys.push(route.request().headers()["idempotency-key"]);
      if (first) {
        first = false;
        const response = await route.fetch();
        expect(response.status()).toBe(202);
        return route.abort("connectionreset");
      }
      return route.continue();
    },
  );
  await page.getByRole("button", { name: "提交局部改写", exact: true }).click();
  await expect(page.getByLabel("改写指令", { exact: true })).toHaveValue(
    "保留人物动机",
  );
  await expect(
    page.getByRole("button", { name: "提交局部改写", exact: true }),
  ).toBeEnabled();
  await result(page, env);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (
      await page.request.get(env.prefix + "/runs", { headers: env.headers })
    ).json(),
  ).toHaveLength(1);
  const stored = await page.evaluate(
    (id) =>
      JSON.parse(localStorage.getItem(`arclight:drafts:v1:${id}`) ?? "{}"),
    env.owner.user.id,
  );
  expect(
    Object.keys(stored).filter((key) => key.includes('"rewrite"')),
  ).toHaveLength(0);
});

test("draft survives refresh and a successful POST with failed refresh only retries reads", async ({
  page,
}) => {
  const env = await setup(page);
  await page.getByLabel("改写指令", { exact: true }).fill("恢复这条草稿");
  await page.getByRole("button", { name: "关闭改写", exact: true }).click();
  await page.reload();
  await open(page);
  await expect(page.getByLabel("改写指令", { exact: true })).toHaveValue(
    "恢复这条草稿",
  );
  await expect(
    page.getByText("已恢复此精确版本的本机改写草稿。", { exact: true }),
  ).toBeVisible();
  let accepted = false;
  let posts = 0;
  await page.route(
    "**" + env.prefix + "/script-rewrite-runs",
    async (route) => {
      posts++;
      const response = await route.fetch();
      accepted = true;
      return route.fulfill({ response });
    },
  );
  await page.route("**" + env.prefix + "/canvas", (route) =>
    accepted
      ? route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ error: { message: "刷新暂不可用" } }),
        })
      : route.continue(),
  );
  await result(page, env);
  await expect(
    page.getByRole("button", { name: "重试刷新工作区", exact: true }),
  ).toBeVisible();
  await page.unroute("**" + env.prefix + "/canvas");
  await page
    .getByRole("button", { name: "重试刷新工作区", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "重试刷新工作区", exact: true }),
  ).toBeHidden();
  expect(posts).toBe(1);
});
