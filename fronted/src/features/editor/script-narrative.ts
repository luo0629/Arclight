import type { LedgerEntry, LedgerKind } from "@/features/story/story-ledger";

export type NarrativeRequest = {
  series_plan_version_id: string;
  ledger_version_id: string | null;
  expected_ledger_head_revision: number;
  target_story_time: number;
  target_scene_no: number;
  character_ids: string[];
};
export type StoryStateReceipt = {
  revision: number;
  latest_version_id: string;
  approved_version_id: string;
  approval_id: string;
  script_version_id: string;
  script_content_sha256: string;
  proposal_sha256: string;
  approval_decision_id: string;
};
export type DramaticBeat = {
  story_time: number;
  character_objectives: {
    character_id: string;
    purpose: string;
    emotion_start: string;
    emotion_end: string;
  }[];
  conflict: string;
  dialogue_interactions: {
    line_ids: string[];
    character_ids: string[];
    action: string;
  }[];
};
export type StateChange = {
  id: string;
  kind: LedgerKind;
  subject_id: string | null;
  target_id: string | null;
  state_key: string;
  value: string;
  story_start: number;
  story_end: number | null;
  known_by: {
    character_id: string;
    story_start: number;
    story_end: number | null;
  }[];
  causes: string[];
  supersedes_entry_id: string | null;
};
export type NarrativeScene = {
  scene_id?: string;
  scene_no: number;
  dramatic_beat?: DramaticBeat;
  state_changes?: StateChange[];
  dialogue: { line_id?: string; speaker: string; text: string }[];
};
export type NarrativeProjection = {
  perspective: "AUTHOR" | "CHARACTER";
  character_id: string | null;
  target: { story_time: number; scene_no: number; episode_no: number };
  plan: Record<string, unknown> | null;
  facts: Omit<LedgerEntry, "known_by">[];
  planned: Omit<LedgerEntry, "known_by">[];
  proposals: Omit<LedgerEntry, "known_by">[];
  causal_summaries: {
    episode_no: number;
    script_version_id: string;
    summary: string;
    truncated?: boolean;
  }[];
  sources: Record<string, unknown>[];
  truncated: boolean;
  index_truncated?: boolean;
  related_truncated?: boolean;
  related_missing?: { episode_no: number; reason: "NO_APPROVED_SCRIPT" }[];
  result_sha256: string;
};
export type NarrativeCapture = {
  context: Record<string, unknown>;
  projection: NarrativeProjection;
  character_projections: NarrativeProjection[];
  character_projections_truncated?: boolean;
};
export const emptyNarrativeRequest = (): NarrativeRequest => ({
  series_plan_version_id: "",
  ledger_version_id: null,
  expected_ledger_head_revision: 0,
  target_story_time: 1,
  target_scene_no: 1,
  character_ids: [],
});
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((item) => typeof item === "string");
const number = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value);
const nullableText = (value: unknown) =>
  value === null || typeof value === "string";
export function isNarrativeRequest(value: unknown): value is NarrativeRequest {
  return (
    object(value) &&
    typeof value.series_plan_version_id === "string" &&
    nullableText(value.ledger_version_id) &&
    number(value.expected_ledger_head_revision) &&
    number(value.target_story_time) &&
    number(value.target_scene_no) &&
    strings(value.character_ids)
  );
}
export function narrativeRequestProblem(value: NarrativeRequest): string {
  const uuid =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(value.series_plan_version_id))
    return "请选择已批准的全剧规划精确版本。";
  if (value.ledger_version_id !== null && !uuid.test(value.ledger_version_id))
    return "请填写账本精确版本，尚无批准账本时留空。";
  if (value.expected_ledger_head_revision < 0 || value.target_scene_no < 1)
    return "账本修订须为非负整数，场景编号须为正整数。";
  if (
    ![
      value.expected_ledger_head_revision,
      value.target_story_time,
      value.target_scene_no,
    ].every(Number.isSafeInteger)
  )
    return "账本修订、故事时点和场景编号须为整数。";
  if (
    value.target_scene_no > 50 ||
    Math.abs(value.target_story_time) > 1000000000
  )
    return "场景编号最多50，故事时点须在正负10亿以内。";
  if (
    value.character_ids.length > 50 ||
    value.character_ids.some((id) => !uuid.test(id))
  )
    return "请选择有效故事角色，最多50位。";
  if (new Set(value.character_ids).size !== value.character_ids.length)
    return "故事角色不能重复。";
  return "";
}
export function isNarrativeSceneDraft(value: unknown): boolean {
  if (!object(value) || !object(value.dramatic_beat)) return false;
  const beat = value.dramatic_beat;
  return (
    number(beat.story_time) &&
    typeof beat.conflict === "string" &&
    Array.isArray(beat.character_objectives) &&
    beat.character_objectives.every(
      (v) =>
        object(v) &&
        ["character_id", "purpose", "emotion_start", "emotion_end"].every(
          (key) => typeof v[key] === "string",
        ),
    ) &&
    Array.isArray(beat.dialogue_interactions) &&
    beat.dialogue_interactions.every(
      (v) =>
        object(v) &&
        strings(v.line_ids) &&
        strings(v.character_ids) &&
        typeof v.action === "string",
    ) &&
    Array.isArray(value.state_changes) &&
    value.state_changes.every(
      (v) =>
        object(v) &&
        ["id", "kind", "state_key", "value"].every(
          (key) => typeof v[key] === "string",
        ) &&
        ["subject_id", "target_id", "supersedes_entry_id"].every((key) =>
          nullableText(v[key]),
        ) &&
        number(v.story_start) &&
        (v.story_end === null || number(v.story_end)) &&
        strings(v.causes) &&
        Array.isArray(v.known_by) &&
        v.known_by.every(
          (k) =>
            object(k) &&
            typeof k.character_id === "string" &&
            number(k.story_start) &&
            (k.story_end === null || number(k.story_end)),
        ),
    )
  );
}
export const emptyBeat = (storyTime: number): DramaticBeat => ({
  story_time: storyTime,
  character_objectives: [],
  conflict: "",
  dialogue_interactions: [],
});
export function requestFromContext(
  value: Record<string, unknown>,
): NarrativeRequest | null {
  // The server receipt contains its frozen request; never substitute today's head.
  for (const candidate of [value.request, value.narrative_request, value])
    if (isNarrativeRequest(candidate))
      return {
        series_plan_version_id: candidate.series_plan_version_id,
        ledger_version_id: candidate.ledger_version_id,
        expected_ledger_head_revision: candidate.expected_ledger_head_revision,
        target_story_time: candidate.target_story_time,
        target_scene_no: candidate.target_scene_no,
        character_ids: [...candidate.character_ids],
      };
  return null;
}
