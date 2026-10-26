import { expect, test, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "duration-test-password",
      display_name: "时长验收",
    },
  });
  expect(registered.status()).toBe(201);
  const headers = {
    Authorization: "Bearer " + (await registered.json()).access_token,
  };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "时长合同" },
    })
  ).json();
  const episode = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 1, title: "时长验收", target_duration_ms: 30000 },
    })
  ).json();
  const prefix = "/api/v1/episodes/" + episode.id;
  return { headers, prefix };
}

test("TXT-10：剧本估算时长越界保留草稿，修正后另存并展示估算时长", async ({
  page,
}) => {
  const env = await setup(page);
  await page.goto("/");
  await expect(
    page.getByRole("radio", { name: "创作画布", exact: true }),
  ).toBeVisible();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("三十秒的故事");
  await page.getByLabel("梗概", { exact: true }).fill("主角找到一封信。");
  await page.getByLabel("场景 1", { exact: true }).fill("街角");
  await page.getByLabel("场景动作", { exact: true }).fill("主角打开信封。");
  const estimate = page.getByLabel("剧本估算时长（秒）", { exact: true });
  await expect(estimate).toHaveValue("30");
  await estimate.fill("33.001");
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "剧本估算时长 33001ms 不在目标 30000ms 的允许范围 [27000, 33000]ms 内",
  );
  await expect(estimate).toHaveValue("33.001");
  expect(
    await (
      await page.request.get(env.prefix + "/scripts", { headers: env.headers })
    ).json(),
  ).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await estimate.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/content-duration-script-mobile.png",
    fullPage: true,
  });
  await estimate.fill("33");
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("剧本 v1");
  await expect(
    page.getByText("剧本估算时长：33 秒", { exact: false }),
  ).toBeVisible();
  const versions = await (
    await page.request.get(env.prefix + "/scripts", { headers: env.headers })
  ).json();
  expect(versions).toHaveLength(1);
  expect(versions[0].content_json.estimated_duration_ms).toBe(33000);
});

test("TXT-10：镜头总时长由服务端求和，超限保存失败后可修正重试", async ({
  page,
}) => {
  const env = await setup(page);
  const created = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "时长来源",
        summary: "街角来信",
        estimated_duration_ms: 30000,
        characters: ["主角"],
        scenes: [
          { scene_no: 1, title: "街角", action: "主角驻足", dialogue: [] },
        ],
      },
    },
  });
  expect(created.status()).toBe(201);
  const script = await created.json();
  expect(
    (
      await page.request.post(
        env.prefix + "/scripts/" + script.id + "/review",
        { headers: env.headers, data: { decision: "APPROVE" } },
      )
    ).status(),
  ).toBe(200);
  await page.goto("/");
  await expect(
    page.getByRole("radio", { name: "创作画布", exact: true }),
  ).toBeVisible();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page
    .getByRole("option", { name: "v1 · 时长来源", exact: true })
    .click();
  await page.getByRole("button", { name: "使用此剧本起稿" }).click();
  await page.getByLabel("画面描述", { exact: true }).fill("主角在街角打开信封");
  const duration = page.getByLabel("时长（秒）", { exact: true });
  await duration.fill("26.999");
  await expect(
    page.getByText("镜头总时长：26.999 秒", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "镜头总时长 26999ms 不在目标 30000ms 的允许范围 [27000, 33000]ms 内",
  );
  await expect(page.getByLabel("画面描述", { exact: true })).toHaveValue(
    "主角在街角打开信封",
  );
  expect(
    await (
      await page.request.get(env.prefix + "/storyboards", {
        headers: env.headers,
      })
    ).json(),
  ).toEqual([]);
  await duration.fill("27");
  await duration.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/content-duration-board-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("分镜 v1");
  const boards = await (
    await page.request.get(env.prefix + "/storyboards", {
      headers: env.headers,
    })
  ).json();
  expect(boards).toHaveLength(1);
  expect(boards[0].content_json.shots[0].duration_ms).toBe(27000);
});
