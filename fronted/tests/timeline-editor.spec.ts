import { readFile, writeFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import { deferred } from "./render-helpers";
import { timelineFixture } from "./timeline-helpers";
import type { TimelineSnapshot } from "../src/features/rendering/types";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("render05-result.json"),
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

test("RENDER-05：五轨显示精确来源，键盘选中片段与恢复本机草稿", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  const { editor } = await env.openEditor();
  for (const name of ["配音", "音乐", "音效", "字幕"])
    await expect(
      editor.getByLabel(`启用${name}轨道`, { exact: true }),
    ).toBeChecked();
  const shot = editor.getByRole("button", { name: /^画面片段 2，/ });
  await shot.focus();
  await page.keyboard.press("Enter");
  await editor.getByLabel("片段时长（毫秒）", { exact: true }).fill("1700");
  await editor.getByText("片段精确来源", { exact: true }).click();
  await expect(editor).toContainText(
    env.original.snapshot.tracks[0].clips[1].source_asset_version_id,
  );
  await page.reload();
  await page.getByText("时间轴编辑器（有本机草稿）", { exact: true }).click();
  await expect(
    page.getByRole("region", { name: "时间轴编辑器", exact: true }),
  ).toContainText("已恢复此精确时间轴版本的本机草稿");
  await page.getByRole("button", { name: /^画面片段 2，/ }).click();
  await expect(
    page.getByLabel("片段时长（毫秒）", { exact: true }),
  ).toHaveValue("1700");
});

test("RENDER-05：时长顺延画面，声音字幕时点保留并另存新版本", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  const original = structuredClone(env.original);
  const { panel, editor } = await env.openEditor();
  await editor.getByLabel("片段时长（毫秒）", { exact: true }).fill("1800");
  await expect(
    panel.getByRole("button", { name: "创建预览清单", exact: true }),
  ).toBeDisabled();
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  const snapshot = env.saves[0].body.snapshot as TimelineSnapshot;
  expect(snapshot.schema_version).toBe(2);
  expect(snapshot.duration_ms).toBe(3300);
  expect(snapshot.tracks[0].clips[1].start_ms).toBe(1800);
  expect(snapshot.tracks.slice(1)).toEqual(original.snapshot.tracks.slice(1));
  expect(env.original).toEqual(original);
  await expect(panel.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    env.timelines[0].id,
  );
});

test("RENDER-05：镜头移动裁切运镜和转场进入结构化保存合同", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  const { editor } = await env.openEditor();
  await editor.getByRole("button", { name: /^画面片段 2，/ }).click();
  const secondId = env.original.snapshot.tracks[0].clips[1].id;
  await editor.getByLabel("起始裁切宽度（%）").fill("80");
  await editor.getByLabel("起始裁切高度（%）").fill("80");
  await editor.getByLabel("轻量运镜", { exact: true }).selectOption("LINEAR");
  await editor.getByLabel("结束裁切左侧（%）").fill("5");
  await editor.getByLabel("进入转场", { exact: true }).selectOption("FADE");
  await editor.getByLabel("转场时长（毫秒）").fill("400");
  await editor
    .getByRole("button", { name: "向前移动镜头", exact: true })
    .click();
  await expect(editor.getByLabel("进入转场", { exact: true })).toHaveValue(
    "CUT",
  );
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  const video = (env.saves[0].body.snapshot as TimelineSnapshot).tracks[0];
  expect(video.clips[0]).toMatchObject({
    id: secondId,
    ordinal: 0,
    start_ms: 0,
    visual: {
      crop: { width: 8000, height: 8000 },
      motion: { kind: "LINEAR", end_crop: { x: 500 } },
      transition: { kind: "CUT", duration_ms: 0 },
    },
  });
});

test("RENDER-05：音频裁切保留自动化，时长冲突须显式处理", async ({ page }) => {
  const env = await timelineFixture(page);
  const { editor } = await env.openEditor();
  await editor.getByRole("button", { name: /^音乐片段 1，/ }).click();
  await editor.getByLabel("片段开始（毫秒）").fill("200");
  await editor.getByLabel("源音频入点（毫秒）").fill("100");
  await editor.getByLabel("片段时长（毫秒）").fill("500");
  await expect(editor.getByRole("alert")).toContainText("已有音量包络");
  await expect(
    editor.getByRole("button", { name: "保存时间轴新版本", exact: true }),
  ).toBeDisabled();
  await editor
    .getByRole("button", { name: "按新时长缩放自动化", exact: true })
    .click();
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  const music = (env.saves[0].body.snapshot as TimelineSnapshot).tracks.find(
    (t) => t.kind === "MUSIC",
  )!.clips[0];
  expect(music).toMatchObject({
    start_ms: 200,
    source_start_ms: 100,
    duration_ms: 500,
    audio: {
      gain_milli: 650,
      fade_in_ms: 50,
      fade_out_ms: 50,
      envelope: [
        { offset_ms: 0, gain_milli: 500 },
        { offset_ms: 500, gain_milli: 800 },
      ],
    },
  });
});

test("RENDER-05：四类轨道开关保存后重新载入仍关闭", async ({ page }) => {
  const env = await timelineFixture(page);
  const { editor } = await env.openEditor();
  for (const name of ["配音", "音乐", "音效", "字幕"])
    await editor.getByLabel(`启用${name}轨道`, { exact: true }).uncheck();
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  expect(
    (env.saves[0].body.snapshot as TimelineSnapshot).tracks
      .filter((t) => t.kind !== "VIDEO")
      .every((t) => t.enabled === false),
  ).toBe(true);
  await page.reload();
  await page.getByText("时间轴编辑器", { exact: true }).click();
  for (const name of ["配音", "音乐", "音效", "字幕"])
    await expect(
      page.getByLabel(`启用${name}轨道`, { exact: true }),
    ).not.toBeChecked();
});

test("RENDER-05：CAS 冲突保留草稿，刷新列表不覆盖输入", async ({ page }) => {
  const env = await timelineFixture(page);
  env.control.conflict = true;
  const { editor } = await env.openEditor();
  await editor.getByLabel("片段时长（毫秒）").fill("2100");
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect(editor.getByRole("alert")).toContainText("当前草稿已保留");
  await editor
    .getByRole("button", { name: "刷新时间轴版本列表", exact: true })
    .click();
  await expect(editor.getByLabel("片段时长（毫秒）")).toHaveValue("2100");
  await page.reload();
  await page.getByText("时间轴编辑器（有本机草稿）", { exact: true }).click();
  await expect(page.getByLabel("片段时长（毫秒）")).toHaveValue("2100");
});

test("RENDER-05：结果未知同键重试，重复点击不会并发保存", async ({ page }) => {
  const env = await timelineFixture(page);
  env.control.networkFailure = true;
  const { editor } = await env.openEditor();
  await editor.getByLabel("片段时长（毫秒）").fill("1900");
  const save = editor.getByRole("button", {
    name: "保存时间轴新版本",
    exact: true,
  });
  await save.click();
  await expect(editor.getByRole("alert")).toBeVisible();
  const gate = deferred();
  env.control.saveGate = gate.promise;
  await save.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  try {
    await expect.poll(() => env.saves.length).toBe(2);
    await expect(save).toBeDisabled();
  } finally {
    gate.release();
  }
  await expect
    .poll(
      () =>
        env.timelines.filter(
          (t) => t.snapshot.episode_id === env.episodes[0].id,
        ).length,
    )
    .toBe(2);
  expect(env.saves[1].body).toEqual(env.saves[0].body);
});

test("RENDER-05：保存中切换版本，迟到响应不覆盖新视图和旧草稿", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  const other = {
    ...structuredClone(env.original),
    id: crypto.randomUUID(),
    version_no: 2,
  };
  env.timelines.push(other);
  const { editor, panel } = await env.openEditor();
  await editor.getByLabel("片段时长（毫秒）").fill("2200");
  const gate = deferred();
  env.control.saveGate = gate.promise;
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  try {
    await panel
      .getByLabel("时间轴版本", { exact: true })
      .selectOption(other.id);
  } finally {
    gate.release();
  }
  await expect(panel.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    other.id,
  );
  await panel
    .getByLabel("时间轴版本", { exact: true })
    .selectOption(env.original.id);
  await panel.getByText("时间轴编辑器（有本机草稿）", { exact: true }).click();
  await expect(panel.getByLabel("片段时长（毫秒）")).toHaveValue("2200");
});

test("RENDER-05：只读用户可查看五轨而不能更改或保存", async ({ page }) => {
  const env = await timelineFixture(page);
  env.control.role = "viewer";
  const { editor } = await env.openEditor();
  await expect(
    editor.getByLabel("启用配音轨道", { exact: true }),
  ).toBeDisabled();
  await expect(editor.getByLabel("片段时长（毫秒）")).toBeDisabled();
  await expect(
    editor.getByRole("button", { name: "保存时间轴新版本", exact: true }),
  ).toHaveCount(0);
  expect(env.saves).toHaveLength(0);
});

test("RENDER-05：真实 MP4 可播放并按片段定位，未保存草稿明确区分", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  env.setMedia(
    await readFile(new URL("./fixtures/timeline-preview.mp4", import.meta.url)),
  );
  const { editor } = await env.openEditor();
  await editor
    .getByRole("button", { name: "读取时间轴成片预览", exact: true })
    .click();
  const video = editor.locator("video");
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState))
    .toBeGreaterThanOrEqual(2);
  await video.evaluate(async (v: HTMLVideoElement) => {
    await v.play();
  });
  await expect
    .poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime))
    .toBeGreaterThan(0);
  await video.evaluate((v: HTMLVideoElement) => v.pause());
  await editor.getByRole("button", { name: /^画面片段 2，/ }).click();
  await expect
    .poll(() =>
      video.evaluate((v: HTMLVideoElement) => Math.round(v.currentTime * 1000)),
    )
    .toBe(1500);
  await editor.getByLabel("片段时长（毫秒）").fill("1800");
  await expect(
    editor.getByRole("region", { name: "时间轴保存版本预览" }),
  ).toContainText("画面仍对应保存版本 v1");
});

test("RENDER-05：没有精确匹配的成片时不会播放其他时间轴版本", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  for (const row of env.deliveries) row.timeline_id = crypto.randomUUID();
  const { editor } = await env.openEditor();
  await expect(editor).toContainText("此保存版本尚无可播放成片");
  await expect(
    editor.getByRole("button", { name: "读取时间轴成片预览", exact: true }),
  ).toHaveCount(0);
  expect(env.contentReads).toHaveLength(0);
});

test("RENDER-05：预览详情哈希错配时停止加载私有内容", async ({ page }) => {
  const env = await timelineFixture(page);
  for (const row of env.deliveries)
    if (row.episode_id === env.episodes[0].id)
      row.timeline_sha256 = "f".repeat(64);
  const { editor } = await env.openEditor();
  await expect(editor.getByRole("alert")).toContainText("精确版本不一致");
  expect(env.contentReads).toHaveLength(0);
});

test("RENDER-05：撤权卸载预览并回收对象 URL", async ({ page }) => {
  const env = await timelineFixture(page);
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL;
    (window as unknown as { revoked: string[] }).revoked = [];
    URL.revokeObjectURL = (url) => {
      (window as unknown as { revoked: string[] }).revoked.push(url);
      revoke(url);
    };
  });
  const { editor, panel } = await env.openEditor();
  await editor
    .getByRole("button", { name: "读取时间轴成片预览", exact: true })
    .click();
  await expect(editor.locator("video")).toBeVisible();
  env.control.readStatus = 403;
  await panel
    .getByRole("button", { name: "刷新渲染记录", exact: true })
    .click();
  await expect(panel).toContainText("当前渲染记录不可访问");
  await expect(panel.locator("video")).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(
        () => (window as unknown as { revoked: string[] }).revoked.length,
      ),
    )
    .toBeGreaterThan(0);
});

test("RENDER-05：授权字体和字幕设置随新清单冻结", async ({ page }) => {
  const env = await timelineFixture(page);
  const { panel } = await env.openEditor();
  const create = panel.getByRole("button", {
    name: "创建预览清单",
    exact: true,
  });
  await expect(create).toBeDisabled();
  await panel.getByText("字幕渲染设置与授权字体", { exact: true }).click();
  await panel
    .getByLabel("字幕字体", { exact: true })
    .selectOption(env.fonts[0].id);
  await panel.getByLabel("字幕字号", { exact: true }).fill("36");
  await panel.getByLabel("字幕底部边距", { exact: true }).fill("40");
  await panel.getByLabel("字幕加粗", { exact: true }).check();
  await create.click();
  await expect
    .poll(
      () =>
        env.mutations.filter((m) => m.path.endsWith("/render-manifests"))
          .length,
    )
    .toBe(1);
  expect(
    env.mutations.find((m) => m.path.endsWith("/render-manifests"))!.body,
  ).toMatchObject({
    profile: "PREVIEW",
    font_file_id: env.fonts[0].id,
    subtitle_style: { font_size: 36, margin_bottom: 40, bold: true },
  });
});

test("RENDER-05：字幕文字时点另存、审核再加入草稿，原始来源保持", async ({
  page,
}) => {
  const env = await timelineFixture(page);
  const original = structuredClone(env.subtitles[0]);
  const { editor } = await env.openEditor();
  await editor.getByText("管理字幕文字、时点与版本", { exact: true }).click();
  const subtitles = editor.getByRole("region", {
    name: "字幕版本与编辑",
    exact: true,
  });
  await subtitles
    .getByLabel("字幕显示文字", { exact: true })
    .fill("雨终于停了。");
  await subtitles.getByLabel("字幕出现（毫秒）").fill("300");
  await subtitles.getByLabel("字幕消失（毫秒）").fill("800");
  await subtitles
    .getByRole("button", { name: "另存字幕修改", exact: true })
    .click();
  await expect.poll(() => env.subtitles.length).toBe(2);
  expect(env.subtitles[0]).toEqual(original);
  expect(env.subtitleWrites[0]).toMatchObject({
    changes: [{ cue_no: 1, text: "雨终于停了。", start_ms: 300, end_ms: 800 }],
  });
  await subtitles
    .getByRole("button", { name: "提交字幕审核", exact: true })
    .click();
  await subtitles
    .getByRole("button", { name: "批准此字幕版本", exact: true })
    .click();
  await subtitles
    .getByRole("button", { name: "将此字幕版本加入草稿", exact: true })
    .click();
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  expect(
    (env.saves[0].body.snapshot as TimelineSnapshot).tracks.find(
      (t) => t.kind === "SUBTITLE",
    )!.clips[0],
  ).toMatchObject({
    source_asset_version_id: env.subtitles[1].id,
    start_ms: 300,
    duration_ms: 500,
    audio_version_id: original.clips[0].audio_version_id,
  });
});

test("RENDER-05：字幕保存冲突保留文字与时点草稿", async ({ page }) => {
  const env = await timelineFixture(page);
  env.control.subtitleConflict = true;
  const { editor } = await env.openEditor();
  await editor.getByText("管理字幕文字、时点与版本", { exact: true }).click();
  await editor
    .getByLabel("字幕显示文字", { exact: true })
    .fill("保留冲突后的字幕");
  await editor
    .getByRole("button", { name: "另存字幕修改", exact: true })
    .click();
  await expect(editor.getByRole("alert")).toContainText("本机修改已保留");
  await page.reload();
  await page.getByText("时间轴编辑器", { exact: true }).click();
  await page.getByText("管理字幕文字、时点与版本", { exact: true }).click();
  await expect(page.getByLabel("字幕显示文字", { exact: true })).toHaveValue(
    "保留冲突后的字幕",
  );
});

test("RENDER-05：从既有配音绑定和选用音效装配精确来源", async ({ page }) => {
  const env = await timelineFixture(page);
  const snapshotId = crypto.randomUUID(),
    audioId = crypto.randomUUID(),
    musicId = crypto.randomUUID(),
    musicVersion = crypto.randomUUID();
  const scriptId = env.sourceBoards[0].script_version_id!;
  const snapshot = {
    id: snapshotId,
    episode_id: env.episodes[0].id,
    script_version_id: scriptId,
    script_sha256: "a".repeat(64),
    line_count: 1,
    created_at: new Date().toISOString(),
  };
  await page.route(
    `**/api/v1/episodes/${env.episodes[0].id}/audio-snapshots?*`,
    (route) => route.fulfill({ json: [snapshot] }),
  );
  await page.route(`**/api/v1/audio-snapshots/${snapshotId}/lines?*`, (route) =>
    route.fulfill({
      json: [
        {
          line_id: "rain-line",
          scene_id: "s1",
          scene_no: 1,
          sequence_no: 1,
          speaker: "林澈",
          text: "雨停了。",
          line_type: "dialogue",
          emotion: "平静",
        },
      ],
    }),
  );
  await page.route(
    `**/api/v1/audio-snapshots/${snapshotId}/lines/rain-line/history?*`,
    (route) =>
      route.fulfill({
        json: [
          {
            binding: {
              id: crypto.randomUUID(),
              snapshot_id: snapshotId,
              line_id: "rain-line",
              asset_version_id: audioId,
              duration_ms: 600,
            },
            version: {
              id: audioId,
              checksum: "d".repeat(64),
              status: "APPROVED",
              stale: false,
              version_no: 2,
            },
          },
        ],
      }),
  );
  await page.route(`**/api/v1/projects/${env.project.id}/assets?*`, (route) =>
    route.fulfill({
      json: [
        {
          id: musicId,
          project_id: env.project.id,
          asset_type: "MUSIC",
          name: "雨声环境音",
          status: "APPROVED",
          current_version_id: musicVersion,
        },
      ],
    }),
  );
  await page.route(
    `**/api/v1/projects/${env.project.id}/assets/${musicId}/versions/${musicVersion}`,
    (route) =>
      route.fulfill({
        json: {
          id: musicVersion,
          asset_id: musicId,
          checksum: "e".repeat(64),
          status: "APPROVED",
          stale: false,
          audio_resource: {
            purpose_role: "AMBIENCE",
            validation: { duration_ms: 2000 },
          },
        },
      }),
  );
  const { editor } = await env.openEditor();
  await editor.getByText("加入配音、音乐与音效", { exact: true }).click();
  await editor
    .getByRole("button", { name: "将此句配音加入草稿", exact: true })
    .click();
  await editor
    .getByLabel("音效关联镜头", { exact: true })
    .selectOption(env.original.snapshot.tracks[0].clips[0].shot_id!);
  await editor
    .getByRole("button", { name: "将此音乐或音效加入草稿", exact: true })
    .click();
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  await expect.poll(() => env.saves.length).toBe(1);
  const saved = env.saves[0].body.snapshot as TimelineSnapshot;
  expect(
    saved.tracks.find((t) => t.kind === "VOICE")!.clips.at(-1),
  ).toMatchObject({
    source_asset_version_id: audioId,
    script_version_id: scriptId,
    line_id: "rain-line",
    duration_ms: 600,
  });
  expect(
    saved.tracks.find((t) => t.kind === "SFX")!.clips.at(-1),
  ).toMatchObject({
    source_asset_version_id: musicVersion,
    duration_ms: 2000,
    shot_id: env.original.snapshot.tracks[0].clips[0].shot_id,
    audio: { purpose_role: "AMBIENCE" },
  });
});

test("RENDER-05：字体上传沿用原始字节网关并声明授权", async ({ page }) => {
  const env = await timelineFixture(page);
  const uploadId = crypto.randomUUID(),
    font = {
      ...env.fonts[0],
      id: crypto.randomUUID(),
      original_name: "scene.ttf",
    };
  const bytes = Buffer.from("controlled browser font upload contract");
  const requests: { headers: Record<string, string>; body: Buffer | null }[] =
    [];
  let declaration: Record<string, unknown> | undefined;
  await page.route(`**/api/v1/projects/${env.project.id}/uploads`, (route) =>
    route.fulfill({
      status: 201,
      json: {
        upload_id: uploadId,
        url: `/file-transfers/${uploadId}`,
        method: "POST",
        body: "raw",
        headers: { "Content-Type": "font/ttf" },
      },
    }),
  );
  await page.route(`**/file-transfers/${uploadId}`, (route) => {
    requests.push({
      headers: route.request().headers(),
      body: route.request().postDataBuffer(),
    });
    return route.fulfill({ status: 200, body: "" });
  });
  await page.route(
    `**/api/v1/projects/${env.project.id}/uploads/${uploadId}/complete`,
    (route) => {
      declaration = route.request().postDataJSON();
      return route.fulfill({ status: 201, json: font });
    },
  );
  const { panel } = await env.openEditor();
  await panel.getByText("字幕渲染设置与授权字体", { exact: true }).click();
  await panel.getByText("上传授权字体", { exact: true }).click();
  await panel
    .getByLabel("字体文件", { exact: true })
    .setInputFiles({ name: "scene.ttf", mimeType: "font/ttf", buffer: bytes });
  await panel.getByLabel("字体来源", { exact: true }).fill("本项目字体");
  await panel.getByLabel("字体授权说明", { exact: true }).fill("OFL 授权");
  await panel
    .getByLabel("我确认该字体可用于本作品字幕与成片", { exact: true })
    .check();
  await panel
    .getByRole("button", { name: "上传并选用字幕字体", exact: true })
    .click();
  await expect(panel.getByLabel("字幕字体", { exact: true })).toHaveValue(
    font.id,
  );
  expect(requests).toHaveLength(1);
  expect(requests[0].body).toEqual(bytes);
  expect(requests[0].headers.authorization).toBeUndefined();
  expect(requests[0].headers["content-type"]).toBe("font/ttf");
  expect(declaration).toEqual({
    source_note: "本项目字体",
    rights_note: "OFL 授权",
    rights_confirmed: true,
  });
});

for (const viewport of [
  { width: 1440, height: 1100 },
  { width: 390, height: 844 },
]) {
  test(`RENDER-05：${viewport.width}px 时间线布局和键盘焦点`, async ({
    page,
  }, info) => {
    await page.setViewportSize(viewport);
    const env = await timelineFixture(page);
    const { editor } = await env.openEditor();
    const toggle = editor.getByLabel("启用音乐轨道", { exact: true });
    await toggle.focus();
    await page.keyboard.press("Space");
    await expect(toggle).not.toBeChecked();
    await expect(toggle).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
    ).toBe(true);
    await editor.screenshot({
      path: info.outputPath(`render05-editor-${viewport.width}.png`),
    });
  });
}
