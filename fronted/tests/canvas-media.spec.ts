import { writeFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { canvasMediaFixture } from "./canvas-media-helpers";
import { deferred } from "./render-helpers";
import { openDirectory, closeDirectory } from "./workspace-helpers";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("ui01-result.json"),
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

async function observeURLs(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL;
    (window as unknown as { revoked: string[] }).revoked = [];
    URL.revokeObjectURL = (url) => {
      (window as unknown as { revoked: string[] }).revoked.push(url);
      revoke(url);
    };
  });
}

test("UI-01：候选原图、正式选用和历史失效状态来自同一精确图", async ({
  page,
}) => {
  const env = await canvasMediaFixture(page);
  const panel = await env.openCanvas(env.image);
  await expect(panel).toContainText("候选版本");
  const image = panel.getByRole("img", {
    name: "精确资产版本原图",
    exact: true,
  });
  await expect(image).toBeVisible();
  expect(
    await image.evaluate((img: HTMLImageElement) => img.naturalWidth),
  ).toBeGreaterThan(1);
  await expect(panel).toContainText(env.image.checksum!);
  await env.selectNode(env.history);
  await expect(panel).toContainText("红色箱子 · 历史选用");
  await expect(panel).toContainText("正式选用");
  await expect(panel).toContainText("需更新");
  await expect(panel).toContainText(env.history.resource_id!);
  expect(env.mediaReads).toContain("/api/v1" + env.history.preview_path);
});

test("UI-01：精确依赖可跳转五轨时间轴且不产生业务写入", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  const panel = await env.openCanvas(env.image);
  const dependencies = page.getByRole("region", {
    name: "精确版本依赖",
    exact: true,
  });
  await expect(dependencies).toContainText(env.original.dependency_manifest_id);
  await dependencies
    .getByRole("button", { name: /雨后街角 · 五轨剪辑/ })
    .click();
  const media = panel.getByRole("region", {
    name: "画布媒体预览",
    exact: true,
  });
  for (const track of ["画面", "配音", "音乐", "音效", "字幕"])
    await expect(media).toContainText(track);
  await expect(panel).toContainText(env.original.id);
  await expect(dependencies).toContainText(env.original.dependency_manifest_id);
  expect(env.mutations).toHaveLength(0);
  expect(env.layoutWrites).toHaveLength(0);
});

test("UI-01：列表筛选搜索分页与键盘选择覆盖全部媒体节点", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  const extra = Array.from({ length: 34 }, (_, i) => {
    const resource_id = crypto.randomUUID();
    return {
      ...env.missing,
      id: "asset-version:" + resource_id,
      resource_id,
      title: `附加声音 ${i + 1}`,
    };
  });
  env.graph.nodes.push(...extra);
  await env.openCanvas();
  const list = page.getByRole("region", {
    name: "内容与媒体列表",
    exact: true,
  });
  await list.getByLabel("内容类型", { exact: true }).selectOption("audio");
  await expect(list).toContainText("共 37 项");
  await expect(list.getByRole("button", { name: /原句配音/ })).toBeVisible();
  await list.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(list).toContainText("第 2 / 2 页");
  await list
    .getByLabel("搜索名称或版本 ID", { exact: true })
    .fill(extra[33].resource_id!);
  const row = list.getByRole("button", { name: /附加声音 34/ });
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("region", { name: "画布资产详情", exact: true }),
  ).toContainText(extra[33].resource_id!);
  await expect(list).toContainText("共 1 项");
  await list
    .getByLabel("搜索名称或版本 ID", { exact: true })
    .fill("不存在的素材");
  await expect(list).toContainText("没有匹配的内容");
});

for (const kind of ["voice", "music"] as const)
  test(`UI-01：${kind}真实WAV播放及切换回收私有URL`, async ({ page }) => {
    const env = await canvasMediaFixture(page);
    await observeURLs(page);
    const panel = await env.openCanvas(env[kind]);
    await panel
      .getByRole("button", { name: "读取画布试听", exact: true })
      .click();
    const audio = panel.getByLabel("画布音频播放器", { exact: true });
    await expect(audio).toBeVisible();
    await audio.evaluate(async (element: HTMLAudioElement) => {
      element.muted = true;
      await element.play();
    });
    await expect
      .poll(() =>
        audio.evaluate((element: HTMLAudioElement) => element.currentTime),
      )
      .toBeGreaterThan(0.15);
    const url = await audio.getAttribute("src");
    await env.selectNode(env.missing);
    await expect(panel.locator("audio")).toHaveCount(0);
    await expect
      .poll(() =>
        page.evaluate(
          () => (window as unknown as { revoked: string[] }).revoked,
        ),
      )
      .toContain(url);
  });

for (const field of [
  "id",
  "project_id",
  "asset_version_id",
  "checksum",
] as const)
  test(`UI-01：音频${field}不匹配时阻止读取字节`, async ({ page }) => {
    const env = await canvasMediaFixture(page);
    env.controlMedia.metadata = {
      [field]: field === "checksum" ? "0".repeat(64) : crypto.randomUUID(),
    };
    const panel = await env.openCanvas(env.voice);
    await panel
      .getByRole("button", { name: "读取画布试听", exact: true })
      .click();
    await expect(panel).toContainText("音频文件来源或格式不匹配");
    await expect(panel.locator("audio")).toHaveCount(0);
    expect(env.mediaReads.some((path) => path.endsWith("/content"))).toBe(
      false,
    );
  });

test("UI-01：实际音频字节损坏拒绝播放", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  env.controlMedia.corrupt = true;
  const panel = await env.openCanvas(env.voice);
  await panel
    .getByRole("button", { name: "读取画布试听", exact: true })
    .click();
  await expect(panel).toContainText("音频完整性检查未通过");
  await expect(panel.locator("audio")).toHaveCount(0);
});

test("UI-01：音频迟到响应不能覆盖新选中的时间轴", async ({ page }) => {
  const env = await canvasMediaFixture(page),
    wait = deferred();
  env.controlMedia.audioGate = wait.promise;
  const panel = await env.openCanvas(env.voice);
  await panel
    .getByRole("button", { name: "读取画布试听", exact: true })
    .click();
  await expect.poll(() => env.mediaReads.length).toBeGreaterThan(0);
  await env.selectNode(env.timeline);
  wait.release();
  await expect(panel).toContainText("时间轴 v1");
  await expect(panel.locator("audio")).toHaveCount(0);
});

test("UI-01：时间轴迟到响应不能覆盖成片详情", async ({ page }) => {
  const env = await canvasMediaFixture(page),
    wait = deferred();
  env.controlMedia.timelineGate = wait.promise;
  const panel = await env.openCanvas(env.timeline);
  await expect
    .poll(() => env.mediaReads.some((p) => p.includes(env.original.id)))
    .toBe(true);
  await env.selectNode(env.preview);
  wait.release();
  await expect(panel).toContainText("预览输出");
  await expect(
    panel.getByRole("link", { name: "打开此时间轴版本", exact: true }),
  ).toHaveCount(0);
});

test("UI-01：撤权清空整张图和正在播放的私有音频", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  await observeURLs(page);
  const panel = await env.openCanvas(env.voice);
  await panel
    .getByRole("button", { name: "读取画布试听", exact: true })
    .click();
  const audio = panel.getByLabel("画布音频播放器", { exact: true });
  await expect(audio).toBeVisible();
  const url = await audio.getAttribute("src");
  env.controlMedia.status = 403;
  await panel
    .getByRole("button", { name: "重新读取画布音频", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "当前画布内容不可访问", exact: true }),
  ).toBeVisible();
  await expect(page.locator("audio, video")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "内容与媒体列表", exact: true }),
  ).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() => (window as unknown as { revoked: string[] }).revoked),
    )
    .toContain(url);
});

for (const kind of ["delivery", "preview"] as const)
  test(`UI-01：${kind}实际MP4播放并准确显示交付状态`, async ({ page }) => {
    const env = await canvasMediaFixture(page);
    const panel = await env.openCanvas(env[kind]);
    await expect(panel).toContainText(
      kind === "delivery" ? "已正式交付" : "尚未正式交付",
    );
    await panel
      .getByRole("button", { name: "读取画布成片预览", exact: true })
      .click();
    const video = panel.locator("video");
    await expect(video).toBeVisible();
    await video.evaluate(async (element: HTMLVideoElement) => {
      element.muted = true;
      await element.play();
    });
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.currentTime),
      )
      .toBeGreaterThan(0.15);
    expect(env.contentReads.length).toBeGreaterThan(0);
  });

test("UI-01：时间轴匹配真实MP4可定位片段并打开同一历史编辑版本", async ({
  page,
}) => {
  const env = await canvasMediaFixture(page);
  env.timelines.unshift({
    ...structuredClone(env.original),
    id: crypto.randomUUID(),
    version_no: 2,
    content_sha256: "f".repeat(64),
  });
  const panel = await env.openCanvas(env.timeline);
  await panel
    .getByRole("button", { name: "读取时间轴成片预览", exact: true })
    .click();
  const video = panel.locator("video");
  await expect(video).toBeVisible();
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState))
    .toBeGreaterThan(0);
  await panel.getByRole("button", { name: /^1\.5 秒 ·/ }).click();
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
    .toBeCloseTo(1.5, 1);
  await panel
    .getByRole("link", { name: "打开此时间轴版本", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp("timeline=" + env.original.id));
  const render = page.getByRole("region", { name: "渲染与成片", exact: true });
  await expect(render.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    env.original.id,
  );
});

for (const kind of ["timeline", "delivery"] as const)
  test(`UI-01：${kind}精确来源不一致时不显示媒体`, async ({ page }) => {
    const env = await canvasMediaFixture(page);
    env[kind].checksum = "0".repeat(64);
    const panel = await env.openCanvas(env[kind]);
    await expect(panel).toContainText("与所选精确版本不一致");
    await expect(
      panel.getByRole("button", { name: /读取.*成片预览/ }),
    ).toHaveCount(0);
    expect(env.contentReads).toHaveLength(0);
  });

test("UI-01：缺失媒体和跨项目图片路径明确提示且不读取", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  env.image.preview_path = env.image.preview_path!.replace(
    env.project.id,
    crypto.randomUUID(),
  );
  const panel = await env.openCanvas(env.image);
  await expect(panel).toContainText("图片来源与当前精确版本不匹配");
  expect(env.mediaReads).toHaveLength(0);
  await env.selectNode(env.missing);
  await expect(panel).toContainText("暂无可读取的已登记媒体");
});

test("UI-01：Viewer可使用列表媒体预览且画布没有布局写入口", async ({
  page,
}) => {
  const env = await canvasMediaFixture(page);
  env.control.role = "viewer";
  const panel = await env.openCanvas(env.preview, "canvas");
  await expect(
    page.getByText("当前为只读模式", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /保存画布位置|位置已保存/ }),
  ).toHaveCount(0);
  await panel
    .getByRole("button", { name: "读取画布成片预览", exact: true })
    .click();
  await expect(panel.locator("video")).toBeVisible();
  expect(env.layoutWrites).toHaveLength(0);
});

test("UI-01：切集取消旧图请求，迟到失败不覆盖新集媒体", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  await env.openCanvas(env.music);
  const wait = deferred();
  let requested = false;
  await page.route(
    `**/api/v1/episodes/${env.episodes[0].id}/canvas`,
    async (route) => {
      requested = true;
      await wait.promise;
      await route
        .fulfill({
          status: 403,
          json: { error: { code: "FORBIDDEN", message: "旧集迟到撤权" } },
        })
        .catch(() => {});
    },
  );
  await page.getByRole("button", { name: "刷新内容", exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await openDirectory(page);
  await page.getByRole("button", { name: /第二集的独立成片/ }).click();
  await closeDirectory(page);
  await expect(page).toHaveURL(new RegExp(env.episodes[1].id));
  wait.release();
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  const panel = await env.selectNode(env.music);
  await panel
    .getByRole("button", { name: "读取画布试听", exact: true })
    .click();
  await expect(
    panel.getByLabel("画布音频播放器", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "当前画布内容不可访问", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("旧集迟到撤权", { exact: true })).toHaveCount(0);
});

test("UI-01：媒体节点保存布局仍用原ID和CAS且不写业务依赖", async ({ page }) => {
  const env = await canvasMediaFixture(page);
  await env.openCanvas(undefined, "canvas");
  const node = page.locator(`.react-flow__node[data-id="${env.image.id}"]`);
  await expect(node).toBeVisible();
  const box = await node.boundingBox();
  expect(box).toBeTruthy();
  await page.mouse.move(box!.x + 30, box!.y + 20);
  await page.mouse.down();
  await page.mouse.move(box!.x + 65, box!.y + 55, { steps: 8 });
  await page.mouse.up();
  env.controlMedia.layoutConflict = true;
  await page.getByRole("button", { name: "保存画布位置", exact: true }).click();
  await expect(
    page.getByText("布局已更新，请刷新后重试。", { exact: true }),
  ).toBeVisible();
  expect(env.layoutWrites[0].body.expected_revision).toBe(1);
  expect(env.layoutWrites[0].body).not.toHaveProperty("edges");
  env.controlMedia.layoutConflict = false;
  await page.getByRole("button", { name: "保存画布位置", exact: true }).click();
  await expect(page.getByText("画布位置已保存", { exact: true })).toBeVisible();
  expect(
    Object.keys(env.layoutWrites[1].body.positions as object).sort(),
  ).toEqual(env.nodes.map((n) => n.id).sort());
  expect(env.mutations).toHaveLength(0);
});

for (const size of [
  { width: 1440, height: 1050 },
  { width: 390, height: 844 },
])
  test(`UI-01：${size.width}px真实图片和五轨预览无溢出`, async ({
    page,
  }, info) => {
    await page.setViewportSize(size);
    const env = await canvasMediaFixture(page);
    const panel = await env.openCanvas(
      env.image,
      size.width > 500 ? "canvas" : "list",
    );
    if (size.width > 500) {
      const fit = page.getByRole("button", { name: "适应画布", exact: true });
      const nodes = page.locator(".react-flow__node");
      const tile = (id: string) =>
        page.locator(`.react-flow__node[data-id="${id}"]`);
      await fit.click();
      await expect(nodes).toHaveCount(env.graph.nodes.length);
      for (const node of env.graph.nodes)
        await expect(tile(node.id)).toContainText(node.title);
      await tile(env.timeline.id)
        .getByRole("button", {
          name: `查看时间轴：${env.timeline.title} v${env.timeline.version_no}`,
          exact: true,
        })
        .click();
      await expect(panel).toContainText(env.original.id);
      await tile(env.image.id)
        .getByRole("button", {
          name: `查看镜头候选图：${env.image.title} v${env.image.version_no}`,
          exact: true,
        })
        .click();
      await fit.click();
      await expect(nodes).toHaveCount(env.graph.nodes.length);
      const images = nodes.locator("img");
      await expect(images).toHaveCount(2);
      for (const image of await images.all())
        await expect
          .poll(() =>
            image.evaluate((img: HTMLImageElement) => img.naturalWidth),
          )
          .toBeGreaterThan(1);
      expect(env.mutations).toHaveLength(0);
      expect(env.layoutWrites).toHaveLength(0);
    }
    await expect(
      panel.getByRole("img", { name: "精确资产版本原图", exact: true }),
    ).toBeVisible();
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: info.outputPath(`ui01-image-${size.width}.png`),
      fullPage: false,
    });
    if (size.width > 500)
      await page.getByRole("radio", { name: "版本列表", exact: true }).click();
    await env.selectNode(env.timeline);
    await panel
      .getByRole("button", { name: "读取时间轴成片预览", exact: true })
      .click();
    const video = panel.locator("video");
    await expect(video).toBeVisible();
    await expect
      .poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState))
      .toBeGreaterThan(0);
    await video.evaluate((v: HTMLVideoElement) => {
      v.currentTime = 1;
    });
    await expect
      .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
      .toBeCloseTo(1, 1);
    await video.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: info.outputPath(`ui01-timeline-${size.width}.png`),
      fullPage: false,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  });
