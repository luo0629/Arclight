export type OutlineEntry = {
  episode_no: number;
  title: string;
  goal: string;
  conflict: string;
  turning_point: string;
  ending_hook: string;
};

type OutlineBase = {
  target_episode_count: number;
  episode_duration_ms: number;
  episodes: OutlineEntry[];
};
export type LegacyOutlineContent = OutlineBase & { schema_version: "1.0" };
export type Milestone = { episode_no: number; change: string };
export type SeriesThread = {
  id: string;
  kind: "MAIN" | "SUBPLOT";
  title: string;
  central_conflict: string;
  planned_resolution: string;
};
export type CharacterArc = {
  character_id: string;
  character_revision: number;
  starting_goal: string;
  internal_conflict: string;
  planned_growth: string;
  milestones: Milestone[];
};
export type RelationshipArc = {
  id: string;
  source_character_id: string;
  target_character_id: string;
  initial_relationship: string;
  planned_development: string;
  milestones: Milestone[];
};
export type Foreshadowing = {
  id: string;
  thread_id: string;
  setup_episode_no: number;
  setup: string;
  expected_payoff_episode_no: number;
  planned_payoff: string;
};
export type EpisodeProgression = {
  episode_no: number;
  thread_beats: { thread_id: string; progression: string }[];
  caused_by_episode_nos: number[];
  causal_link: string;
  ending_handoff: string;
};
export type SeriesPlan = {
  schema_version: "1.0";
  kind: "PLANNED";
  threads: SeriesThread[];
  character_arcs: CharacterArc[];
  relationship_arcs: RelationshipArc[];
  foreshadowing: Foreshadowing[];
  episode_progressions: EpisodeProgression[];
};
export type SeriesOutlineContent = OutlineBase & {
  schema_version: "2.0";
  series_plan: SeriesPlan;
};
export type OutlineContent = LegacyOutlineContent | SeriesOutlineContent;
export type CharacterReference = { id: string; revision: number; name: string };

const object = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown) => typeof v === "string" && v.length <= 4000;
const integer = (v: unknown) => typeof v === "number" && Number.isInteger(v);
const array = (v: unknown, max: number, valid: (v: unknown) => boolean) =>
  Array.isArray(v) && v.length <= max && v.every(valid);
const milestone = (v: unknown) =>
  object(v) && integer(v.episode_no) && text(v.change);

// A recoverable draft may still have blank fields. Validate its complete shape
// here; semantic checks below guide the author before saving.
export function isSeriesPlanDraft(v: unknown): v is SeriesPlan {
  return (
    object(v) &&
    v.schema_version === "1.0" &&
    v.kind === "PLANNED" &&
    array(
      v.threads,
      100,
      (r) =>
        object(r) &&
        text(r.id) &&
        ["MAIN", "SUBPLOT"].includes(String(r.kind)) &&
        text(r.title) &&
        text(r.central_conflict) &&
        text(r.planned_resolution),
    ) &&
    array(
      v.character_arcs,
      200,
      (r) =>
        object(r) &&
        text(r.character_id) &&
        integer(r.character_revision) &&
        text(r.starting_goal) &&
        text(r.internal_conflict) &&
        text(r.planned_growth) &&
        array(r.milestones, 1000, milestone),
    ) &&
    array(
      v.relationship_arcs,
      200,
      (r) =>
        object(r) &&
        text(r.id) &&
        text(r.source_character_id) &&
        text(r.target_character_id) &&
        text(r.initial_relationship) &&
        text(r.planned_development) &&
        array(r.milestones, 1000, milestone),
    ) &&
    array(
      v.foreshadowing,
      500,
      (r) =>
        object(r) &&
        text(r.id) &&
        text(r.thread_id) &&
        integer(r.setup_episode_no) &&
        text(r.setup) &&
        integer(r.expected_payoff_episode_no) &&
        text(r.planned_payoff),
    ) &&
    array(
      v.episode_progressions,
      1000,
      (r) =>
        object(r) &&
        integer(r.episode_no) &&
        array(
          r.thread_beats,
          100,
          (b) => object(b) && text(b.thread_id) && text(b.progression),
        ) &&
        array(r.caused_by_episode_nos, 1000, integer) &&
        text(r.causal_link) &&
        text(r.ending_handoff),
    )
  );
}

export function isOutlineContentDraft(v: unknown): v is OutlineContent {
  if (
    !object(v) ||
    !integer(v.target_episode_count) ||
    !integer(v.episode_duration_ms) ||
    !array(
      v.episodes,
      1000,
      (r) =>
        object(r) &&
        integer(r.episode_no) &&
        [r.title, r.goal, r.conflict, r.turning_point, r.ending_hook].every(
          text,
        ),
    ) ||
    !(v.episodes as unknown[]).length
  )
    return false;
  return v.schema_version === "1.0"
    ? !("series_plan" in v)
    : v.schema_version === "2.0" && isSeriesPlanDraft(v.series_plan);
}

export function emptySeriesPlan(episodes: OutlineEntry[]): SeriesPlan {
  const threadId = crypto.randomUUID();
  return {
    schema_version: "1.0",
    kind: "PLANNED",
    threads: [
      {
        id: threadId,
        kind: "MAIN",
        title: "",
        central_conflict: "",
        planned_resolution: "",
      },
    ],
    character_arcs: [],
    relationship_arcs: [],
    foreshadowing: [
      {
        id: crypto.randomUUID(),
        thread_id: threadId,
        setup_episode_no: 1,
        setup: "",
        expected_payoff_episode_no: episodes.length,
        planned_payoff: "",
      },
    ],
    episode_progressions: episodes.map((episode) => ({
      episode_no: episode.episode_no,
      thread_beats: [{ thread_id: threadId, progression: "" }],
      caused_by_episode_nos: [],
      causal_link: "",
      ending_handoff: "",
    })),
  };
}

const filled = (v: string, max = 4000) => !!v.trim() && v.length <= max;
const unique = <T>(values: T[]) => new Set(values).size === values.length;
const validId = (v: string) => /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(v);
const characterId = (v: string) =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);

export function seriesPlanProblem(
  plan: SeriesPlan,
  episodes: OutlineEntry[],
): string {
  const episodeNos = new Set(episodes.map((e) => e.episode_no));
  const threadIds = new Set(plan.threads.map((t) => t.id));
  const characters = new Set(plan.character_arcs.map((c) => c.character_id));
  const milestonesValid = (rows: Milestone[]) =>
    rows.length > 0 &&
    unique(rows.map((m) => m.episode_no)) &&
    rows.every((m) => episodeNos.has(m.episode_no) && filled(m.change));
  if (!plan.threads.length || !plan.threads.some((t) => t.kind === "MAIN"))
    return "请保留至少一条主线。";
  if (
    !unique(plan.threads.map((t) => t.id)) ||
    plan.threads.some(
      (t) =>
        !validId(t.id) ||
        !filled(t.title, 200) ||
        !filled(t.central_conflict) ||
        !filled(t.planned_resolution),
    )
  )
    return "请补齐主支线标题、关键冲突和预定收束。";
  if (!plan.character_arcs.length) return "请添加至少一位既有人物的成长规划。";
  if (
    !unique(plan.character_arcs.map((c) => c.character_id)) ||
    plan.character_arcs.some(
      (c) =>
        !characterId(c.character_id) ||
        c.character_revision < 0 ||
        !filled(c.starting_goal) ||
        !filled(c.internal_conflict) ||
        !filled(c.planned_growth) ||
        !milestonesValid(c.milestones),
    )
  )
    return "请补齐人物选择、成长说明及有效的分集变化。";
  if (
    !unique(plan.relationship_arcs.map((r) => r.id)) ||
    plan.relationship_arcs.some(
      (r) =>
        !validId(r.id) ||
        !characters.has(r.source_character_id) ||
        !characters.has(r.target_character_id) ||
        r.source_character_id === r.target_character_id ||
        !filled(r.initial_relationship) ||
        !filled(r.planned_development) ||
        !milestonesValid(r.milestones),
    )
  )
    return "请补齐关系双方、关系发展及有效的分集变化。";
  if (
    !plan.foreshadowing.length ||
    !unique(plan.foreshadowing.map((f) => f.id)) ||
    plan.foreshadowing.some(
      (f) =>
        !validId(f.id) ||
        !threadIds.has(f.thread_id) ||
        !episodeNos.has(f.setup_episode_no) ||
        !episodeNos.has(f.expected_payoff_episode_no) ||
        f.expected_payoff_episode_no < f.setup_episode_no ||
        !filled(f.setup) ||
        !filled(f.planned_payoff),
    )
  )
    return "请补齐伏笔、所属线索及有效的设置和预计回收集。";
  if (
    plan.episode_progressions.length !== episodes.length ||
    !unique(plan.episode_progressions.map((e) => e.episode_no)) ||
    plan.episode_progressions.some(
      (e) =>
        !episodeNos.has(e.episode_no) ||
        !e.thread_beats.length ||
        !unique(e.thread_beats.map((b) => b.thread_id)) ||
        e.thread_beats.some(
          (b) => !threadIds.has(b.thread_id) || !filled(b.progression),
        ) ||
        !unique(e.caused_by_episode_nos) ||
        e.caused_by_episode_nos.some(
          (n) => !episodeNos.has(n) || n === e.episode_no,
        ) ||
        !filled(e.causal_link) ||
        !filled(e.ending_handoff),
    )
  )
    return "请补齐每集的线索推进、因果说明和结尾承接；引用集数须有效且不重复。";
  const usedThreads = new Set(
    plan.episode_progressions.flatMap((episode) =>
      episode.thread_beats.map((beat) => beat.thread_id),
    ),
  );
  if (plan.threads.some((thread) => !usedThreads.has(thread.id)))
    return "请为每条主支线添加至少一次分集推进。";
  return "";
}

export function resizeOutline(
  content: OutlineContent,
  episodes: OutlineEntry[],
): OutlineContent {
  const base = { ...content, target_episode_count: episodes.length, episodes };
  if (content.schema_version === "1.0")
    return { ...base, schema_version: "1.0" };
  const byEpisode = new Map(
    content.series_plan.episode_progressions.map((e) => [e.episode_no, e]),
  );
  return {
    ...base,
    schema_version: "2.0",
    series_plan: {
      ...content.series_plan,
      episode_progressions: episodes.map(
        (e) =>
          byEpisode.get(e.episode_no) ?? {
            episode_no: e.episode_no,
            thread_beats: [
              {
                thread_id: content.series_plan.threads[0]?.id ?? "",
                progression: "",
              },
            ],
            caused_by_episode_nos: [],
            causal_link: "",
            ending_handoff: "",
          },
      ),
    },
  };
}
