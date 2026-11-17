import { test, expect, type Page } from "@playwright/test";
import type { StoryEntry } from "../src/types/api";
import type {
  LegacyOutlineContent,
  OutlineContent,
  SeriesOutlineContent,
} from "../src/features/story/series-plan";
import { openWorkspacePanels } from "./workspace-helpers";

type Outline = {
  id: string;
  version_no: number;
  status: string;
  base_version_id: string | null;
  content: OutlineContent;
  source_snapshot: {
    series_plan_references?: {
      characters: { id: string; revision: number; name: string }[];
    };
  };
};

async function openOutline(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "分集大纲", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "分集大纲", exact: true }),
  ).toBeVisible();
}

async function readEntries(
  page: Page,
  path: string,
  headers: { Authorization: string },
) {
  const rows: StoryEntry[] = [];
  for (let offset = 0; ; offset += 100) {
    const response = await page.request.get(
      `${path}?limit=100&offset=${offset}`,
      { headers },
    );
    expect(response.status()).toBe(200);
    const batch: StoryEntry[] = await response.json();
    rows.push(...batch);
    if (batch.length < 100) return rows;
  }
}

async function setup(page: Page, directoryPadding = 0) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "series-" + crypto.randomUUID() + "@example.com",
      password: "series-planning-password",
      display_name: "全剧策划验收",
    },
  });
  expect(registration.status()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "雨夜送信三集规划",
      story_settings: {
        target_episode_count: 3,
        default_episode_duration_ms: 30000,
      },
    },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: { episode_no: 1, title: "雨夜拾信" },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const path = `/api/v1/episodes/${episode.id}`;
  const charactersPath = `/api/v1/projects/${project.id}/story-entries`;
  for (let index = 0; index < directoryPadding; index++) {
    const response = await page.request.post(charactersPath, {
      headers,
      data: { kind: "WORLD", name: `已有世界设定 ${index + 1}` },
    });
    const padding: StoryEntry = await response.json();
    expect(response.status(), JSON.stringify(padding)).toBe(201);
    // Keep a real second directory page without exceeding the 100 active-entry limit.
    const archived = await page.request.patch(
      `${charactersPath}/${padding.id}`,
      {
        headers,
        data: { expected_revision: padding.revision, archived: true },
      },
    );
    expect(archived.status(), await archived.text()).toBe(200);
  }
  const characters: StoryEntry[] = [];
  for (const [name, description] of [
    ["阿青", "只求自保的年轻信使"],
    ["石岚", "守护家族秘密的旅人"],
  ]) {
    const response = await page.request.post(charactersPath, {
      headers,
      data: { kind: "CHARACTER", name, description },
    });
    const character: StoryEntry = await response.json();
    expect(response.status(), JSON.stringify(character)).toBe(201);
    characters.push(character);
  }
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  const projectEpisodesReady = page.waitForResponse(async (response) => {
    if (
      response.request().method() !== "GET" ||
      new URL(response.url()).pathname !==
        `/api/v1/projects/${project.id}/episodes` ||
      response.status() !== 200
    )
      return false;
    try {
      const rows: unknown = await response.json();
      return (
        Array.isArray(rows) &&
        rows.some(
          (row) => row?.id === episode.id && row.project_id === project.id,
        )
      );
    } catch {
      return false;
    }
  });
  const episodeCanvasReady = page.waitForResponse(async (response) => {
    if (
      response.request().method() !== "GET" ||
      new URL(response.url()).pathname !==
        `/api/v1/episodes/${episode.id}/canvas` ||
      response.status() !== 200
    )
      return false;
    try {
      const graph: unknown = await response.json();
      return (
        !!graph &&
        typeof graph === "object" &&
        "nodes" in graph &&
        Array.isArray(graph.nodes) &&
        "edges" in graph &&
        Array.isArray(graph.edges)
      );
    } catch {
      return false;
    }
  });
  await Promise.all([projectEpisodesReady, episodeCanvasReady, page.goto("/")]);
  await expect(
    page.getByRole("heading", {
      name: `第 ${episode.episode_no} 集 · ${episode.title}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator(".episode-studio")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await openOutline(page);
  await page
    .getByLabel("待规划故事", { exact: true })
    .fill("阿青雨夜捡到一封信，与石岚共同寻找收件人，决定把选择留给家人。");
  await page.getByRole("button", { name: "生成分集大纲", exact: true }).click();
  await expect(page.getByRole("button", { name: /^大纲 v1/ })).toBeVisible();
  await page.getByRole("button", { name: /^大纲 v1/ }).click();
  await expect(page.getByLabel("第3集结尾钩子", { exact: true })).toBeVisible();
  const outlines = await (
    await page.request.get(path + "/story-outlines", { headers })
  ).json();
  const legacy: Outline = await (
    await page.request.get(path + "/story-outlines/" + outlines[0].id, {
      headers,
    })
  ).json();
  const initialEntries = await readEntries(page, charactersPath, headers);
  return {
    owner,
    headers,
    project,
    episode,
    path,
    charactersPath,
    characters,
    initialEntries,
    legacy,
  };
}

type Setup = Awaited<ReturnType<typeof setup>>;

async function select(page: Page, label: string, option: string) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}

async function fillPlan(page: Page) {
  await page.getByRole("button", { name: "补充全剧规划", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "刷新可选人物", exact: true }),
  ).toBeEnabled();
  await page.getByLabel("线索1标题", { exact: true }).fill("送达失落的信");
  await page
    .getByLabel("线索1关键冲突", { exact: true })
    .fill("信中的家族秘密使收件人面临风险。");
  await page
    .getByLabel("线索1预定收束", { exact: true })
    .fill("第三集交信，由家人决定如何回应。");
  await page.getByRole("button", { name: "添加支线", exact: true }).click();
  await page.getByLabel("线索2标题", { exact: true }).fill("从戒备到合作");
  await page
    .getByLabel("线索2关键冲突", { exact: true })
    .fill("石岚怀疑阿青靠信件牟利。");
  await page
    .getByLabel("线索2预定收束", { exact: true })
    .fill("护送中的取舍建立双方信任。");
  for (const [index, name, goal, conflict, growth, change] of [
    [
      1,
      "阿青",
      "送信换取路费。",
      "害怕承担陌生人的风险。",
      "学会尊重收件人的选择。",
      "放弃卖信，主动保护收信人。",
    ],
    [
      2,
      "石岚",
      "夺回家族信件。",
      "认为保守秘密才是保护。",
      "让家人共同面对真相。",
      "决定与阿青合作护送。",
    ],
  ] as const) {
    await page
      .getByRole("button", { name: "添加人物成长", exact: true })
      .click();
    if (index === 1) {
      await page
        .getByRole("combobox", { name: "人物1角色", exact: true })
        .focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("option", { name, exact: true }),
      ).toBeVisible();
      await page.keyboard.press("Home");
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("combobox", { name: "人物1角色", exact: true }),
      ).toContainText(name);
    } else await select(page, `人物${index}角色`, name);
    await page.getByLabel(`人物${index}初始目标`, { exact: true }).fill(goal);
    await page
      .getByLabel(`人物${index}内在冲突`, { exact: true })
      .fill(conflict);
    await page.getByLabel(`人物${index}预定成长`, { exact: true }).fill(growth);
    await page
      .getByRole("button", { name: `添加人物${index}分集变化`, exact: true })
      .click();
    await page.getByLabel(`人物${index}变化1集数`, { exact: true }).fill("2");
    await page
      .getByLabel(`人物${index}变化1说明`, { exact: true })
      .fill(change);
  }
  await page
    .locator("summary")
    .filter({ hasText: /^关系发展$/ })
    .click();
  await page.getByRole("button", { name: "添加关系发展", exact: true }).click();
  await select(page, "关系1一方", "阿青");
  await select(page, "关系1另一方", "石岚");
  await page
    .getByLabel("关系1初始关系", { exact: true })
    .fill("双方互相戒备。");
  await page
    .getByLabel("关系1预定发展", { exact: true })
    .fill("通过共同护送成为坦诚合作的伙伴。");
  await page
    .getByRole("button", { name: "添加关系1分集变化", exact: true })
    .click();
  await page.getByLabel("关系1变化1集数", { exact: true }).fill("3");
  await page
    .getByLabel("关系1变化1说明", { exact: true })
    .fill("两人共同守护收信人的决定。");
  await page
    .getByLabel("伏笔1设置说明", { exact: true })
    .fill("信封红印与石岚的旧物相同。");
  await page
    .getByLabel("伏笔1预定回收说明", { exact: true })
    .fill("第三集由旧物确认信件来自失散母亲。");
  for (let episode = 1; episode <= 3; episode++) {
    await page
      .getByLabel(`第${episode}集推进1说明`, { exact: true })
      .fill(`第${episode}集推进送信主线，作出下一步选择。`);
    await page
      .getByRole("button", { name: `添加第${episode}集线索推进`, exact: true })
      .click();
    await page
      .getByLabel(`第${episode}集推进2说明`, { exact: true })
      .fill(`第${episode}集以护送中的行动改变信任。`);
    if (episode > 1) {
      await page
        .getByRole("button", {
          name: `添加第${episode}集因果来源`,
          exact: true,
        })
        .click();
      await page
        .getByLabel(`第${episode}集因果来源1集号`, { exact: true })
        .fill(String(episode - 1));
    }
    await page
      .getByLabel(`第${episode}集因果说明`, { exact: true })
      .fill(
        episode === 1
          ? "雨夜误捡信引出追查。"
          : `因第${episode - 1}集的送信决定而继续同行。`,
      );
    await page
      .getByLabel(`第${episode}集结尾承接`, { exact: true })
      .fill(
        episode === 3
          ? "信件交还家人，留下未来的选择。"
          : `线索把两人引向第${episode + 1}集的收信人。`,
      );
  }
  await expect(
    page.getByRole("button", { name: "另存大纲新版本", exact: true }),
  ).toBeEnabled();
}

function plannedContent(data: Setup): SeriesOutlineContent {
  const [first, second] = data.characters;
  return {
    ...(data.legacy.content as LegacyOutlineContent),
    schema_version: "2.0",
    series_plan: {
      schema_version: "1.0",
      kind: "PLANNED",
      threads: [
        {
          id: "letter",
          kind: "MAIN",
          title: "送达失落的信",
          central_conflict: "家族秘密威胁收信人。",
          planned_resolution: "第三集交信，由家人决定。",
        },
      ],
      character_arcs: [first, second].map((character) => ({
        character_id: character.id,
        character_revision: character.revision,
        starting_goal: "保护家人。",
        internal_conflict: "担心说出真相。",
        planned_growth: "学会共同面对秘密。",
        milestones: [{ episode_no: 2, change: "决定共同护送信件。" }],
      })),
      relationship_arcs: [
        {
          id: "trust",
          source_character_id: first.id,
          target_character_id: second.id,
          initial_relationship: "相互戒备。",
          planned_development: "逐渐信任。",
          milestones: [{ episode_no: 3, change: "共同守护收信人。" }],
        },
      ],
      foreshadowing: [
        {
          id: "seal",
          thread_id: "letter",
          setup_episode_no: 1,
          setup: "信封上有红印。",
          expected_payoff_episode_no: 3,
          planned_payoff: "红印指向失散的母亲。",
        },
      ],
      episode_progressions: [1, 2, 3].map((episode_no) => ({
        episode_no,
        thread_beats: [
          {
            thread_id: "letter",
            progression: `第${episode_no}集找到新的送信线索。`,
          },
        ],
        caused_by_episode_nos: episode_no === 1 ? [] : [episode_no - 1],
        causal_link: "之前的选择引出新的后果。",
        ending_handoff: "沿着线索继续前进。",
      })),
    },
  };
}

async function seedPlanned(page: Page, data: Setup) {
  const response = await page.request.post(data.path + "/story-outlines", {
    headers: { ...data.headers, "Idempotency-Key": crypto.randomUUID() },
    data: { base_version_id: data.legacy.id, content: plannedContent(data) },
  });
  expect(response.status()).toBe(201);
  const planned: Outline = await response.json();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await openOutline(page);
  await page.getByRole("button", { name: /^大纲 v2/ }).click();
  await expect(page.getByLabel("线索1标题", { exact: true })).toHaveValue(
    "送达失落的信",
  );
  return planned;
}

async function reloadVersion(page: Page, version: number) {
  page.once("dialog", (dialog) => void dialog.accept());
  await page.reload();
  await openOutline(page);
  await page
    .getByRole("button", { name: new RegExp(`^大纲 v${version} ·`) })
    .click();
}

test("SERIES-01 plans three episodes, confirms an exact version and revises only a planned payoff", async ({
  page,
}, testInfo) => {
  const characterPageOffsets: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith("/story-entries") && request.method() === "GET")
      characterPageOffsets.push(url.searchParams.get("offset") ?? "");
  });
  const data = await setup(page, 100);
  expect(data.legacy.content.schema_version).toBe("1.0");
  expect(data.legacy.content).not.toHaveProperty("series_plan");
  await fillPlan(page);
  expect(characterPageOffsets).toContain("100");
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
  const rows = await (
    await page.request.get(data.path + "/story-outlines", {
      headers: data.headers,
    })
  ).json();
  const approved: Outline = await (
    await page.request.get(data.path + "/story-outlines/" + rows[0].id, {
      headers: data.headers,
    })
  ).json();
  expect(approved.status).toBe("APPROVED");
  expect(approved.base_version_id).toBe(data.legacy.id);
  expect(approved.content.schema_version).toBe("2.0");
  if (approved.content.schema_version !== "2.0")
    throw new Error("Series plan was not persisted");
  expect(approved.content.episodes).toEqual(data.legacy.content.episodes);
  expect(approved.content.series_plan.kind).toBe("PLANNED");
  expect(
    approved.content.series_plan.threads.map((thread) => thread.kind),
  ).toEqual(["MAIN", "SUBPLOT"]);
  expect(
    approved.content.series_plan.episode_progressions.map(
      (episode) => episode.caused_by_episode_nos,
    ),
  ).toEqual([[], [1], [2]]);
  expect(approved.content.series_plan.foreshadowing[0]).toMatchObject({
    setup_episode_no: 1,
    expected_payoff_episode_no: 3,
  });
  expect(
    approved.source_snapshot.series_plan_references?.characters.map(
      ({ id, revision, name }) => ({ id, revision, name }),
    ),
  ).toEqual(
    data.characters.map(({ id, revision, name }) => ({ id, revision, name })),
  );
  expect(await readEntries(page, data.charactersPath, data.headers)).toEqual(
    data.initialEntries,
  );
  expect(
    await (
      await page.request.get(data.path + "/story-outlines/" + data.legacy.id, {
        headers: data.headers,
      })
    ).json(),
  ).toEqual(data.legacy);
  await expect(
    page.getByText(
      "以下内容是预定规划，尚非已发生事实。伏笔的预计回收也不表示已经完成。",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .locator('section[aria-label="全剧规划"]')
    .screenshot({ path: testInfo.outputPath("series-plan-desktop.png") });
  const changedPayoff = "第三集将红印与旧物并置，请石岚自行辨认母亲的笔迹。";
  await page
    .getByLabel("伏笔1预定回收说明", { exact: true })
    .fill(changedPayoff);
  await page
    .getByRole("button", { name: "另存大纲新版本", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "大纲 v3", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认此大纲版本", exact: true }),
  ).toBeEnabled();
  const revisedRows = await (
    await page.request.get(data.path + "/story-outlines", {
      headers: data.headers,
    })
  ).json();
  const revised: Outline = await (
    await page.request.get(data.path + "/story-outlines/" + revisedRows[0].id, {
      headers: data.headers,
    })
  ).json();
  const expected = structuredClone(approved.content);
  expected.series_plan.foreshadowing[0].planned_payoff = changedPayoff;
  expect(revised.content).toEqual(expected);
  expect(revised.status).toBe("DRAFT");
  expect(revised.base_version_id).toBe(approved.id);
  expect(
    await (
      await page.request.get(data.path + "/story-outlines/" + approved.id, {
        headers: data.headers,
      })
    ).json(),
  ).toEqual(approved);
});

test("SERIES-01 retains an incomplete planning draft across refresh and rejects corrupted nested draft data", async ({
  page,
}) => {
  const data = await setup(page);
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  await page.getByRole("button", { name: "补充全剧规划", exact: true }).click();
  await page.getByLabel("线索1标题", { exact: true }).fill("尚未写完的主线");
  await page.getByRole("button", { name: "添加人物成长", exact: true }).click();
  await page
    .getByLabel("人物1初始目标", { exact: true })
    .fill("先找到信件失主，人物暂未选择。");
  await reloadVersion(page, 1);
  await expect(page.getByLabel("线索1标题", { exact: true })).toHaveValue(
    "尚未写完的主线",
  );
  await expect(page.getByLabel("线索1关键冲突", { exact: true })).toHaveValue(
    "",
  );
  await expect(page.getByLabel("人物1初始目标", { exact: true })).toHaveValue(
    "先找到信件失主，人物暂未选择。",
  );
  await expect(
    page.getByRole("combobox", { name: "人物1角色", exact: true }),
  ).toContainText("选择故事设定中的人物");
  await expect(
    page.getByRole("button", { name: "另存大纲新版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("已恢复此精确版本的本机草稿。", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toHaveCount(0);
  await page.evaluate(
    ({ userId, episodeId, versionId }) => {
      const storageKey = `arclight:drafts:v1:${userId}`;
      const drafts = JSON.parse(localStorage.getItem(storageKey)!);
      drafts[
        JSON.stringify([episodeId, "outline", versionId])
      ].value.content.series_plan.episode_progressions[0].thread_beats = [null];
      localStorage.setItem(storageKey, JSON.stringify(drafts));
    },
    {
      userId: data.owner.user.id,
      episodeId: data.episode.id,
      versionId: data.legacy.id,
    },
  );
  await reloadVersion(page, 1);
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("本机草稿已损坏");
  await expect(
    page.getByText("此版本未记录全剧规划。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("第1集目标", { exact: true })).toHaveValue(
    data.legacy.content.episodes[0].goal,
  );
  expect(crashes).toEqual([]);
});

test("SERIES-01 restores a full draft and retries a committed save with the same body after a character directory refresh", async ({
  page,
}) => {
  const data = await setup(page);
  const planned = await seedPlanned(page, data);
  await page
    .getByLabel("线索1标题", { exact: true })
    .fill("本机恢复的送信主线");
  await reloadVersion(page, 2);
  await expect(page.getByLabel("线索1标题", { exact: true })).toHaveValue(
    "本机恢复的送信主线",
  );
  await expect(page.getByLabel("第3集结尾承接", { exact: true })).toHaveValue(
    "沿着线索继续前进。",
  );
  const keys: string[] = [];
  const bodies: unknown[] = [];
  let once = true;
  await page.route("**/story-outlines", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    bodies.push(route.request().postDataJSON());
    const response = await route.fetch();
    if (once) {
      once = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  try {
    await page
      .getByRole("button", { name: "另存大纲新版本", exact: true })
      .click();
    await expect(
      page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
    ).toBeVisible();
    const first = data.characters[0];
    const changed = await page.request.put(
      data.charactersPath + "/" + first.id,
      {
        headers: data.headers,
        data: {
          kind: first.kind,
          name: "阿青的新设定",
          description: first.description,
          rules: first.rules,
          relations: first.relations,
          asset: first.asset,
          expected_revision: first.revision,
        },
      },
    );
    expect(changed.status()).toBe(200);
    const currentCharacter: StoryEntry = await changed.json();
    await page
      .getByRole("button", { name: "刷新可选人物", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "人物1采用当前设定", exact: true }),
    ).toBeEnabled();
    await expect(
      page.getByText(`规划引用 阿青 的设定修订 ${first.revision}`, {
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "另存大纲新版本", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "大纲 v3", exact: true }),
    ).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).toBe(keys[1]);
    expect(bodies[0]).toEqual(bodies[1]);
    const rows = await (
      await page.request.get(data.path + "/story-outlines", {
        headers: data.headers,
      })
    ).json();
    expect(rows).toHaveLength(3);
    const saved: Outline = await (
      await page.request.get(data.path + "/story-outlines/" + rows[0].id, {
        headers: data.headers,
      })
    ).json();
    expect(saved.base_version_id).toBe(planned.id);
    expect(
      saved.source_snapshot.series_plan_references?.characters[0],
    ).toMatchObject({ name: first.name, revision: first.revision });
    await page
      .getByRole("button", { name: "人物1采用当前设定", exact: true })
      .click();
    await expect(
      page.getByText(
        `规划引用 阿青的新设定 的设定修订 ${currentCharacter.revision}`,
        { exact: true },
      ),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "另存大纲新版本", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "大纲 v4", exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "确认此大纲版本", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "从此大纲编写第1集剧本", exact: true }),
    ).toBeEnabled();
    const currentRows = await (
      await page.request.get(data.path + "/story-outlines", {
        headers: data.headers,
      })
    ).json();
    const adopted: Outline = await (
      await page.request.get(
        data.path + "/story-outlines/" + currentRows[0].id,
        { headers: data.headers },
      )
    ).json();
    expect(
      adopted.source_snapshot.series_plan_references?.characters[0],
    ).toMatchObject({
      name: currentCharacter.name,
      revision: currentCharacter.revision,
    });
    expect(
      await (
        await page.request.get(data.path + "/story-outlines/" + saved.id, {
          headers: data.headers,
        })
      ).json(),
    ).toEqual(saved);
  } finally {
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("SERIES-01 mobile Viewer reads approved planning with frozen character names and disabled editing", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const data = await setup(page);
  const planned = await seedPlanned(page, data);
  const confirmation = await page.request.post(
    data.path + "/story-outlines/" + planned.id + "/confirm",
    { headers: data.headers, data: {} },
  );
  expect(confirmation.status()).toBe(200);
  const first = data.characters[0];
  const renamed = await page.request.put(data.charactersPath + "/" + first.id, {
    headers: data.headers,
    data: {
      kind: first.kind,
      name: "已经改名的人物",
      description: first.description,
      rules: first.rules,
      relations: first.relations,
      asset: first.asset,
      expected_revision: first.revision,
    },
  });
  expect(renamed.status()).toBe(200);
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "series-viewer-" + crypto.randomUUID() + "@example.com",
      password: "series-viewer-password",
      display_name: "全剧规划阅读者",
    },
  });
  expect(registration.status()).toBe(201);
  const viewer = await registration.json();
  const membership = await page.request.post(
    `/api/v1/projects/${data.project.id}/members`,
    {
      headers: data.headers,
      data: { email: viewer.user.email, role: "viewer" },
    },
  );
  expect(membership.status()).toBe(201);
  await reloadVersion(page, 2);
  await expect(
    page.getByText("当前为只读模式，可查看项目内的历史大纲。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "人物1角色", exact: true }),
  ).toContainText("阿青");
  await expect(
    page.getByRole("combobox", { name: "人物1角色", exact: true }),
  ).not.toContainText("已经改名的人物");
  await expect(
    page.getByText(`规划引用 阿青 的设定修订 ${first.revision}`, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "人物1采用当前设定", exact: true }),
  ).toHaveCount(0);
  for (const label of [
    "线索1标题",
    "人物1初始目标",
    "伏笔1预定回收说明",
    "第3集结尾承接",
  ]) {
    await expect(page.getByLabel(label, { exact: true })).toBeDisabled();
  }
  await expect(
    page.getByRole("combobox", { name: "人物1角色", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "另存大纲新版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "从此大纲编写第1集剧本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "刷新可选人物", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("正在读取项目人物…", { exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page
    .locator('section[aria-label="全剧规划"]')
    .screenshot({ path: testInfo.outputPath("series-plan-mobile-viewer.png") });
  const forbidden = await page.request.post(data.path + "/story-outlines", {
    headers: {
      Authorization: "Bearer " + viewer.access_token,
      "Idempotency-Key": crypto.randomUUID(),
    },
    data: { base_version_id: planned.id, content: plannedContent(data) },
  });
  expect(forbidden.status()).toBe(403);
});
