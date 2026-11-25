import { expect, test, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";
import { approvedProp } from "./asset-reference-helpers";
import type { ShotPerformance } from "../src/features/editor/shot-performance";

async function setup(
  page: Page,
  { dramatic = true, series = false, withProp = false } = {},
) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `performance-${crypto.randomUUID()}@example.test`,
      password: "series-performance-test",
      display_name: "表演分镜验收",
    },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "门口的冲突与反应",
      ...(series
        ? {
            story_settings: {
              target_episode_count: 3,
              default_episode_duration_ms: 30000,
            },
          }
        : {}),
    },
  });
  expect(projectResponse.status(), await projectResponse.text()).toBe(201);
  const project = await projectResponse.json();
  const prop = withProp
    ? await approvedProp(page, headers, project.id, "门口的旧钥匙")
    : null;
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: { episode_no: 1, title: "留下真相", target_duration_ms: 30000 },
    },
  );
  expect(episodeResponse.status(), await episodeResponse.text()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const scriptResponse = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "冲突的精确来源",
        summary: "林岚追问，周野停下并回应。",
        characters: dramatic ? ["林岚", "周野"] : ["林岚"],
        estimated_duration_ms: 30000,
        scenes: [
          {
            scene_id: crypto.randomUUID(),
            scene_no: 1,
            title: "门口",
            props: prop ? [prop] : [],
            action: dramatic
              ? "林岚挡住去路，周野停步回应。"
              : "门口的静态空镜。",
            dialogue: dramatic
              ? [
                  {
                    line_id: crypto.randomUUID(),
                    speaker: "林岚",
                    text: "你不能再隐瞒。",
                    line_type: "dialogue",
                    emotion: "急切",
                  },
                  {
                    line_id: crypto.randomUUID(),
                    speaker: "周野",
                    text: "我会告诉你真相。",
                    line_type: "dialogue",
                    emotion: "犹豫后坚定",
                  },
                  {
                    line_id: crypto.randomUUID(),
                    speaker: "旁白",
                    text: "她终于停下脚步。",
                    line_type: "narration",
                    emotion: "平静",
                  },
                ]
              : [],
          },
        ],
      },
    },
  });
  expect(scriptResponse.status(), await scriptResponse.text()).toBe(201);
  const source = await scriptResponse.json();
  const approved = await page.request.post(
    prefix + `/scripts/${source.id}/review`,
    { headers, data: { decision: "APPROVE" } },
  );
  expect(approved.status(), await approved.text()).toBe(200);
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  async function openVersion(id: string) {
    await page.goto(`/projects/${project.id}/episodes/${episode.id}`);
    await openWorkspacePanels(page);
    await page.getByRole("radio", { name: "版本列表", exact: true }).click();
    await page.locator(`[data-node-id$=":${id}"]`).click();
  }
  return {
    session,
    headers,
    project,
    episode,
    prefix,
    source,
    openVersion,
    prop,
  };
}

async function approvedNarrativeSource(
  page: Page,
  env: Awaited<ReturnType<typeof setup>>,
) {
  const characters: { id: string; name: string; revision: number }[] = [];
  for (const name of ["林岚", "周野"]) {
    const response = await page.request.post(
      `/api/v1/projects/${env.project.id}/story-entries`,
      {
        headers: env.headers,
        data: {
          kind: "CHARACTER",
          name,
          description: "在门口面对真相的故事人物",
        },
      },
    );
    expect(response.status(), await response.text()).toBe(201);
    characters.push(await response.json());
  }
  const queued = await page.request.post(env.prefix + "/story-outline-runs", {
    headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
    data: {
      story: "林岚追问被隐瞒的真相，周野决定面对并回应。",
      target_episode_count: 3,
      episode_duration_ms: 30000,
    },
  });
  expect(queued.status(), await queued.text()).toBe(202);
  let outlineId = "";
  await expect(async () => {
    const rows: { id: string }[] = await (
      await page.request.get(env.prefix + "/story-outlines", {
        headers: env.headers,
      })
    ).json();
    expect(rows).toHaveLength(1);
    outlineId = rows[0].id;
  }).toPass();
  const legacy = await (
    await page.request.get(env.prefix + "/story-outlines/" + outlineId, {
      headers: env.headers,
    })
  ).json();
  const savedPlan = await page.request.post(env.prefix + "/story-outlines", {
    headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
    data: {
      base_version_id: legacy.id,
      content: {
        ...legacy.content,
        schema_version: "2.0",
        series_plan: {
          schema_version: "1.0",
          kind: "PLANNED",
          threads: [
            {
              id: "truth",
              kind: "MAIN",
              title: "面对真相",
              central_conflict: "隐瞒破坏了信任",
              planned_resolution: "共同承担真相的后果",
            },
          ],
          character_arcs: characters.map((character) => ({
            character_id: character.id,
            character_revision: character.revision,
            starting_goal: "保护自己在意的人",
            internal_conflict: "害怕失去信任",
            planned_growth: "学会坦诚面对",
            milestones: [{ episode_no: 3, change: "一起承担决定的后果" }],
          })),
          relationship_arcs: [],
          foreshadowing: [
            {
              id: "door",
              thread_id: "truth",
              setup_episode_no: 1,
              setup: "门口的承诺留下疑问",
              expected_payoff_episode_no: 3,
              planned_payoff: "承诺获得验证",
            },
          ],
          episode_progressions: [1, 2, 3].map((episode_no) => ({
            episode_no,
            thread_beats: [
              { thread_id: "truth", progression: "逐步面对秘密的后果" },
            ],
            caused_by_episode_nos: episode_no === 1 ? [] : [1],
            causal_link:
              episode_no === 1
                ? "门口追问引出承诺"
                : "第一集的承诺带来后续选择",
            ending_handoff: "留下下一次决定",
          })),
        },
      },
    },
  });
  expect(savedPlan.status(), await savedPlan.text()).toBe(201);
  const plan = await savedPlan.json();
  const confirmed = await page.request.post(
    env.prefix + `/story-outlines/${plan.id}/confirm`,
    { headers: env.headers, data: {} },
  );
  expect(confirmed.status(), await confirmed.text()).toBe(200);
  const capture = await page.request.post(env.prefix + "/writing-context", {
    headers: env.headers,
    data: {
      series_plan_version_id: plan.id,
      ledger_version_id: null,
      expected_ledger_head_revision: 0,
      target_story_time: 10,
      target_scene_no: 1,
      character_ids: characters.map((character) => character.id),
    },
  });
  expect(capture.status(), await capture.text()).toBe(200);
  const context = (await capture.json()).context;
  const sourceResponse = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 1,
      base_version_id: env.source.id,
      content: {
        ...env.source.content_json,
        schema_version: "3.0",
        narrative_context: context,
        scenes: env.source.content_json.scenes.map(
          (scene: { dialogue: { line_id: string }[] }) => ({
            ...scene,
            dramatic_beat: {
              story_time: 10,
              character_objectives: characters.map((character) => ({
                character_id: character.id,
                purpose: "从追问走向坦诚",
                emotion_start: "紧张",
                emotion_end: "坚定",
              })),
              conflict: "林岚追问，周野决定不再隐瞒",
              dialogue_interactions: [
                {
                  line_ids: scene.dialogue.map((line) => line.line_id),
                  character_ids: characters.map((character) => character.id),
                  action: "林岚抬手示意，周野停步回应",
                },
              ],
            },
            state_changes: [
              {
                id: crypto.randomUUID(),
                kind: "EVENT",
                subject_id: characters[0].id,
                target_id: null,
                state_key: "truth-promised",
                value: "周野承诺说明真相",
                story_start: 10,
                story_end: null,
                known_by: characters.map((character) => ({
                  character_id: character.id,
                  story_start: 10,
                  story_end: null,
                })),
                causes: [],
                supersedes_entry_id: null,
              },
            ],
          }),
        ),
      },
    },
  });
  expect(sourceResponse.status(), await sourceResponse.text()).toBe(201);
  const source = await sourceResponse.json();
  const impact = await page.request.get(
    env.prefix + `/scripts/${source.id}/dependency-impact`,
    { headers: env.headers },
  );
  expect(impact.status(), await impact.text()).toBe(200);
  const approved = await page.request.post(
    env.prefix + `/scripts/${source.id}/review`,
    {
      headers: env.headers,
      data: {
        decision: "APPROVE",
        impact_digest: (await impact.json()).impact_digest,
      },
    },
  );
  expect(approved.status(), await approved.text()).toBe(200);
  expect((await approved.json()).story_state_receipt.script_version_id).toBe(
    source.id,
  );
  return source;
}

function performance(
  index: number,
  source: Awaited<ReturnType<typeof setup>>["source"],
): ShotPerformance {
  const line = source.content_json.scenes[0].dialogue[index];
  const characters =
    index === 2
      ? []
      : ["林岚", "周野"].map((character) => ({
          character,
          objective: character === "林岚" ? "要求对方解释" : "决定说出真相",
          emotion_start: index === 0 ? "紧张" : "犹豫",
          emotion_end: "坚定",
          action_start: index === 0 ? "抬手示意" : "放下双手",
          action_end: index === 0 ? "停步相对" : "点头回应",
          position_start: character === "林岚" ? "门口左侧" : "门口右侧",
          position_end: character === "林岚" ? "门口左侧" : "门口右侧",
          eyeline_start: character === "林岚" ? "周野" : "林岚",
          eyeline_end: character === "林岚" ? "周野" : "林岚",
        }));
  return {
    scene_purpose: "让隐瞒转为承诺",
    narrative_purpose: [
      "建立正面冲突",
      "承接对方的反应与决定",
      "留出反应后的停顿",
    ][index],
    emotion_rhythm: "追问、停顿、回应",
    characters,
    interactions:
      index === 0
        ? [
            {
              actor: "林岚",
              target: "周野",
              action: "抬手挡住去路",
              reaction: "停步看向林岚",
            },
          ]
        : [],
    space_start: "门口",
    space_end: "门口",
    props: [],
    continuity_from_shot: index === 0 ? null : index,
    continuity_mode: index === 0 ? "ellipsis" : "continuous",
    continuity_notes: index === 0 ? "建立场景起点" : "承接前镜人物位置与视线",
    motion_requirement: index === 2 ? "static_allowed" : "dynamic_required",
    dialogue: [
      {
        line_id: line.line_id,
        speaker: line.speaker,
        text: line.text,
        presentation: index === 2 ? "voice_over" : "visible",
        emotion: line.emotion,
        pacing: index === 1 ? "停顿后坚定回应" : "一句完整表达",
        lipsync: index === 2 ? "not_applicable" : "required",
        exemption_reason: index === 2 ? "旁白位于画外，没有出镜说话角色" : "",
      },
    ],
  };
}

test("SERIES-04: preserves conflict, reaction, exact dialogue and capability decisions through editing, refresh and viewer access", async ({
  page,
}, info) => {
  page.on("dialog", (dialog) => dialog.accept());
  const env = await setup(page, { withProp: true });
  const newer = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 1,
      content: {
        ...env.source.content_json,
        title: "后来的剧本不替换原分镜来源",
      },
    },
  });
  expect(newer.status(), await newer.text()).toBe(201);
  const boardResponse = await page.request.post(env.prefix + "/storyboards", {
    headers: env.headers,
    data: {
      expected_version_no: 0,
      script_version_id: env.source.id,
      content: {
        schema_version: "3.0",
        generation_routes: [],
        shots: [0, 1, 2].map((index) => ({
          schema_version: "3.0",
          shot_no: index + 1,
          scene_no: 1,
          scene_id: env.source.content_json.scenes[0].scene_id,
          duration_ms: 10000,
          shot_size: "medium",
          visual_description: [
            "林岚挡住出口，周野停步。",
            "周野迎着林岚的视线，点头回应。",
            "门口空镜，留出安静的停顿。",
          ][index],
          characters: index === 2 ? [] : ["林岚", "周野"],
          camera_movement: "static",
          subtitle: env.source.content_json.scenes[0].dialogue[index].text,
          dialogue_line_ids: [
            env.source.content_json.scenes[0].dialogue[index].line_id,
          ],
          performance: performance(index, env.source),
        })),
      },
    },
  });
  expect(boardResponse.status(), await boardResponse.text()).toBe(201);
  const board = await boardResponse.json();
  await env.openVersion(board.id);
  await expect(
    page.getByRole("region", { name: "表演分镜制作路线" }),
  ).toContainText("表演分镜 · 3.0");
  const details = page.locator("details.shot-details");
  await expect(details).toHaveCount(3);
  for (const detail of await details.all())
    await detail.locator("summary").click();
  await expect(details.nth(0)).toContainText("必须动态");
  await expect(details.nth(0)).toContainText("互动：林岚 → 周野");
  await expect(details.nth(0)).toContainText("可见对白 · 口型：必须同步");
  await expect(details.nth(1)).toContainText("接续来源：镜头 1");
  await expect(details.nth(2)).toContainText("允许静态");
  await expect(details.nth(2)).toContainText("旁白 · 口型：不适用");
  await expect(details.nth(2)).toContainText("没有出镜说话角色");
  await page.getByRole("button", { name: "分镜表格", exact: true }).click();
  await expect(page.getByRole("table", { name: "分镜镜头表" })).toContainText(
    "可见对白／需口型",
  );
  await page
    .getByRole("button", { name: "编辑并另存新版本", exact: true })
    .click();
  await page.getByRole("button", { name: "下移镜头 1", exact: true }).click();
  await page.getByRole("button", { name: "上移镜头 2", exact: true }).click();
  await page.getByRole("button", { name: "镜头详情", exact: true }).click();
  await expect(details).toHaveCount(3);
  for (const detail of await details.all())
    if ((await detail.getAttribute("open")) === null)
      await detail.locator("summary").click();
  await details
    .nth(0)
    .getByRole("textbox", { name: "镜头叙事目的", exact: true })
    .fill("保留冲突，明确人物要求");
  await details
    .nth(1)
    .getByRole("textbox", { name: "台词 1 · 节奏", exact: true })
    .fill("停顿一拍后回应，保留反应时间");
  await details
    .nth(0)
    .getByRole("combobox", { name: "动态要求", exact: true })
    .selectOption("static_allowed");
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("可见对白、人物动作或接续状态变化需要明确选择必须动态。", {
      exact: true,
    }),
  ).toBeVisible();
  await info.attach("performance-editability.json", {
    body: JSON.stringify(
      await details.nth(0).evaluate((detail) => ({
        open: detail.hasAttribute("open"),
        selects: Array.from(detail.querySelectorAll("select")).map(
          (select) => ({
            value: select.value,
            labels: Array.from(select.labels ?? []).map(
              (label) => label.textContent,
            ),
            hidden: select.hidden,
            ancestors: Array.from(
              (function* () {
                let node: Element | null = select;
                while (node && node !== detail) {
                  yield node;
                  node = node.parentElement;
                }
              })(),
            ).map((node) => ({
              tag: node.tagName,
              display: getComputedStyle(node).display,
              visibility: getComputedStyle(node).visibility,
              ariaHidden: node.getAttribute("aria-hidden"),
            })),
          }),
        ),
      })),
      null,
      2,
    ),
    contentType: "application/json",
  });
  await details
    .nth(0)
    .getByRole("heading", { name: "表演与接续计划", exact: true })
    .scrollIntoViewIfNeeded();
  await details
    .nth(0)
    .getByRole("combobox", { name: "动态要求", exact: true })
    .selectOption("dynamic_required");
  await expect(
    details.nth(0).getByRole("combobox", { name: "动态要求", exact: true }),
  ).toHaveValue("dynamic_required");
  const firstShot = details.nth(0);
  await firstShot
    .getByRole("button", { name: "添加接续道具", exact: true })
    .click();
  const propPicker = firstShot.getByRole("combobox", {
    name: "道具 1 · 资产版本",
    exact: true,
  });
  await expect(propPicker).toBeEnabled();
  await propPicker.click();
  await page
    .getByRole("option", { name: "门口的旧钥匙 · v1", exact: true })
    .click();
  for (const [label, value] of [
    ["起始持有人", "林岚"],
    ["结束持有人", "林岚"],
    ["起始位置", "右手"],
    ["结束位置", "右手"],
  ]) {
    await firstShot
      .getByLabel(`道具 1 · ${label}`, { exact: true })
      .fill(value);
  }
  await details
    .nth(0)
    .getByRole("heading", { name: "表演与接续计划", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    details.nth(0).getByRole("textbox", { name: "镜头叙事目的", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("performance-editor-desktop.png"),
    fullPage: true,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(`[data-node-id="storyboard:${board.id}"]`).click();
  await expect(details).toHaveCount(3);
  for (const detail of await details.all())
    if ((await detail.getAttribute("open")) === null)
      await detail.locator("summary").click();
  await expect(
    details.nth(0).getByRole("textbox", { name: "镜头叙事目的", exact: true }),
  ).toHaveValue("保留冲突，明确人物要求");
  await expect(
    details.nth(1).getByRole("textbox", { name: "台词 1 · 节奏", exact: true }),
  ).toHaveValue("停顿一拍后回应，保留反应时间");
  await expect(propPicker).toHaveText("门口的旧钥匙 · v1");
  await expect(firstShot).not.toContainText(env.prop!.asset_version_id);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await details
    .nth(0)
    .getByRole("heading", { name: "表演与接续计划", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    details.nth(0).getByRole("textbox", { name: "镜头叙事目的", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("performance-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v2",
  );
  const versions = await (
    await page.request.get(env.prefix + "/storyboards", {
      headers: env.headers,
    })
  ).json();
  expect(versions).toHaveLength(2);
  expect(versions[1].content_json).toEqual(board.content_json);
  expect(versions[0].script_version_id).toBe(env.source.id);
  expect(versions[0].content_json.shots[0].props).toEqual([env.prop]);
  expect(versions[0].content_json.shots[0].performance.props[0].asset).toEqual(
    env.prop,
  );
  expect(versions[0].content_json.shots[0].performance.narrative_purpose).toBe(
    "保留冲突，明确人物要求",
  );
  expect(
    versions[0].content_json.shots.map(
      (shot: { performance: ShotPerformance }) =>
        shot.performance.dialogue[0].text,
    ),
  ).toEqual(
    env.source.content_json.scenes[0].dialogue.map(
      (line: { text: string }) => line.text,
    ),
  );
  const reviewResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/storyboards/${versions[0].id}/review`) &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "批准该版本", exact: true }).click();
  const rejected = await reviewResponse;
  expect(rejected.status()).toBe(409);
  expect(await rejected.json()).toMatchObject({
    error: { code: "PERFORMANCE_NOT_READY" },
  });
  await expect(
    page.getByRole("alert").filter({ hasText: "表演分镜尚不具备制作条件" }),
  ).toBeVisible();
  expect(
    (
      await (
        await page.request.get(env.prefix + "/storyboards", {
          headers: env.headers,
        })
      ).json()
    )[0].status,
  ).toBe("READY_FOR_REVIEW");
  await page.setViewportSize({ width: 1440, height: 1100 });
  await env.openVersion(env.source.id);
  await page
    .getByRole("radio", { name: "调查、规划与逐场景拆镜", exact: true })
    .click();
  await expect(
    page.getByRole("radio", { name: "表演与动态分镜", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("表演与动态分镜需要已批准的剧情 3.0 剧本", { exact: false }),
  ).toBeVisible();
  const viewerResponse = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `performance-viewer-${crypto.randomUUID()}@example.test`,
      password: "performance-viewer-test",
      display_name: "分镜观察者",
    },
  });
  expect(viewerResponse.status()).toBe(201);
  const viewer = await viewerResponse.json();
  const member = await page.request.post(
    `/api/v1/projects/${env.project.id}/members`,
    {
      headers: env.headers,
      data: { email: viewer.user.email, role: "viewer" },
    },
  );
  expect(member.status(), await member.text()).toBe(201);
  await env.openVersion(versions[0].id);
  await expect(details).toHaveCount(3);
  for (const detail of await details.all())
    if ((await detail.getAttribute("open")) === null)
      await detail.locator("summary").click();
  await expect(details.nth(0)).toContainText("保留冲突，明确人物要求");
  await expect(
    page.getByRole("button", { name: "编辑并另存新版本", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "批准该版本", exact: true }),
  ).toHaveCount(0);
  await expect(details.getByRole("textbox")).toHaveCount(0);
  await details
    .nth(0)
    .getByRole("heading", { name: "表演与接续计划", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    details.nth(0).getByText("保留冲突，明确人物要求", { exact: false }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("performance-viewer-desktop.png"),
    fullPage: true,
  });
});

test("SERIES-04: explicitly enables an editable static performance draft while retaining the legacy source contract", async ({
  page,
}, info) => {
  const env = await setup(page, { dramatic: false });
  await page.goto(`/projects/${env.project.id}/episodes/${env.episode.id}`);
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写分镜", exact: true }).click();
  await page.getByRole("combobox", { name: "已批准的剧本版本" }).click();
  await page
    .getByRole("option", { name: "v1 · 冲突的精确来源", exact: true })
    .click();
  await page
    .getByRole("button", { name: "使用此剧本起稿", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "表演分镜制作路线" }),
  ).toHaveCount(0);
  await page
    .getByLabel("画面描述", { exact: true })
    .fill("门口没有人物的固定空镜");
  await page.getByRole("button", { name: "启用表演分镜", exact: true }).click();
  const details = page.locator("details.shot-details");
  await details.locator("summary").click();
  await expect(
    page.getByRole("button", { name: "保存人工分镜", exact: true }),
  ).toBeDisabled();
  for (const [label, value] of Object.entries({
    场景目的: "为下一场行动留出停顿",
    镜头叙事目的: "建立安静的门口",
    情绪与节奏: "平静停顿",
    起始空间状态: "门口空无一人",
    结束空间状态: "门口空无一人",
    接续说明: "独立场景首镜，没有前镜接续",
  }))
    await details.getByLabel(label, { exact: true }).fill(value);
  await details
    .getByRole("combobox", { name: "动态要求", exact: true })
    .selectOption("static_allowed");
  await page.screenshot({
    path: info.outputPath("performance-explicit-editor.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "保存人工分镜", exact: true }).click();
  await expect(page.locator(".inspector .section-heading h3")).toHaveText(
    "分镜 v1",
  );
  const boards = await (
    await page.request.get(env.prefix + "/storyboards", {
      headers: env.headers,
    })
  ).json();
  expect(boards).toHaveLength(1);
  expect(boards[0].content_json).toMatchObject({
    schema_version: "3.0",
    shots: [
      {
        schema_version: "3.0",
        performance: {
          motion_requirement: "static_allowed",
          dialogue: [],
          characters: [],
        },
      },
    ],
  });
  expect(boards[0].script_version_id).toBe(env.source.id);
  const scripts = await (
    await page.request.get(env.prefix + "/scripts", { headers: env.headers })
  ).json();
  expect(scripts[0].content_json).toEqual(env.source.content_json);
  expect(scripts[0].content_json.schema_version).toBe("2.0");
});

test("SERIES-04: starts the performance director from an exact approved narrative version and persists its real capability question", async ({
  page,
}, info) => {
  const env = await setup(page, { series: true });
  const source = await approvedNarrativeSource(page, env);
  await env.openVersion(source.id);
  await page
    .getByRole("radio", { name: "调查、规划与逐场景拆镜", exact: true })
    .click();
  await page
    .getByRole("radio", { name: "表演与动态分镜", exact: true })
    .click();
  await expect(
    page.getByText("记录叙事目的、动作起止、人物互动与接续", { exact: false }),
  ).toBeVisible();
  const start = page.getByRole("button", {
    name: "从此版本生成分镜",
    exact: true,
  });
  await expect(page.getByLabel("最多模型调用次数", { exact: true })).toHaveCount(0);
  await expect(start).toBeEnabled();
  const response = page.waitForResponse(
    (result) =>
      result.url().endsWith("/storyboard-runs") &&
      result.request().method() === "POST",
  );
  await start.click();
  const queued = await response;
  expect(queued.status(), await queued.text()).toBe(202);
  const request = queued.request().postDataJSON();
  expect(request).toMatchObject({
    script_version_id: source.id,
    performance: true,
    professional: true,
    director_revision_rounds: 3,
  });
  expect(request).not.toHaveProperty("agent_limits");
  const working = page.getByRole("region", {
    name: "导演计划与镜头工作稿",
    exact: true,
  });
  await expect(
    working.getByText("表演草稿已保留，但Mock/动作/口型能力未经真实验收", {
      exact: false,
    }),
  ).toBeVisible({ timeout: 20000 });
  const runs = (
    await (
      await page.request.get(env.prefix + "/runs", { headers: env.headers })
    ).json()
  ).filter(
    (run: { workflow_version: string }) =>
      run.workflow_version === "director/4.0",
  );
  expect(runs).toHaveLength(1);
  expect(runs[0].workflow_version).toBe("director/4.0");
  const progress = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/director`, {
      headers: env.headers,
    })
  ).json();
  const operation = progress.operations.find(
    (row: { kind: string; human_request: unknown }) =>
      row.kind === "director_check" && row.human_request,
  );
  expect(operation).toMatchObject({
    status: "WAITING_HUMAN",
    human_request: { question: expect.stringContaining("表演草稿已保留") },
  });
  expect(operation.question_id).toBeTruthy();
  expect(
    operation.director_revision.initial_check.issues.length,
  ).toBeGreaterThan(0);
  await working
    .getByText("表演草稿已保留，但Mock/动作/口型能力未经真实验收", {
      exact: false,
    })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("performance-director-question.png"),
    fullPage: true,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    working.getByText("表演草稿已保留，但Mock/动作/口型能力未经真实验收", {
      exact: false,
    }),
  ).toBeVisible();
  const restored = await (
    await page.request.get(`/api/v1/runs/${runs[0].id}/director`, {
      headers: env.headers,
    })
  ).json();
  expect(
    restored.operations.find(
      (row: { loop_id: string }) => row.loop_id === operation.loop_id,
    ),
  ).toMatchObject({
    question_id: operation.question_id,
    input_sha256: operation.input_sha256,
    status: "WAITING_HUMAN",
  });
  expect(
    await (
      await page.request.get(env.prefix + "/storyboards", {
        headers: env.headers,
      })
    ).json(),
  ).toEqual([]);
});

test("SERIES-04: explicitly confirms only intent, restores its receipt and keeps production blocked", async ({
  page,
}, info) => {
  page.on("dialog", (dialog) => dialog.accept());
  const env = await setup(page);
  const created = await page.request.post(env.prefix + "/storyboards", {
    headers: env.headers,
    data: {
      expected_version_no: 0,
      script_version_id: env.source.id,
      content: {
        schema_version: "3.0",
        generation_routes: [],
        shots: [0, 1, 2].map((index) => ({
          schema_version: "3.0",
          shot_no: index + 1,
          scene_no: 1,
          scene_id: env.source.content_json.scenes[0].scene_id,
          duration_ms: 10000,
          shot_size: "medium",
          visual_description:
            index === 2 ? "门口静态空镜。" : "两人在门口对峙。",
          characters: index === 2 ? [] : ["林岚", "周野"],
          camera_movement: "static",
          subtitle: env.source.content_json.scenes[0].dialogue[index].text,
          dialogue_line_ids: [
            env.source.content_json.scenes[0].dialogue[index].line_id,
          ],
          performance: performance(index, env.source),
        })),
      },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const board = await created.json();
  await env.openVersion(board.id);
  const choice = page.getByRole("checkbox", {
    name: "仅确认表演意图，保留制作阻塞",
    exact: true,
  });
  await expect(choice).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "批准该版本", exact: true }),
  ).toBeEnabled();
  await choice.check();
  const confirm = page.getByRole("button", {
    name: "仅确认表演意图",
    exact: true,
  });
  await expect(confirm).toBeDisabled();
  const reason =
    "确认冲突、反应与旁白安排；动作、精确参考及口型能力具备后另存重审。";
  const reasonField = page.getByLabel("表演意图确认理由（必填）", {
    exact: true,
  });
  await reasonField.fill("   ");
  await expect(confirm).toBeDisabled();
  await reasonField.fill(reason);
  const response = page.waitForResponse(
    (row) =>
      row.url().endsWith(`/storyboards/${board.id}/review`) &&
      row.request().method() === "POST",
  );
  await confirm.click();
  const approved = await response;
  expect(approved.status(), await approved.text()).toBe(200);
  expect(approved.request().postDataJSON()).toMatchObject({
    decision: "APPROVE",
    performance_intent_only: true,
    comment: reason,
  });
  const result = await approved.json();
  expect(result).toMatchObject({
    version_id: board.id,
    status: "APPROVED",
    human_review_request_id: expect.any(String),
    performance_intent: {
      scope: "INTENT_ONLY",
      production_ready: false,
      source_script_version_id: env.source.id,
      blockers: expect.arrayContaining([
        expect.objectContaining({ code: "PERFORMANCE_CAPABILITY_UNSUPPORTED" }),
      ]),
    },
  });
  const receipt = page.getByRole("region", {
    name: "表演意图审核决定",
    exact: true,
  });
  await expect(receipt).toContainText("仅确认表演意图 · 制作仍阻塞");
  await expect(receipt).toContainText(reason);
  await expect(receipt).toContainText("此精确版本仍不可进入制作");
  await page.reload();
  await openWorkspacePanels(page);
  await expect(receipt).toContainText(reason);
  const ledgerResponse = await page.request.get(
    `/api/v1/projects/${env.project.id}/human-reviews?version_id=${board.id}&limit=100`,
    { headers: env.headers },
  );
  expect(ledgerResponse.status(), await ledgerResponse.text()).toBe(200);
  const ledger = (await ledgerResponse.json()).find(
    (row: { id: string }) => row.id === result.human_review_request_id,
  );
  expect(ledger).toMatchObject({
    snapshot: { version_id: board.id },
    decision: {
      decision_json: {
        version_id: board.id,
        decision: "APPROVE",
        comment: reason,
        performance_intent: result.performance_intent,
      },
    },
  });
  const stateResponse = await page.request.get(
    env.prefix + "/production-state",
    { headers: env.headers },
  );
  expect(stateResponse.status(), await stateResponse.text()).toBe(200);
  expect((await stateResponse.json()).issues).toContain(
    "PERFORMANCE_NOT_READY",
  );
  await page.getByRole("button", { name: "制作进度", exact: true }).click();
  const progress = page.getByRole("region", { name: "剧集生产阶段" });
  await progress.getByRole("button", { name: /^4[.] 画面/ }).click();
  await expect(
    progress.getByRole("list", { name: "阶段待处理条件" }),
  ).toContainText("表演分镜制作条件尚未满足");
  const production = await page.request.get(
    env.prefix + `/storyboards/${board.id}/production`,
    { headers: env.headers },
  );
  expect(production.status(), await production.text()).toBe(200);
  const shots = (await production.json()).shots;
  expect(shots).toHaveLength(3);
  for (const shot of [shots[0], shots[2]]) {
    const code =
      shot.shot_no === 3 ? "PERFORMANCE_NOT_READY" : "DYNAMIC_MEDIA_REQUIRED";
    expect(shot.request).toBeNull();
    expect(shot.blocker.code).toBe(code);
    const captured = await page.request.post(
      `/api/v1/projects/${env.project.id}/generation-jobs`,
      {
        headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
        data: {
          episode_id: env.episode.id,
          target_type: "SHOT_IMAGE",
          task_type: "IMAGE",
          review_target: { storyboard_id: board.id, shot_id: shot.id },
          inputs: [
            {
              source_asset_version_id: board.id,
              dependency_type: "CONTENT_DEPENDENCY",
              selectors: ["shot:" + shot.id],
            },
          ],
          spec: {
            provider: "mock",
            model: "mock-image-v1",
            model_config_version: "series04-admission/1",
            prompt: "表演",
            prompt_version: "series04-admission/1",
            output_spec: { width: 1024, height: 1024, format: "png" },
          },
        },
      },
    );
    expect(captured.status(), await captured.text()).toBe(409);
    expect(await captured.json()).toMatchObject({ error: { code } });
  }
  const timeline = await page.request.post(env.prefix + "/timelines", {
    headers: env.headers,
    data: {
      creation_key: crypto.randomUUID(),
      storyboard_id: board.id,
      selected_shots: shots.map((shot: { id: string }) => ({
        shot_id: shot.id,
        asset_version_id: crypto.randomUUID(),
      })),
    },
  });
  expect(timeline.status(), await timeline.text()).toBe(409);
  expect(await timeline.json()).toMatchObject({
    error: { code: "DYNAMIC_MEDIA_REQUIRED" },
  });
  const jobs = await page.request.get(
    `/api/v1/projects/${env.project.id}/generation-jobs`,
    { headers: env.headers },
  );
  expect(jobs.status(), await jobs.text()).toBe(200);
  expect(await jobs.json()).toEqual([]);
  await receipt
    .getByRole("heading", { name: "仅确认表演意图 · 制作仍阻塞", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("performance-intent-blocked.png"),
    fullPage: true,
  });
});
