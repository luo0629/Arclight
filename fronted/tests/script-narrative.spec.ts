import { test, expect, type Page } from "@playwright/test";
import type {
  LegacyOutlineContent,
  SeriesOutlineContent,
} from "../src/features/story/series-plan";
import { openWorkspacePanels } from "./workspace-helpers";

type Character = { id: string; name: string; revision: number };

async function setup(page: Page, count = 3) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `narrative-${crypto.randomUUID()}@example.com`,
      password: "narrative-test-password",
      display_name: "连续剧交付验收",
    },
  });
  expect(registration.status(), await registration.text()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: `Bearer ${owner.access_token}` };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "红信的远集因果",
      story_settings: {
        target_episode_count: count,
        default_episode_duration_ms: 30000,
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const project = await created.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "渡口接信" } },
  );
  expect(episodeResponse.status(), await episodeResponse.text()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const ledger = `/api/v1/projects/${project.id}/story-ledger`;
  const characters: Character[] = [];
  for (const name of ["阿青", "石岚"]) {
    const response = await page.request.post(
      `/api/v1/projects/${project.id}/story-entries`,
      {
        headers,
        data: { kind: "CHARACTER", name, description: "在渡口护送信件的旅人" },
      },
    );
    expect(response.status(), await response.text()).toBe(201);
    characters.push(await response.json());
  }
  // Prepare the real approved planning contract through the existing generation/save/confirm chain.
  const queued = await page.request.post(prefix + "/story-outline-runs", {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: {
      story: "阿青在第一集接过红信，石岚尚未知情；之后沿信件线索寻找失主。",
      target_episode_count: count,
      episode_duration_ms: 30000,
    },
  });
  expect(queued.status(), await queued.text()).toBe(202);
  let outlineId = "";
  await expect(async () => {
    const rows: { id: string }[] = await (
      await page.request.get(prefix + "/story-outlines", { headers })
    ).json();
    expect(rows).toHaveLength(1);
    outlineId = rows[0].id;
  }).toPass();
  const legacy: { id: string; content: LegacyOutlineContent } = await (
    await page.request.get(prefix + "/story-outlines/" + outlineId, { headers })
  ).json();
  const content: SeriesOutlineContent = {
    ...legacy.content,
    schema_version: "2.0",
    series_plan: {
      schema_version: "1.0",
      kind: "PLANNED",
      threads: [
        {
          id: "letter",
          kind: "MAIN",
          title: "红信归途",
          central_conflict: "公开秘密可能伤害家人。",
          planned_resolution: "收信人自己作出决定。",
        },
      ],
      character_arcs: characters.map((character) => ({
        character_id: character.id,
        character_revision: character.revision,
        starting_goal: "保护家人。",
        internal_conflict: "害怕秘密被发现。",
        planned_growth: "学会尊重彼此选择。",
        milestones: [{ episode_no: count, change: "共同承担选择的后果。" }],
      })),
      relationship_arcs: [],
      foreshadowing: [
        {
          id: "seal",
          thread_id: "letter",
          setup_episode_no: 1,
          setup: "红信的封口留有一枚旧印。",
          expected_payoff_episode_no: count,
          planned_payoff: "旧印成为寻找失主的线索。",
        },
      ],
      episode_progressions: Array.from({ length: count }, (_, index) => ({
        episode_no: index + 1,
        thread_beats: [
          { thread_id: "letter", progression: "沿红信线索继续调查。" },
        ],
        caused_by_episode_nos: index === 0 ? [] : [1],
        causal_link:
          index === 0
            ? "渡口接信引出后果。"
            : "第一集接信的决定带来当前的追查。",
        ending_handoff: "留下下一步选择。",
      })),
    },
  };
  const savedPlan = await page.request.post(prefix + "/story-outlines", {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: { base_version_id: legacy.id, content },
  });
  expect(savedPlan.status(), await savedPlan.text()).toBe(201);
  const plan = await savedPlan.json();
  const confirmed = await page.request.post(
    prefix + `/story-outlines/${plan.id}/confirm`,
    { headers, data: {} },
  );
  expect(confirmed.status(), await confirmed.text()).toBe(200);
  const legacyResponse = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "渡口旧稿",
        summary: "阿青接信，石岚仍不知道秘密。",
        estimated_duration_ms: 30000,
        characters: characters.map((row) => row.name),
        scenes: [
          {
            scene_no: 1,
            title: "雨夜渡口",
            action: "阿青护住红信，示意同行。",
            dialogue: [{ speaker: "阿青", text: "先过河，我会向你解释。" }],
          },
        ],
      },
    },
  });
  expect(legacyResponse.status(), await legacyResponse.text()).toBe(201);
  const source = await legacyResponse.json();
  return {
    owner,
    project,
    episode,
    prefix,
    ledger,
    headers,
    characters,
    plan,
    source,
  };
}
type Case = Awaited<ReturnType<typeof setup>>;

async function openScript(
  page: Page,
  env: Case,
  version: { id: string; version_no: number },
) {
  await page.goto(
    `/projects/${env.project.id}/episodes/${env.episode.id}?view=list&node=script:${version.id}`,
  );
  await openWorkspacePanels(page);
  await page
    .locator(".version-row")
    .filter({ hasText: `版本 ${version.version_no}` })
    .click();
  await expect(
    page.getByRole("heading", {
      name: `剧本 v${version.version_no}`,
      exact: true,
    }),
  ).toBeVisible();
}

async function capture(page: Page, env: Case, time = 10) {
  const response = await page.request.post(env.prefix + "/writing-context", {
    headers: env.headers,
    data: {
      series_plan_version_id: env.plan.id,
      ledger_version_id: null,
      expected_ledger_head_revision: 0,
      target_story_time: time,
      target_scene_no: 1,
      character_ids: env.characters.map((row) => row.id),
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).context;
}

async function selectWritingCharacters(page: Page, env: Case) {
  const inputs = page.getByRole("region", {
    name: "连续剧写作依据",
    exact: true,
  });
  await inputs
    .getByRole("button", { name: "加载更多写作角色", exact: true })
    .click();
  const scope = inputs.getByRole("group", {
    name: "需要隔离知情范围的故事角色",
    exact: true,
  });
  for (const character of env.characters) {
    const checkbox = scope.getByRole("checkbox", {
      name: character.name,
      exact: true,
    });
    await checkbox.check();
    await expect(checkbox).toBeChecked();
  }
}

function narrativeContent(env: Case, context: Record<string, unknown>) {
  const scene = structuredClone(env.source.content_json.scenes[0]);
  return {
    ...env.source.content_json,
    schema_version: "3.0",
    narrative_context: context,
    scenes: [
      {
        ...scene,
        dramatic_beat: {
          story_time: context.target_story_time,
          character_objectives: env.characters.map((row) => ({
            character_id: row.id,
            purpose: "护送红信。",
            emotion_start: "戒备",
            emotion_end: "愿意暂时合作",
          })),
          conflict: "石岚要看信，阿青要求先过河。",
          dialogue_interactions: [
            {
              line_ids: scene.dialogue.map(
                (line: { line_id: string }) => line.line_id,
              ),
              character_ids: env.characters.map((row) => row.id),
              action: "阿青伸手示意，石岚放下阻拦的手。",
            },
          ],
        },
        state_changes: [
          {
            id: crypto.randomUUID(),
            kind: "EVENT",
            subject_id: env.characters[0].id,
            target_id: null,
            state_key: "received-letter",
            value: "阿青接过红信",
            story_start: context.target_story_time,
            story_end: null,
            known_by: [
              {
                character_id: env.characters[0].id,
                story_start: context.target_story_time,
                story_end: null,
              },
            ],
            causes: [],
            supersedes_entry_id: null,
          },
        ],
      },
    ],
  };
}

async function approve(page: Page, env: Case, versionId: string) {
  const path = env.prefix + `/scripts/${versionId}`;
  const impact = await page.request.get(path + "/dependency-impact", {
    headers: env.headers,
  });
  expect(impact.status(), await impact.text()).toBe(200);
  const response = await page.request.post(path + "/review", {
    headers: env.headers,
    data: {
      decision: "APPROVE",
      impact_digest: (await impact.json()).impact_digest,
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).story_state_receipt;
}

test("explicitly upgrades a manual draft and atomically approves its exact narrative and facts", async ({
  page,
}, info) => {
  const env = await setup(page);
  await openScript(page, env, env.source);
  await expect(
    page.getByRole("button", { name: "升级为连续剧剧本", exact: true }),
  ).toHaveCount(0);
  expect(env.source.content_json.schema_version).toBe("2.0");
  expect(env.source.content_json).not.toHaveProperty("narrative_context");
  await page
    .getByRole("button", { name: "局部改写此版本", exact: true })
    .click();
  const rewrite = page.getByRole("dialog", {
    name: "局部改写 · 剧本 v1",
    exact: true,
  });
  await rewrite
    .getByRole("radio", { name: "调查、规划与评价改写", exact: true })
    .click();
  await expect(
    rewrite.getByRole("button", {
      name: "使用全剧规划与时点事实",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(rewrite).toContainText("先将此旧版剧本人工另存并显式升级");
  await page.keyboard.press("Escape");
  await expect(rewrite).toHaveCount(0);
  await page
    .getByRole("button", { name: "编辑并另存新版本", exact: true })
    .click();
  await page
    .getByRole("button", { name: "升级为连续剧剧本", exact: true })
    .click();
  await page
    .getByLabel("已批准全剧规划版本", { exact: true })
    .fill(env.plan.id);
  await page.getByLabel("写作目标故事时点", { exact: true }).fill("10");
  await selectWritingCharacters(page, env);
  const delivery = page.getByRole("region", {
    name: "连续剧场景交付",
    exact: true,
  });
  await delivery
    .getByRole("button", { name: "加载更多叙事故事设定", exact: true })
    .click();
  await page.getByLabel("场景故事时点", { exact: true }).fill("10");
  await page
    .getByLabel("场景关键冲突", { exact: true })
    .fill("石岚要看信，阿青要求先过河。");
  await page.getByRole("button", { name: "添加人物目的", exact: true }).click();
  const sceneCharacter = delivery.getByRole("combobox", {
    name: "场景人物",
    exact: true,
  });
  for (const character of env.characters) {
    await expect(
      sceneCharacter.locator(`option[value="${character.id}"]`),
    ).toHaveText(character.name);
  }
  await sceneCharacter.selectOption(env.characters[0].id);
  await page
    .getByLabel("人物目的", { exact: true })
    .fill("保护红信并争取协助。");
  await page.getByLabel("起始情绪", { exact: true }).fill("戒备");
  await page.getByLabel("结束情绪", { exact: true }).fill("愿意合作");
  await page.getByRole("button", { name: "添加对白互动", exact: true }).click();
  const interaction = page.getByRole("group", {
    name: "对白与行动互动 1",
    exact: true,
  });
  await interaction
    .getByLabel("互动行动与反应", { exact: true })
    .fill("阿青伸手示意同行，石岚放下阻拦的手。");
  await interaction
    .getByRole("checkbox", {
      name: "阿青：先过河，我会向你解释。",
      exact: true,
    })
    .check();
  await interaction
    .getByRole("checkbox", { name: "阿青", exact: true })
    .check();
  await page
    .getByRole("button", { name: "添加状态变更提案", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "状态主体", exact: true })
    .selectOption(env.characters[0].id);
  await page.getByLabel("状态名称", { exact: true }).fill("received-letter");
  await page
    .getByLabel("拟确认的状态变化", { exact: true })
    .fill("阿青接过红信");
  await page
    .getByRole("button", { name: "添加提案知情角色", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "知情角色", exact: true })
    .selectOption(env.characters[0].id);
  await page
    .getByRole("button", { name: "预览精确写作依据", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "写作依据预览", exact: true }),
  ).toContainText("作者计划与已确认事实");
  await page.screenshot({
    path: info.outputPath("manual-narrative-desktop.png"),
    fullPage: true,
  });
  const savedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === env.prefix + "/scripts",
  );
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  const response = await savedResponse;
  expect(response.status(), await response.text()).toBe(201);
  const saved = await response.json();
  expect(saved.content_json.schema_version).toBe("3.0");
  expect(saved.content_json.narrative_context.episode_id).toBe(env.episode.id);
  expect(saved.content_json).not.toHaveProperty("_narrative_request");
  await expect(
    page.getByRole("heading", { name: "剧本 v2", exact: true }),
  ).toBeVisible();
  expect(
    (
      await (
        await page.request.get(env.ledger, { headers: env.headers })
      ).json()
    ).revision,
  ).toBe(0);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(
    page
      .getByRole("region", { name: "连续剧绑定与状态提案", exact: true })
      .first(),
  ).toContainText("阿青接过红信");
  await page.getByRole("button", { name: "批准该版本", exact: true }).click();
  const receipt = page.getByRole("region", {
    name: "本次剧本批准回执",
    exact: true,
  });
  await expect(receipt).toContainText("账本修订 1");
  await expect(receipt).not.toContainText(saved.id);
  const head = await (
    await page.request.get(env.ledger, { headers: env.headers })
  ).json();
  const ledgerVersion = await (
    await page.request.get(
      env.ledger + `/versions/${head.approved_version_id}`,
      { headers: env.headers },
    )
  ).json();
  expect(ledgerVersion.content.schema_version).toBe("2.0");
  expect(ledgerVersion.content.entries[0]).toMatchObject({
    status: "CONFIRMED",
    value: "阿青接过红信",
    source_script_version_id: saved.id,
    source_scene_id: saved.content_json.scenes[0].scene_id,
  });
  const old = await (
    await page.request.get(env.prefix + `/scripts/${env.source.id}`, {
      headers: env.headers,
    })
  ).json();
  expect(old.content_json).toEqual(env.source.content_json);
  await page.setViewportSize({ width: 390, height: 844 });
  await receipt.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("manual-narrative-mobile-receipt.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("keeps a conflicting candidate unapproved and exposes historical narrative only to viewers", async ({
  page,
}) => {
  const env = await setup(page);
  const context = await capture(page, env);
  const firstResponse = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 1,
      base_version_id: env.source.id,
      content: narrativeContent(env, context),
    },
  });
  expect(firstResponse.status(), await firstResponse.text()).toBe(201);
  const first = await firstResponse.json();
  const secondResponse = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 2,
      base_version_id: first.id,
      content: narrativeContent(env, context),
    },
  });
  expect(secondResponse.status(), await secondResponse.text()).toBe(201);
  const second = await secondResponse.json();
  await approve(page, env, first.id);
  await openScript(page, env, second);
  const review = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        env.prefix + `/scripts/${second.id}/review`,
  );
  await page.getByRole("button", { name: "批准该版本", exact: true }).click();
  const reviewResponse = await review;
  expect(reviewResponse.status(), await reviewResponse.text()).toBe(409);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: "批准该版本", exact: true }),
  ).toBeEnabled();
  const unchanged = await (
    await page.request.get(env.prefix + `/scripts/${second.id}`, {
      headers: env.headers,
    })
  ).json();
  expect(unchanged.status).toBe("READY_FOR_REVIEW");
  expect(
    (
      await (
        await page.request.get(env.ledger, { headers: env.headers })
      ).json()
    ).revision,
  ).toBe(1);
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `narrative-viewer-${crypto.randomUUID()}@example.com`,
      password: "narrative-viewer-password",
      display_name: "连续剧阅读者",
    },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  const viewer = await registered.json();
  const membership = await page.request.post(
    `/api/v1/projects/${env.project.id}/members`,
    {
      headers: env.headers,
      data: { email: viewer.user.email, role: "viewer" },
    },
  );
  expect(membership.status(), await membership.text()).toBe(201);
  await openScript(page, env, second);
  await expect(
    page.getByRole("region", { name: "连续剧绑定与状态提案", exact: true }),
  ).toContainText("阿青接过红信");
  for (const name of ["编辑并另存新版本", "批准该版本", "升级为连续剧剧本"])
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
  const denied = await page.request.post(
    env.prefix + `/scripts/${second.id}/review`,
    {
      headers: { Authorization: `Bearer ${viewer.access_token}` },
      data: { decision: "APPROVE" },
    },
  );
  expect(denied.status(), await denied.text()).toBe(403);
});

test("previews distant approved causality with separate character knowledge and runs the series writer", async ({
  page,
}, info) => {
  const env = await setup(page, 7);
  const firstContent = narrativeContent(env, await capture(page, env));
  const savedResponse = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 1,
      base_version_id: env.source.id,
      content: firstContent,
    },
  });
  expect(savedResponse.status(), await savedResponse.text()).toBe(201);
  const first = await savedResponse.json();
  const receipt = await approve(page, env, first.id);
  const created = await page.request.post(
    `/api/v1/projects/${env.project.id}/episodes`,
    { headers: env.headers, data: { episode_no: 7, title: "红信的后果" } },
  );
  expect(created.status(), await created.text()).toBe(201);
  const seventh = await created.json();
  const seventhPrefix = `/api/v1/episodes/${seventh.id}`;
  await page.goto(
    `/projects/${env.project.id}/episodes/${seventh.id}?view=create`,
  );
  await openWorkspacePanels(page);
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill(
      "series03-distant-cause：第一集阿青接过红信的后果在第七集显现，石岚仍不知道信中秘密。",
    );
  await page
    .getByRole("radio", { name: "调查、规划与分段创作", exact: true })
    .click();
  await page
    .getByRole("button", { name: "使用全剧规划与时点事实", exact: true })
    .click();
  await page
    .getByLabel("已批准全剧规划版本", { exact: true })
    .fill(env.plan.id);
  await page
    .getByRole("button", { name: "读取当前批准账本基准", exact: true })
    .click();
  await expect(
    page.getByLabel("已批准账本版本（尚无批准账本时留空）", { exact: true }),
  ).toHaveValue(receipt.approved_version_id);
  await page.getByLabel("写作目标故事时点", { exact: true }).fill("70");
  await selectWritingCharacters(page, env);
  await expect(
    page.getByRole("radio", { name: "调查、规划与逐场景拆镜", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "预览精确写作依据", exact: true })
    .click();
  const preview = page.getByRole("region", {
    name: "写作依据预览",
    exact: true,
  });
  await expect(preview).toContainText("第 1 集因果摘要");
  const firstKnowledge = preview.locator("details").filter({
    has: page.locator("summary").filter({ hasText: /^角色所知 · 阿青$/ }),
  });
  const secondKnowledge = preview.locator("details").filter({
    has: page.locator("summary").filter({ hasText: /^角色所知 · 石岚$/ }),
  });
  await firstKnowledge.locator("summary").first().click();
  await expect(firstKnowledge).toContainText("阿青接过红信");
  await secondKnowledge.locator("summary").first().click();
  await expect(secondKnowledge).not.toContainText("阿青接过红信");
  await page.screenshot({
    path: info.outputPath("distant-cause-author-and-knowledge.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "批准并继续", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  const runs = await (
    await page.request.get(seventhPrefix + "/runs", { headers: env.headers })
  ).json();
  expect(runs[0].workflow_version).toBe("screenwriter-series/1.0");
  const rows = await (
    await page.request.get(seventhPrefix + "/scripts", { headers: env.headers })
  ).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].content_json.schema_version).toBe("3.0");
  expect(rows[0].content_json.narrative_context).toMatchObject({
    episode_id: seventh.id,
    series_plan_version_id: env.plan.id,
    ledger_version_id: receipt.approved_version_id,
    target_story_time: 70,
  });
  await expect(
    page.getByRole("region", { name: "连续剧绑定与状态提案", exact: true }),
  ).toContainText("状态提案");
  await page.getByRole("button", { name: "批准并继续", exact: true }).click();
  await expect(async () => {
    const head = await (
      await page.request.get(env.ledger, { headers: env.headers })
    ).json();
    expect(head.revision).toBe(2);
  }).toPass();
});
