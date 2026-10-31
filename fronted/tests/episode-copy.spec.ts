import { test, expect, type Page } from "@playwright/test";
import {
  openWorkspacePanels,
  openDirectory,
  closeDirectory,
} from "./workspace-helpers";

async function setup(page: Page) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "copy-" + crypto.randomUUID() + "@example.com",
      password: "episode-copy-test",
      display_name: "剧集复制验收",
    },
  });
  expect(registration.status()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const projectResult = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "复制剧集验收" },
  });
  expect(projectResult.status()).toBe(201);
  const project = await projectResult.json();
  const episodeResult = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: {
        episode_no: 1,
        title: "雨夜初遇",
        target_duration_ms: 31125,
      },
    },
  );
  expect(episodeResult.status()).toBe(201);
  const episode = await episodeResult.json();
  const path = "/api/v1/episodes/" + episode.id;
  const scriptResult = await page.request.post(path + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "雨夜初遇",
        summary: "主角发现旧信",
        characters: ["主角"],
        estimated_duration_ms: 31125,
        scenes: [
          {
            scene_no: 1,
            title: "街角",
            action: "主角驻足",
            dialogue: [{ speaker: "主角", text: "我回来了。" }],
          },
        ],
      },
    },
  });
  expect(scriptResult.status()).toBe(201);
  const script = await scriptResult.json();
  expect(
    (
      await page.request.post(path + `/scripts/${script.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  const boardResult = await page.request.post(path + "/storyboards", {
    headers,
    data: {
      expected_version_no: 0,
      script_version_id: script.id,
      content: {
        shots: [
          {
            shot_no: 1,
            scene_no: 1,
            duration_ms: 16000,
            visual_description: "雨夜街角",
            characters: ["主角"],
          },
          {
            shot_no: 2,
            scene_no: 1,
            duration_ms: 15125,
            visual_description: "旧信特写",
            characters: ["主角"],
          },
        ],
      },
    },
  });
  expect(boardResult.status()).toBe(201);
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  return { headers, project, episode, path, script };
}

async function openCopy(page: Page) {
  await page
    .getByRole("button", { name: "复制为新集草稿", exact: true })
    .click();
  return page.getByRole("dialog", { name: "复制剧集为草稿", exact: true });
}

test("STORY-03：复制草稿持久化、独立版本、正确派生和重新审核", async ({
  page,
}, testInfo) => {
  const { headers, project, episode, script } = await setup(page);
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const dialog = await openCopy(page);
  await expect(dialog.getByLabel("新剧集集数", { exact: true })).toHaveValue(
    "2",
  );
  await dialog.getByLabel("新剧集标题", { exact: true }).fill("雨夜的回声");
  await page.screenshot({
    path: testInfo.outputPath("episode-copy-desktop.png"),
    fullPage: true,
  });
  const responsePromise = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/episodes/${episode.id}/copy`) &&
      r.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "确认复制", exact: true }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const copied = await response.json();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 雨夜的回声", exact: true }),
  ).toBeVisible();
  await openDirectory(page);
  await expect(
    page.getByRole("navigation", { name: "剧集列表" }),
  ).toContainText("雨夜的回声");
  await closeDirectory(page);
  expect(copied.status).toBe("DRAFT");
  expect(copied.target_duration_ms).toBe(31125);
  const newPath = "/api/v1/episodes/" + copied.id;
  const scripts = await (
    await page.request.get(newPath + "/scripts", { headers })
  ).json();
  const boards = await (
    await page.request.get(newPath + "/storyboards", { headers })
  ).json();
  expect(scripts).toHaveLength(1);
  expect(boards).toHaveLength(1);
  expect(scripts[0].run_id).toBeNull();
  expect(scripts[0].id).not.toBe(script.id);
  expect(scripts[0].status).toBe("READY_FOR_REVIEW");
  expect(boards[0].script_version_id).toBe(scripts[0].id);
  expect(boards[0].status).toBe("READY_FOR_REVIEW");
  expect(
    await (await page.request.get(newPath + "/runs", { headers })).json(),
  ).toEqual([]);
  expect(
    (
      await page.request.post(newPath + `/storyboards/${boards[0].id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(409);
  await page.reload();
  await openWorkspacePanels(page);
  await openDirectory(page);
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .filter({ hasText: "雨夜的回声" })
    .click();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 雨夜的回声", exact: true }),
  ).toBeVisible();
  const episodes = await (
    await page.request.get(`/api/v1/projects/${project.id}/episodes`, {
      headers,
    })
  ).json();
  expect(episodes).toHaveLength(2);
  expect(crashes).toEqual([]);
});

test("STORY-03：冲突及网络失败保留输入、禁止重复提交、取消键盘及窄屏", async ({
  page,
}, testInfo) => {
  const { headers, project, path } = await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  let dialog = await openCopy(page);
  await dialog.getByLabel("新剧集标题", { exact: true }).fill("保留的复制草稿");
  await dialog.getByLabel("新剧集集数", { exact: true }).fill("1");
  await dialog.getByRole("button", { name: "确认复制", exact: true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByLabel("新剧集标题", { exact: true })).toHaveValue(
    "保留的复制草稿",
  );
  await dialog.getByLabel("新剧集集数", { exact: true }).fill("2");
  await page.route("**" + path + "/copy", (route) => route.abort("failed"));
  await dialog.getByRole("button", { name: "确认复制", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("网络连接失败");
  await expect(dialog.getByLabel("新剧集标题", { exact: true })).toHaveValue(
    "保留的复制草稿",
  );
  await page.unroute("**" + path + "/copy");
  await page.screenshot({
    path: testInfo.outputPath("episode-copy-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "复制为新集草稿", exact: true }),
  ).toBeFocused();
  dialog = await openCopy(page);
  await dialog.getByLabel("新剧集标题", { exact: true }).fill("纯文本副本");
  await dialog.getByRole("combobox", { name: "资产复用", exact: true }).click();
  await page
    .getByRole("option", { name: "仅复制文本，移除资产引用", exact: true })
    .click();
  let requests = 0;
  await page.route("**" + path + "/copy", async (route) => {
    requests += 1;
    expect(route.request().postDataJSON().reuse_assets).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.continue();
  });
  await dialog
    .getByRole("button", { name: "确认复制", exact: true })
    .dblclick();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 纯文本副本", exact: true }),
  ).toBeVisible();
  expect(requests).toBe(1);
  expect(
    await (
      await page.request.get(`/api/v1/projects/${project.id}/episodes`, {
        headers,
      })
    ).json(),
  ).toHaveLength(2);
});

test("STORY-03：活动任务、归档和只读权限门禁", async ({ page }) => {
  const { headers, project, path } = await setup(page);
  const runResult = await page.request.post(path + "/script-runs", {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: { story: "新的故事" },
  });
  expect(runResult.status()).toBe(202);
  const run = await runResult.json();
  // SQLite cannot serialize cancellation against the Mock worker's in-flight write.
  await expect
    .poll(async () => {
      const result = await page.request.get(`/api/v1/runs/${run.run_id}`, {
        headers,
      });
      expect(result.status()).toBe(200);
      return (await result.json()).status;
    })
    .toBe("WAITING_REVIEW");
  const dialog = await openCopy(page);
  await dialog.getByRole("button", { name: "确认复制", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("请先结束或取消");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  expect(
    (
      await page.request.post(`/api/v1/runs/${run.run_id}/cancel`, { headers })
    ).status(),
  ).toBe(200);
  expect(
    (
      await page.request.patch(path, { headers, data: { archived: true } })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "复制为新集草稿", exact: true }),
  ).toBeDisabled();
  expect(
    (
      await page.request.patch(path, { headers, data: { archived: false } })
    ).status(),
  ).toBe(200);
  const memberResult = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "episode-copy-test",
      display_name: "只读成员",
    },
  });
  expect(memberResult.status()).toBe(201);
  const member = await memberResult.json();
  expect(
    (
      await page.request.post(`/api/v1/projects/${project.id}/members`, {
        headers,
        data: { email: member.user.email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "复制为新集草稿", exact: true }),
  ).toHaveCount(0);
});
