import { test, expect, type Page } from "@playwright/test";
import type {
  LedgerContent,
  LedgerHead,
  LedgerVersion,
} from "../src/features/story/story-ledger";
import { openWorkspacePanels } from "./workspace-helpers";

async function openLedger(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  const disclosure = page.locator("details").filter({
    has: page.locator("summary", { hasText: /^剧情事件与状态账本$/ }),
  });
  if ((await disclosure.getAttribute("open")) === null)
    await disclosure.locator("summary").first().click();
  await expect(
    page.getByRole("region", { name: "剧情事件与状态账本", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "重新载入账本状态", exact: true }),
  ).toBeEnabled();
}
async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `ledger-${crypto.randomUUID()}@example.com`,
      password: "story-ledger-browser-password",
      display_name: "剧情账本验收",
    },
  });
  expect(registered.status()).toBe(201);
  const user = await registered.json();
  const headers = { Authorization: `Bearer ${user.access_token}` };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "剧情时序验收" },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const ep = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "雨夜拾信" } },
  );
  expect(ep.status()).toBe(201);
  const episode = await ep.json();
  const characterIds: string[] = [];
  for (const name of ["阿青", "石岚"]) {
    const character = await page.request.post(
      `/api/v1/projects/${project.id}/story-entries`,
      { headers, data: { kind: "CHARACTER", name } },
    );
    expect(character.status()).toBe(201);
    characterIds.push((await character.json()).id);
  }
  const sceneIds = [crypto.randomUUID(), crypto.randomUUID()];
  const script = await page.request.post(
    `/api/v1/episodes/${episode.id}/scripts`,
    {
      headers,
      data: {
        expected_version_no: 0,
        content: {
          schema_version: "2.0",
          title: "雨夜拾信",
          summary: "阿青拾到信件，次日交还。",
          estimated_duration_ms: 30000,
          characters: ["阿青", "石岚"],
          scenes: sceneIds.map((scene_id, index) => ({
            scene_id,
            scene_no: index + 1,
            title: index ? "清晨交信" : "无对白拾信",
            action: index ? "阿青把信交给石岚。" : "阿青在雨中拾到信件。",
            dialogue: [],
          })),
        },
      },
    },
  );
  expect(script.status()).toBe(201);
  const scriptRow = await script.json();
  const approved = await page.request.post(
    `/api/v1/episodes/${episode.id}/scripts/${scriptRow.id}/review`,
    { headers, data: { decision: "APPROVE" } },
  );
  expect(approved.status()).toBe(200);
  const path = `/api/v1/projects/${project.id}/story-ledger`;
  const times = sceneIds.map((scene_id, index) => ({
    id: crypto.randomUUID(),
    episode_id: episode.id,
    episode_no: 1,
    script_version_id: scriptRow.id,
    scene_id,
    scene_no: index + 1,
    segment_no: 1,
    story_time: index + 1,
  }));
  const content: LedgerContent = {
    schema_version: "1.0",
    scene_times: times,
    entries: times.map((scene, index) => ({
      id: crypto.randomUUID(),
      kind: "EVENT",
      status: "CONFIRMED",
      subject_id: null,
      target_id: null,
      state_key: index ? "交还信件" : "拾到信件",
      value: index ? "信件已经交给石岚" : "阿青捡到密封信件",
      story_start: index + 1,
      story_end: null,
      disclosure_start: scene.id,
      disclosure_end: null,
      source_script_version_id: scriptRow.id,
      source_scene_id: scene.scene_id,
      known_by: [
        {
          character_id: characterIds[index],
          story_start: index + 1,
          story_end: null,
        },
      ],
      causes: [],
    })),
  };
  async function save(
    expected_revision: number,
    base_version_id: string | null,
    value = content,
  ) {
    const response = await page.request.post(path + "/versions", {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: { expected_revision, base_version_id, content: value },
    });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()) as {
      head: LedgerHead;
      version: LedgerVersion;
    };
  }
  async function visit() {
    await page.addInitScript(
      (id) => localStorage.setItem("arclight-project", id),
      project.id,
    );
    await page.goto("/");
    await openLedger(page);
  }
  return {
    headers,
    project,
    episode,
    sceneIds,
    scriptRow,
    characterIds,
    content,
    times,
    path,
    save,
    visit,
  };
}

test("SERIES-02：真实批准来源含无对白场景，人工建版与按时点/角色查询不泄漏未来事实", async ({
  page,
}, testInfo) => {
  const env = await setup(page);
  await env.visit();
  await page.getByRole("button", { name: "新建账本草稿", exact: true }).click();
  await page
    .getByLabel("映射来源剧本", { exact: true })
    .selectOption(env.scriptRow.id);
  await expect(
    page
      .getByLabel("映射来源场景", { exact: true })
      .getByRole("option", { name: "第 1 场 · 无对白拾信" }),
  ).toHaveCount(1);
  await page
    .getByLabel("映射来源场景", { exact: true })
    .selectOption(env.sceneIds[0]);
  await page.getByLabel("故事时点", { exact: true }).fill("1");
  await page.getByRole("button", { name: "添加时序映射", exact: true }).click();
  await page.getByRole("button", { name: "添加账本条目", exact: true }).click();
  await page.getByLabel("条目 1 状态名称", { exact: true }).fill("拾到信件");
  await page
    .getByLabel("条目 1 内容", { exact: true })
    .fill("阿青捡到密封信件");
  await page
    .getByLabel("条目 1 性质", { exact: true })
    .selectOption("CONFIRMED");
  await page
    .getByLabel("条目 1 精确来源", { exact: true })
    .selectOption({ index: 1 });
  const entry = page.getByRole("group", { name: "账本条目 1", exact: true });
  await entry.locator("summary").click();
  await entry
    .getByRole("button", { name: "添加知情角色", exact: true })
    .click();
  await page
    .getByLabel("条目 1 知情角色 1", { exact: true })
    .selectOption(env.characterIds[0]);
  const originalViewport = page.viewportSize();
  await page.screenshot({
    path: testInfo.outputPath("story-ledger-editor-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("story-ledger-editor-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  if (originalViewport) await page.setViewportSize(originalViewport);
  await page
    .getByRole("button", { name: "保存账本新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "账本版本 1（未批准）", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "批准此精确版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "账本版本 1（已批准）", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("账本查询位置", { exact: true })
    .selectOption({ index: 1 });
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "此时已确认事实", exact: true }),
  ).toContainText("阿青捡到密封信件");
  await page
    .getByLabel("账本查询视角", { exact: true })
    .selectOption("CHARACTER");
  await expect(
    page.getByRole("region", { name: "账本受控查询结果", exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("账本视角角色", { exact: true })
    .selectOption(env.characterIds[1]);
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "此时已确认事实", exact: true }),
  ).not.toContainText("阿青捡到密封信件");
  await page
    .getByLabel("账本视角角色", { exact: true })
    .selectOption(env.characterIds[0]);
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "此时已确认事实", exact: true }),
  ).toContainText("阿青捡到密封信件");
  await page.screenshot({
    path: testInfo.outputPath("story-ledger-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("story-ledger-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);

  // Add an actual later confirmed event in another immutable version.
  const first = await (
    await page.request.get(env.path + "/versions", { headers: env.headers })
  ).json();
  const head = await (
    await page.request.get(env.path, { headers: env.headers })
  ).json();
  const second = await env.save(head.revision, first.items[0].id);
  expect(
    (
      await page.request.post(
        env.path + `/versions/${second.version.id}/approve`,
        {
          headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
          data: { expected_revision: second.head.revision },
        },
      )
    ).status(),
  ).toBe(200);
  await page
    .getByRole("button", { name: "重新载入账本状态", exact: true })
    .click();
  await page.getByRole("button", { name: /^版本 2 · 最新/ }).click();
  await expect(
    page.getByRole("heading", { name: "账本版本 2（已批准）", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("账本查询位置", { exact: true })
    .selectOption(env.times[0].id);
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  const result = page.getByRole("region", {
    name: "账本受控查询结果",
    exact: true,
  });
  await expect(result).toContainText("阿青捡到密封信件");
  await expect(result).not.toContainText("信件已经交给石岚");
});

test("SERIES-02：本机恢复与保存/批准丢回执复用原键，不产生重复版本", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await env.save(0, null);
  await env.visit();
  await page.getByRole("button", { name: "以此版本起草", exact: true }).click();
  await page
    .getByLabel("条目 1 内容", { exact: true })
    .fill("保存在本机的账本草稿");
  page.once("dialog", (dialog) => void dialog.accept());
  await page.reload();
  await openLedger(page);
  await expect(page.getByLabel("条目 1 内容", { exact: true })).toHaveValue(
    "保存在本机的账本草稿",
  );
  const keys: string[] = [];
  const payloads: string[] = [];
  let loseSave = true;
  await page.route("**" + env.path + "/versions", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    payloads.push(route.request().postData()!);
    const response = await route.fetch();
    if (loseSave) {
      loseSave = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "保存账本新版本", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "原修订和重试凭据已保留" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "保存账本新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "账本版本 2（未批准）", exact: true }),
  ).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[0]).toBe(keys[1]);
  expect(payloads[0]).toBe(payloads[1]);
  const rows = await (
    await page.request.get(env.path + "/versions", { headers: env.headers })
  ).json();
  expect(rows.total).toBe(2);
  expect(rows.items[1].id).toBe(first.version.id);
  const approvalKeys: string[] = [];
  let loseApproval = true;
  await page.route("**" + env.path + "/versions/*/approve", async (route) => {
    approvalKeys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    if (loseApproval) {
      loseApproval = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "批准此精确版本", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "批准请求与重试凭据已保留" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "批准此精确版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "账本版本 2（已批准）", exact: true }),
  ).toBeVisible();
  expect(approvalKeys).toHaveLength(2);
  expect(approvalKeys[0]).toBe(approvalKeys[1]);
  const old = await (
    await page.request.get(env.path + "/versions/" + first.version.id, {
      headers: env.headers,
    })
  ).json();
  expect(old.content).toEqual(first.version.content);
  expect(old.approval).toBeNull();
});

test("SERIES-02：CAS冲突保留精确基础和草稿，显式放弃后可从最新版起草", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await env.save(0, null);
  await env.visit();
  await page.getByRole("button", { name: "以此版本起草", exact: true }).click();
  await page
    .getByLabel("条目 1 内容", { exact: true })
    .fill("冲突后仍保留的内容");
  await env.save(first.head.revision, first.version.id);
  await page
    .getByRole("button", { name: "保存账本新版本", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "草稿、原修订和重试凭据已保留" }),
  ).toBeVisible();
  await expect(page.getByLabel("条目 1 内容", { exact: true })).toHaveValue(
    "冲突后仍保留的内容",
  );
  await page
    .getByRole("button", { name: "重新载入账本状态", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "草稿仍基于修订 1" }),
  ).toBeVisible();
  await page.getByRole("button", { name: /^版本 2 · 最新/ }).click();
  await expect(
    page.getByRole("heading", { name: "账本版本 2（未批准）", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("条目 1 内容", { exact: true })).toHaveValue(
    "冲突后仍保留的内容",
  );
  await page.getByRole("button", { name: "放弃账本草稿", exact: true }).click();
  await page.getByRole("button", { name: "继续编辑账本", exact: true }).click();
  await expect(page.getByLabel("条目 1 内容", { exact: true })).toHaveValue(
    "冲突后仍保留的内容",
  );
  await page.getByRole("button", { name: "放弃账本草稿", exact: true }).click();
  await page
    .getByRole("button", { name: "确认放弃账本草稿", exact: true })
    .click();
  await page.getByRole("button", { name: "以此版本起草", exact: true }).click();
  await expect(
    page.getByRole("form", { name: "剧情账本草稿", exact: true }),
  ).toContainText("基于修订 2");
  await expect(page.getByLabel("条目 1 内容", { exact: true })).toHaveValue(
    "阿青捡到密封信件",
  );
});

test("SERIES-02：版本真实分页，迟到详情不能替换新选择，查询迟到不显示旧视角", async ({
  page,
}) => {
  const env = await setup(page);
  let previous: string | null = null;
  for (let revision = 0; revision < 21; revision++)
    previous = (await env.save(revision, previous)).version.id;
  await env.visit();
  const pagination = page.getByRole("region", {
    name: "账本版本分页",
    exact: true,
  });
  await expect(pagination).toContainText("已加载 20 条");
  await pagination
    .getByRole("button", { name: "加载更多账本版本", exact: true })
    .click();
  await expect(pagination).toContainText("已加载 21 条");
  const rows = await (
    await page.request.get(env.path + "/versions?limit=100", {
      headers: env.headers,
    })
  ).json();
  let release!: () => void;
  let detailSettled!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const detailCompletion = new Promise<void>((resolve) => {
    detailSettled = resolve;
  });
  const oldDetailPath = env.path + "/versions/" + rows.items[20].id;
  const detailCancelled = page.waitForEvent("requestfailed", {
    predicate: (request) => new URL(request.url()).pathname === oldDetailPath,
  });
  await page.route("**" + oldDetailPath, async (route) => {
    const response = await route.fetch();
    await gate;
    try {
      await route.fulfill({ response });
    } catch {
      await detailCancelled;
    } finally {
      detailSettled();
    }
  });
  await page.getByRole("button", { name: /^版本 1 · 未批准$/ }).click();
  await expect(
    page.getByText("正在读取精确账本版本…", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /^版本 21 · 最新/ }).click();
  await expect(
    page.getByRole("heading", { name: "账本版本 21（未批准）", exact: true }),
  ).toBeVisible();
  release();
  const cancelledDetail = await detailCancelled;
  expect(cancelledDetail.failure()?.errorText).toMatch(
    /ERR_ABORTED|NS_BINDING_ABORTED|cancel/i,
  );
  await detailCompletion;
  await expect(
    page.getByRole("heading", { name: "账本版本 21（未批准）", exact: true }),
  ).toBeVisible();
  let queryRelease!: () => void;
  let querySettled!: () => void;
  const queryGate = new Promise<void>((resolve) => {
    queryRelease = resolve;
  });
  const queryCompletion = new Promise<void>((resolve) => {
    querySettled = resolve;
  });
  const queryCancelled = page.waitForEvent("requestfailed", {
    predicate: (request) =>
      new URL(request.url()).pathname === env.path + "/query",
  });
  await page.route("**" + env.path + "/query", async (route) => {
    const response = await route.fetch();
    await queryGate;
    try {
      await route.fulfill({ response });
    } catch {
      await queryCancelled;
    } finally {
      querySettled();
    }
  });
  await page
    .getByLabel("账本查询位置", { exact: true })
    .selectOption(env.times[0].id);
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  await page
    .getByLabel("账本查询视角", { exact: true })
    .selectOption("CHARACTER");
  queryRelease();
  const cancelledQuery = await queryCancelled;
  expect(cancelledQuery.failure()?.errorText).toMatch(
    /ERR_ABORTED|NS_BINDING_ABORTED|cancel/i,
  );
  await queryCompletion;
  await expect(
    page.getByRole("region", { name: "账本受控查询结果", exact: true }),
  ).toHaveCount(0);
});

test("SERIES-02：归档项目可查精确历史，建版与批准入口只读", async ({
  page,
}) => {
  const env = await setup(page);
  await env.save(0, null);
  const archive = await page.request.patch(
    `/api/v1/projects/${env.project.id}`,
    { headers: env.headers, data: { archived: true } },
  );
  expect(archive.status()).toBe(200);
  await env.visit();
  await expect(
    page.getByRole("heading", { name: "账本版本 1（未批准）", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "以此版本起草", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "批准此精确版本", exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("账本查询位置", { exact: true })
    .selectOption(env.times[0].id);
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "账本受控查询结果", exact: true }),
  ).toBeVisible();
});

test("SERIES-02：受控结果缩页使用next_offset继续，不跳漏并支持上一页", async ({
  page,
}) => {
  const env = await setup(page);
  const saved = await env.save(0, null);
  await env.visit();
  const requested: number[] = [];
  await page.route("**" + env.path + "/query", async (route) => {
    const body = route.request().postDataJSON();
    requested.push(body.offset);
    const offset = body.offset as number;
    const { known_by: _knowledge, ...visible } = env.content.entries[0];
    await route.fulfill({
      json: {
        version_id: saved.version.id,
        version_no: 1,
        content_sha256: saved.version.content_sha256,
        approval_id: null,
        target: env.times[0],
        perspective: "AUTHOR",
        character_id: null,
        facts: [],
        planned: [],
        proposals: [{ ...visible, value: "受控结果 " + offset }],
        total: 3,
        limit: 20,
        offset,
        returned_count: 1,
        next_offset: offset < 2 ? offset + 1 : null,
      },
    });
  });
  await page
    .getByLabel("账本查询位置", { exact: true })
    .selectOption(env.times[0].id);
  await page
    .getByRole("checkbox", { name: "单独显示待核实提议", exact: true })
    .check();
  await page.getByRole("button", { name: "查询此时点", exact: true }).click();
  const result = page.getByRole("region", {
    name: "账本受控查询结果",
    exact: true,
  });
  await expect(result).toContainText("受控结果 0");
  await page
    .getByRole("button", { name: "下一页查询结果", exact: true })
    .click();
  await expect(result).toContainText("受控结果 1");
  await page
    .getByRole("button", { name: "下一页查询结果", exact: true })
    .click();
  await expect(result).toContainText("受控结果 2");
  await expect(
    page.getByRole("button", { name: "下一页查询结果", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "上一页查询结果", exact: true })
    .click();
  await expect(result).toContainText("受控结果 1");
  await page
    .getByRole("button", { name: "上一页查询结果", exact: true })
    .click();
  await expect(result).toContainText("受控结果 0");
  await expect(
    page.getByRole("button", { name: "上一页查询结果", exact: true }),
  ).toBeDisabled();
  expect(requested).toEqual([0, 1, 2, 1, 0]);
});

for (const role of ["editor", "viewer"] as const) {
  test(`SERIES-02：真实${role}成员查看账本，批准保持拥有者权限`, async ({
    page,
  }) => {
    const env = await setup(page);
    const saved = await env.save(0, null);
    const member = await page.request.post("/api/v1/auth/register", {
      data: {
        email: `ledger-${role}-${crypto.randomUUID()}@example.com`,
        password: "ledger-member-browser-password",
        display_name: "账本成员",
      },
    });
    expect(member.status()).toBe(201);
    const identity = await member.json();
    const added = await page.request.post(
      `/api/v1/projects/${env.project.id}/members`,
      {
        headers: env.headers,
        data: { email: identity.user.email, role },
      },
    );
    expect(added.status()).toBe(201);
    await env.visit();
    await expect(
      page.getByRole("heading", { name: "账本版本 1（未批准）", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "批准此精确版本", exact: true }),
    ).toHaveCount(0);
    if (role === "editor")
      await expect(
        page.getByRole("button", { name: "以此版本起草", exact: true }),
      ).toBeVisible();
    else
      await expect(
        page.getByRole("button", { name: "以此版本起草", exact: true }),
      ).toHaveCount(0);
    const refused = await page.request.post(
      env.path + `/versions/${saved.version.id}/approve`,
      {
        headers: {
          Authorization: "Bearer " + identity.access_token,
          "Idempotency-Key": crypto.randomUUID(),
        },
        data: { expected_revision: saved.head.revision },
      },
    );
    expect(refused.status()).toBe(403);
  });
}
