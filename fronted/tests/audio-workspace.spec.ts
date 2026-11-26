import { expect, test, type Locator, type Page } from "@playwright/test";
import { audioFixture } from "./audio-helpers";
import { deferred } from "./render-helpers";

async function choose(page: Page, field: Locator, label: string) {
  await field.click();
  await page.getByRole("option", { name: label, exact: true }).click();
}
async function rights(panel: Locator) {
  await panel
    .getByLabel("音频来源说明", { exact: true })
    .fill("本项目已确认的配音生成");
  await panel
    .getByLabel("音频授权说明", { exact: true })
    .fill("本人授权本项目制作使用");
  await panel
    .getByRole("checkbox", { name: "我确认拥有这段音频的使用权", exact: true })
    .check();
}
const imported = (panel: Locator) =>
  panel.locator("details").filter({ hasText: "导入或替换本句录音" });

test("AUDIO-07：剧集入口与逐句来源、缺配音不默认静音", async ({ page }) => {
  const env = await audioFixture(page);
  const panel = await env.open();
  await expect(
    page.getByRole("radio", { name: "配音与试听", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(
    panel.getByRole("region", { name: "配音台词目录", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel
      .getByRole("complementary", { name: "配音台词目录", exact: true })
      .getByRole("button", { pressed: true }),
  ).toContainText(env.lines[0].text);
  await expect(panel.getByText(/本句尚无已登记音频/)).toBeVisible();
  await expect(panel.getByText(/没有配音不会自动视为已静音/)).toBeVisible();
  await expect(
    panel.getByText(`配音来源：剧本 v${env.scripts[0].version_no}`, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(panel).not.toContainText(env.scripts[0].id);
  await panel
    .getByRole("button", { name: "打开渲染清单逐句确认", exact: true })
    .click();
  await expect(page).toHaveURL(/view=render/);
  await expect(
    page.getByRole("region", { name: "渲染与成片", exact: true }),
  ).toBeVisible();
  expect(env.audioMutations).toHaveLength(0);
});

test("AUDIO-07：精确批准剧本另存配音来源，无生成费用调用", async ({ page }) => {
  const env = await audioFixture(page);
  const original = env.snapshots[0].id;
  const panel = await env.open();
  await panel.getByText("从已批准剧本准备配音", { exact: true }).click();
  await choose(
    page,
    panel.getByLabel("配音来源剧本", { exact: true }),
    "剧本 v1 · 已批准",
  );
  await panel
    .getByRole("button", { name: "固定本次配音台词", exact: true })
    .click();
  await expect(
    panel.getByText("已固定本次配音的剧本与台词。后续改稿不会改写这份来源。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(env.snapshots.some((row) => row.id === original)).toBe(true);
  expect(env.audioMutations).toHaveLength(1);
  expect(env.audioMutations[0].body).toMatchObject({
    script_version_id: env.scripts[0].id,
    creation_key: expect.any(String),
  });
});

test("AUDIO-07：音色仅提交模型支持项与最新版本CAS，旧版保留", async ({
  page,
}) => {
  const env = await audioFixture(page);
  env.controlAudio.supported = ["voice", "speed"];
  const original = structuredClone(env.voices[0]);
  const panel = await env.open();
  await panel.getByText("为林舟保存新音色", { exact: true }).click();
  const defaultSpeed = panel.getByLabel("默认语速（可选）", { exact: true });
  await expect(defaultSpeed).toBeVisible();
  await expect(defaultSpeed).toHaveAttribute("min", "0.5");
  await expect(defaultSpeed).toHaveAttribute("max", "2");
  await expect(
    panel.getByLabel("语气与情绪说明（可选）", { exact: true }),
  ).toHaveCount(0);
  await choose(page, panel.getByLabel("供应商音色", { exact: true }), "Nova");
  await panel.getByLabel("默认语速（可选）", { exact: true }).fill("0.95");
  await panel
    .getByRole("button", { name: "保存新音色版本", exact: true })
    .click();
  await expect(
    panel.getByLabel("林舟的音色版本", { exact: true }),
  ).toContainText("v2");
  expect(env.audioMutations[0].body).toMatchObject({
    speaker: "林舟",
    expected_version_no: 1,
    model_config_version: "voice-config:1",
    parameters: { voice: "nova", speed: 0.95 },
    output_spec: {},
  });
  expect(env.voices.find((row) => row.id === original.id)).toEqual(original);
  expect(env.tasks).toHaveLength(0);
});

test("AUDIO-07：语音服务未配置及音色并发冲突保留输入可重试", async ({
  page,
}) => {
  const env = await audioFixture(page);
  env.controlAudio.configured = false;
  const panel = await env.open();
  await panel.getByText("为林舟保存新音色", { exact: true }).click();
  const voice = panel.getByLabel("供应商音色", { exact: true });
  await expect(voice).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "保存新音色版本", exact: true }),
  ).toBeDisabled();
  await expect(
    panel.getByText("请先配置语音服务", { exact: true }),
  ).toBeVisible();
  env.controlAudio.configured = true;
  env.controlAudio.voiceConflict = true;
  await panel
    .getByRole("button", { name: "重新读取音色目录", exact: true })
    .click();
  await choose(page, voice, "Nova");
  const save = panel.getByRole("button", {
    name: "保存新音色版本",
    exact: true,
  });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(
    panel.getByText("音色已由其他成员更新，请刷新音色版本。", { exact: true }),
  ).toBeVisible();
  await expect(voice).toContainText("Nova");
  expect(env.voices.filter((row) => row.speaker === "林舟")).toHaveLength(1);
});

test("音色目录区分没有目录、空目录及查询失败，切换模型后重新选择", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const options = env.controlAudio.catalogOptions;
  env.controlAudio.catalogStatus = "UNAVAILABLE";
  const panel = await env.open();
  await panel.getByText("为林舟保存新音色", { exact: true }).click();
  const voice = panel.getByLabel("供应商音色", { exact: true });
  const refresh = panel.getByRole("button", {
    name: "重新读取音色目录",
    exact: true,
  });
  const save = panel.getByRole("button", {
    name: "保存新音色版本",
    exact: true,
  });

  await expect(panel.getByText(/当前配音模型没有可用的音色目录/)).toBeVisible();
  await expect(voice).toBeDisabled();
  env.controlAudio.catalogStatus = "QUERY_FAILED";
  await refresh.click();
  await expect(panel.getByText(/音色目录查询失败/)).toBeVisible();
  await expect(voice).toBeDisabled();
  env.controlAudio.catalogStatus = "READY";
  env.controlAudio.catalogOptions = [];
  await refresh.click();
  await expect(panel.getByText("当前配音模型的音色目录为空。")).toBeVisible();
  await expect(save).toBeDisabled();

  env.controlAudio.catalogOptions = options;
  await refresh.click();
  await choose(page, voice, "Nova");
  await expect(save).toBeEnabled();
  env.controlAudio.catalogModel = "other-tts";
  env.controlAudio.catalogVersion = "voice-config:2";
  env.controlAudio.catalogOptions = [
    { id: "nova", label: "新模型音色", description: null },
  ];
  await refresh.click();
  await expect(
    panel.getByText("示例配音服务 · other-tts", { exact: true }),
  ).toBeVisible();
  await expect(voice).toContainText("选择当前模型支持的音色");
  await expect(save).toBeDisabled();
  await choose(page, voice, "新模型音色");
  await save.click();
  await expect(
    panel.getByLabel("林舟的音色版本", { exact: true }),
  ).toContainText("v2");
  expect(env.audioMutations).toHaveLength(1);
  expect(env.audioMutations[0].body).toMatchObject({
    model_config_version: "voice-config:2",
    parameters: { voice: "nova" },
  });
});

test("AUDIO-07：单句提交去重，未知网络回执同键重试并刷新恢复", async ({
  page,
}) => {
  const env = await audioFixture(page);
  env.controlAudio.failTaskOnce = true;
  const gate = deferred();
  env.controlAudio.gate = gate.promise;
  const panel = await env.open();
  const generate = panel.getByRole("button", {
    name: "生成本句配音",
    exact: true,
  });
  await expect(generate).toBeEnabled();
  await generate.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  try {
    await expect.poll(() => env.audioMutations.length).toBe(1);
    await expect(
      panel.getByRole("button", { name: "正在创建任务…", exact: true }),
    ).toBeDisabled();
  } finally {
    gate.release();
    env.controlAudio.gate = undefined;
  }
  await expect(generate).toBeEnabled();
  await generate.click();
  const taskHref = new RegExp(`[?&]generation_job=${env.tasks[0].job_id}$`);
  const detail = panel.getByRole("region", {
    name: "本句配音任务",
    exact: true,
  });
  await expect(
    detail.getByRole("link", { name: "在任务中心查看", exact: true }),
  ).toHaveAttribute("href", taskHref);
  await expect(detail).not.toContainText(env.tasks[0].job_id);
  expect(env.audioMutations).toHaveLength(2);
  expect(env.audioMutations[0].body.creation_key).toBe(
    env.audioMutations[1].body.creation_key,
  );
  expect(env.tasks).toHaveLength(1);
  await page.reload();
  await expect(
    page
      .getByRole("region", { name: "本句配音任务", exact: true })
      .getByRole("link", { name: "在任务中心查看", exact: true }),
  ).toHaveAttribute("href", taskHref);
  expect(env.audioMutations).toHaveLength(2);
});

test("AUDIO-07：只重生成所选精确版本，传资产revision并保护旧历史", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const original = env.addHistory();
  const originalCopy = structuredClone(original);
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "仅重生成本句配音", exact: true })
    .click();
  await expect.poll(() => env.audioMutations.length).toBe(1);
  expect(env.audioMutations[0].path).toBe(
    `/api/v1/audio-snapshots/${env.snapshots[0].id}/lines/${env.lines[0].line_id}/tts/regenerate`,
  );
  expect(env.audioMutations[0].body).toMatchObject({
    source_audio_version_id: original.version.id,
    expected_revision: 1,
    voice_version_id: env.voices[0].id,
  });
  expect(original).toEqual(originalCopy);
  expect(env.tasks).toHaveLength(1);
});

test("AUDIO-07：切换音频历史时等待精确版本回执，重生成不用旧详情", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const first = env.addHistory();
  const target = env.assets.get(first.version.asset_id)!;
  env.addHistory(env.snapshots[0], env.lines[0], target);
  const gate = deferred();
  await page.route(
    `**/assets/${target.id}/versions/${first.version.id}`,
    async (route) => {
      await gate.promise;
      await route.fulfill({ json: first.version });
    },
  );
  const panel = await env.open();
  const generate = panel.getByRole("button", {
    name: "仅重生成本句配音",
    exact: true,
  });
  await expect(generate).toBeEnabled();
  await choose(
    page,
    panel.getByLabel("本句音频版本", { exact: true }),
    "v1 · 导入录音 · 1000 毫秒",
  );
  try {
    await expect(generate).toBeDisabled();
    expect(env.audioMutations).toHaveLength(0);
  } finally {
    gate.release();
  }
  await expect(generate).toBeEnabled();
  await generate.click();
  await expect.poll(() => env.audioMutations.length).toBe(1);
  expect(env.audioMutations[0].body).toMatchObject({
    source_audio_version_id: first.version.id,
    expected_revision: 2,
  });
});

test("AUDIO-07：任务轮询未知结果停止且阻止再次生成，任务中心可追溯", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const task = env.addTask("RUNNING");
  const panel = await env.open();
  const detail = panel.getByRole("region", {
    name: "本句配音任务",
    exact: true,
  });
  await expect(detail).toBeVisible();
  await expect(detail).not.toContainText(task.job_id);
  task.status = "RESULT_UNKNOWN";
  task.revision++;
  await expect(detail.getByText(/供应商结果尚未核清/)).toBeVisible({
    timeout: 10000,
  });
  await expect(
    panel.getByRole("button", { name: "生成本句配音", exact: true }),
  ).toBeDisabled();
  await expect(
    detail.getByRole("link", { name: "在任务中心查看", exact: true }),
  ).toHaveAttribute(
    "href",
    new RegExp(`view=tasks.*generation_job=${task.job_id}`),
  );
  expect(env.audioMutations).toHaveLength(0);
});

test("AUDIO-07：成功任务须授权与真实机器检查回执才登记，使用资产CAS", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const task = env.addTask("SUCCEEDED");
  const panel = await env.open();
  const detail = panel.getByRole("region", {
    name: "本句配音任务",
    exact: true,
  });
  const register = detail.getByRole("button", {
    name: "确认授权并登记本句结果",
    exact: true,
  });
  await expect(register).toBeDisabled();
  await rights(detail);
  env.controlAudio.failRegistration = true;
  await register.click();
  await expect(
    detail.getByText("音频机器检查未通过，未登记候选。", { exact: true }),
  ).toBeVisible();
  expect(env.histories.size).toBe(0);
  env.controlAudio.failRegistration = false;
  await register.click();
  await expect(detail.getByText(/机器检查：pcm_s16le/)).toBeVisible();
  await expect(
    detail.getByText("本任务结果已登记。可在本句音频历史中试听和审核。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(env.audioMutations[1].body).toMatchObject({
    expected_revision: 0,
    rights_confirmed: true,
  });
  expect(task.revision).toBe(19);
  await expect(
    panel.getByRole("button", { name: "提交音频审核", exact: true }),
  ).toBeVisible();
});

test("AUDIO-07：鉴权精确文件试听，切句撤销Blob并拒绝损坏字节", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL;
    const urls: string[] = [];
    Object.assign(window, { audioRevokedUrls: urls });
    URL.revokeObjectURL = (url) => {
      urls.push(url);
      revoke.call(URL, url);
    };
  });
  const env = await audioFixture(page);
  const version = env.addHistory();
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取本句试听", exact: true })
    .click();
  const player = panel.getByLabel("本句音频播放器", { exact: true });
  await expect(player).toHaveJSProperty("readyState", 4);
  const url = await player.getAttribute("src");
  expect(url).toMatch(/^blob:/);
  const content = env.audioReads.find((row) =>
    row.path.endsWith(`/files/${version.binding.file_id}/content`),
  )!;
  expect(content.authorization).toMatch(/^Bearer .+/);
  const identity = await page.request.get("/api/v1/auth/me", {
    headers: { Authorization: content.authorization },
  });
  expect(identity.status()).toBe(200);
  expect(await identity.json()).toMatchObject({ id: env.session.user.id });
  await panel.getByRole("button", { name: /2 · 场景 1 · 旁白/ }).click();
  await expect(player).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { audioRevokedUrls: string[] })
            .audioRevokedUrls,
      ),
    )
    .toContain(url);
  await panel.getByRole("button", { name: /1 · 场景 1 · 林舟/ }).click();
  env.controlAudio.corruptMedia = true;
  await panel
    .getByRole("button", { name: "读取本句试听", exact: true })
    .click();
  await expect(
    panel.getByText("音频完整性检查未通过，请重新读取。", { exact: true }),
  ).toBeVisible();
  await expect(player).toHaveCount(0);
});

test("AUDIO-07：切句忽略迟到私有音频响应", async ({ page }) => {
  const env = await audioFixture(page);
  env.addHistory();
  const gate = deferred();
  env.controlAudio.mediaGate = gate.promise;
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取本句试听", exact: true })
    .click();
  try {
    await expect
      .poll(
        () =>
          env.audioReads.filter((row) => row.path.endsWith("/content")).length,
      )
      .toBe(1);
    await panel.getByRole("button", { name: /2 · 场景 1 · 旁白/ }).click();
    await expect(
      panel.getByRole("heading", { name: "旁白", exact: true }),
    ).toBeVisible();
  } finally {
    gate.release();
  }
  await expect(panel.getByLabel("本句音频播放器", { exact: true })).toHaveCount(
    0,
  );
  expect(env.audioMutations).toHaveLength(0);
});

test("AUDIO-07：导入已有录音保留来源说明与CAS，原音频不覆盖", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const original = env.addHistory();
  const saved = structuredClone(original);
  const panel = await env.open();
  const form = imported(panel);
  await form.locator("summary").click();
  await choose(
    page,
    form.getByLabel("或使用已上传的录音", { exact: true }),
    env.sourceFile.original_name,
  );
  await expect(
    form.getByLabel("音频来源说明", { exact: true }),
  ).toHaveAttribute("readonly", "");
  await expect(form.getByLabel("音频来源说明", { exact: true })).toHaveValue(
    env.sourceFile.source_note,
  );
  const submit = form.getByRole("button", {
    name: "确认授权并导入本句录音",
    exact: true,
  });
  await expect(submit).toBeDisabled();
  await form
    .getByRole("checkbox", { name: "我确认拥有这段音频的使用权", exact: true })
    .check();
  await submit.click();
  await expect
    .poll(
      () =>
        env.histories.get(`${env.snapshots[0].id}:${env.lines[0].line_id}`)
          ?.length,
    )
    .toBe(2);
  expect(env.audioMutations[0].body).toMatchObject({
    source_file_id: env.sourceFile.id,
    target_asset_id: original.version.asset_id,
    expected_revision: 1,
  });
  expect(original).toEqual(saved);
});

test("AUDIO-07：上传后登记失败重试复用同一录音与幂等键", async ({ page }) => {
  const env = await audioFixture(page);
  env.controlAudio.failImportOnce = true;
  const panel = await env.open();
  const form = imported(panel);
  await form.locator("summary").click();
  await form.getByLabel("上传外部录音", { exact: true }).setInputFiles({
    name: "本人配音.wav",
    mimeType: "audio/wav",
    buffer: env.media,
  });
  await rights(form);
  const submit = form.getByRole("button", {
    name: "确认授权并导入本句录音",
    exact: true,
  });
  await submit.click();
  await expect(
    form.getByText("资产版本已变化，请刷新后重试。", { exact: true }),
  ).toBeVisible();
  await expect(
    form.getByLabel("音频授权说明", { exact: true }),
  ).toHaveAttribute("readonly", "");
  await submit.click();
  await expect.poll(() => env.histories.size).toBe(1);
  expect(
    env.audioMutations.filter((row) => row.path.endsWith("/uploads")),
  ).toHaveLength(1);
  expect(
    env.audioMutations.filter((row) =>
      row.path.endsWith("/file-transfers/recording-upload"),
    ),
  ).toHaveLength(1);
  const attempts = env.audioMutations.filter((row) =>
    row.path.endsWith("/imports"),
  );
  expect(attempts).toHaveLength(2);
  expect(attempts[0].body).toEqual(attempts[1].body);
});

test("AUDIO-07：审核批准显式选用及锁定沿用版本与资产CAS", async ({ page }) => {
  const env = await audioFixture(page);
  const original = env.addHistory();
  const target = env.assets.get(original.version.asset_id)!;
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "提交音频审核", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "批准本句音频", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "选用本句音频", exact: true })
    .click();
  await expect(panel.getByText("当前选用版本", { exact: true })).toBeVisible();
  await panel
    .getByRole("button", { name: "锁定本句选用音频", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "仅重生成本句配音", exact: true }),
  ).toBeDisabled();
  await imported(panel).locator("summary").click();
  await expect(
    panel.getByRole("button", { name: "确认授权并导入本句录音", exact: true }),
  ).toBeDisabled();
  expect(env.audioMutations.map((row) => row.body)).toMatchObject([
    { expected_revision: 0, status: "READY_FOR_REVIEW" },
    { expected_revision: 1, status: "APPROVED" },
    { expected_revision: 1, expected_lock_version: 0 },
    { expected_revision: 2, expected_lock_version: 0 },
  ]);
  await panel
    .getByRole("button", { name: "解锁本句选用音频", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "仅重生成本句配音", exact: true }),
  ).toBeEnabled();
  expect(target.current_version_id).toBe(original.version.id);
});

test("AUDIO-07：替换选用先展示依赖影响，取消零写入再确认精确摘要", async ({
  page,
}) => {
  const env = await audioFixture(page);
  const first = env.addHistory();
  const target = env.assets.get(first.version.asset_id)!;
  first.version.status = "APPROVED";
  target.current_version_id = first.version.id;
  target.status = "APPROVED";
  const second = env.addHistory(env.snapshots[0], env.lines[0], target);
  second.version.status = "APPROVED";
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "选用本句音频", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "确认变更影响",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "取消变更", exact: true }).click();
  expect(env.audioMutations).toHaveLength(0);
  await panel
    .getByRole("button", { name: "选用本句音频", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "确认影响并继续", exact: true })
    .click();
  await expect.poll(() => target.current_version_id).toBe(second.version.id);
  expect(env.audioMutations[0].body).toMatchObject({
    impact_digest: "audio-impact",
    expected_revision: 2,
    expected_lock_version: 0,
  });
});

for (const mode of ["viewer", "archived"] as const)
  test(`AUDIO-07：${mode}只读仍可试听，不提供写入`, async ({ page }) => {
    const env = await audioFixture(page);
    env.addHistory();
    if (mode === "viewer") env.control.role = "viewer";
    else await env.archiveEpisodes();
    const panel = await env.open();
    await expect(panel.getByText(/当前为只读模式/)).toBeVisible();
    await expect(
      panel.getByRole("button", { name: "生成本句配音", exact: true }),
    ).toHaveCount(0);
    await expect(
      panel.getByText("导入或替换本句录音", { exact: true }),
    ).toHaveCount(0);
    await panel
      .getByRole("button", { name: "读取本句试听", exact: true })
      .click();
    await expect(
      panel.getByLabel("本句音频播放器", { exact: true }),
    ).toBeVisible();
    expect(env.audioMutations).toHaveLength(0);
  });

test("AUDIO-07：撤权清空声音和操作，跨集刷新使用独立来源", async ({ page }) => {
  const env = await audioFixture(page);
  env.addHistory();
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取本句试听", exact: true })
    .click();
  await expect(
    panel.getByLabel("本句音频播放器", { exact: true }),
  ).toBeVisible();
  env.controlAudio.readStatus = 403;
  await panel
    .getByRole("button", { name: "刷新音频历史", exact: true })
    .click();
  await expect(panel.getByText(/配音来源暂不可访问/)).toBeVisible();
  await expect(panel.getByLabel("本句音频播放器", { exact: true })).toHaveCount(
    0,
  );
  env.controlAudio.readStatus = 0;
  await env.open(1);
  await expect(panel.getByText(/本句尚无已登记音频/)).toBeVisible();
  expect(
    env.audioReads.some((row) =>
      row.path.includes(`/audio-snapshots/${env.snapshots[1].id}/lines`),
    ),
  ).toBe(true);
  expect(env.audioMutations).toHaveLength(0);
});

test("AUDIO-07：台词与历史分页保留选择，刷新从头读取", async ({ page }) => {
  const env = await audioFixture(page);
  for (let number = 3; number <= 31; number++)
    env.lines.push({
      ...env.lines[0],
      line_id: `line-${number}`,
      sequence_no: number - 1,
      text: `分页台词 ${number}`,
    });
  env.snapshots[0].line_count = 31;
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "继续加载台词", exact: true })
    .click();
  await panel.getByRole("button", { name: /31 · 场景 1 · 林舟/ }).click();
  await expect(
    panel.getByRole("region", { name: "台词配音详情", exact: true }),
  ).toContainText("分页台词 31");
  expect(
    env.audioReads.some((row) => /\/lines\?limit=30&offset=30$/.test(row.path)),
  ).toBe(true);
  await panel.getByRole("button", { name: "刷新台词", exact: true }).click();
  await expect(
    panel.getByRole("button", { name: /31 · 场景 1 · 林舟/ }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("region", { name: "台词配音详情", exact: true }),
  ).toContainText(env.lines[0].text);
});

for (const size of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
])
  test(`AUDIO-07：${size.width}像素配音工作区与键盘截图`, async ({
    page,
  }, info) => {
    await page.setViewportSize(size);
    const env = await audioFixture(page);
    env.addHistory();
    const panel = await env.open();
    await page.screenshot({
      path: info.outputPath(`audio-workspace-source-${size.width}.png`),
      fullPage: true,
    });
    const voice = panel.getByLabel("林舟的音色版本", { exact: true });
    await voice.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("option").first()).toBeVisible();
    await page.keyboard.press("Escape");
    await panel
      .getByRole("button", { name: "读取本句试听", exact: true })
      .click();
    await expect(
      panel.getByLabel("本句音频播放器", { exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath(`audio-workspace-${size.width}.png`),
      fullPage: true,
    });
  });

test("AUDIO-09：剧情变声、跨集恢复和本句表演参数", async ({ page }) => {
  const env = await audioFixture(page);
  const characterId = crypto.randomUUID();
  env.controlAudio.voiceCharacterId = characterId;
  const normal = env.voices[0];
  normal.character_asset_id = characterId;
  const changed = {
    ...normal,
    id: crypto.randomUUID(),
    version_no: 2,
    parameters: { ...normal.parameters, voice: "ash" },
  };
  env.voices.push(changed);
  const panel = await env.open();
  await panel.getByRole("button", { name: "编辑跨集声音档案" }).click();
  const form = panel.getByRole("form", { name: "跨集声音档案" });
  await form.getByRole("button", { name: "添加剧情变声" }).click();
  await form.getByLabel("故事开始时点").fill("10");
  await form.getByLabel("恢复默认音色时点").fill("20");
  await form.getByLabel("剧情原因").fill("回忆中嗓子受伤");
  await choose(page, form.getByLabel("变声音色"), "v2 · ash");
  await form.getByRole("button", { name: "保存声音档案" }).click();
  const resolved = panel.getByRole("region", { name: "本句声音档案" });
  await expect(resolved).toContainText("音色 v2");
  await expect(resolved).toContainText("故事时点 10");
  await expect(resolved).toContainText("回忆中嗓子受伤");
  await expect(resolved).toContainText("等同伴停步后开口");
  const lineSpeed = panel.getByLabel("本句语速", { exact: true });
  await expect(lineSpeed).toHaveAttribute("min", "0.5");
  await expect(lineSpeed).toHaveAttribute("max", "2");
  await lineSpeed.fill("2.5");
  await expect(panel.getByText("语速须在 0.5 到 2 之间。")).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "生成本句配音", exact: true }),
  ).toBeDisabled();
  await lineSpeed.fill("0.8");
  await panel.getByLabel("补充本句表演说明").fill("第二句前稍停");
  await panel
    .getByRole("button", { name: "生成本句配音", exact: true })
    .click();
  await expect(panel.getByText(/本句配音任务已创建/)).toBeVisible();
  expect(env.audioMutations.map(({ body }) => body)).toEqual([
    expect.objectContaining({
      expected_version_no: 0,
      default_voice_version_id: normal.id,
      ledger_version_id: expect.any(String),
      changes: [
        {
          story_start: 10,
          story_end: 20,
          voice_version_id: changed.id,
          reason: "回忆中嗓子受伤",
        },
      ],
    }),
    expect.objectContaining({
      voice_version_id: changed.id,
      voice_profile_version_id: expect.any(String),
      performance_parameters: { speed: 0.8, instructions: "第二句前稍停" },
    }),
  ]);
  const later = await env.open(1);
  const restored = later.getByRole("region", { name: "本句声音档案" });
  await expect(restored).toContainText("档案 v1 · 音色 v1");
  await expect(restored).toContainText("故事时点 20");
  await expect(restored.getByText(/回忆中嗓子受伤/)).toHaveCount(0);
  expect(env.audioMutations).toHaveLength(2);

  const newerVoices = Array.from({ length: 30 }, (_, index) => ({
    ...normal,
    id: crypto.randomUUID(),
    version_no: 32 - index,
  }));
  await page.route(
    `**/api/v1/projects/${env.project.id}/audio-voices?*`,
    (route) => {
      const params = new URL(route.request().url()).searchParams;
      return params.get("character_asset_id") === characterId &&
        params.get("offset") === "0"
        ? route.fulfill({ json: newerVoices })
        : route.fallback();
    },
  );
  await later.getByRole("button", { name: "编辑跨集声音档案" }).click();
  const savedProfile = later.getByRole("form", { name: "跨集声音档案" });
  await expect(
    savedProfile.getByRole("button", { name: "继续加载角色音色" }),
  ).toBeVisible();
  await expect(savedProfile.getByLabel("跨集默认音色")).toHaveText(
    "已保存的默认音色",
  );
  await expect(savedProfile.getByLabel("变声音色")).toHaveText(
    "剧情变声 1 的已保存音色",
  );
  await savedProfile.getByLabel("剧情原因").fill("回忆中声音沙哑");
  await savedProfile.getByRole("button", { name: "保存声音档案" }).click();
  await expect.poll(() => env.audioMutations.length).toBe(3);
  expect(env.audioMutations[2].body).toMatchObject({
    expected_version_no: 1,
    default_voice_version_id: normal.id,
    changes: [
      {
        story_start: 10,
        story_end: 20,
        voice_version_id: changed.id,
        reason: "回忆中声音沙哑",
      },
    ],
  });
});
