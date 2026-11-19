import type { ExactAssetReference } from "@/types/api";

export type PerformanceCharacter = {
  character: string;
  objective: string;
  emotion_start: string;
  emotion_end: string;
  action_start: string;
  action_end: string;
  position_start: string;
  position_end: string;
  eyeline_start: string;
  eyeline_end: string;
};
export type PerformanceDialogue = {
  line_id: string;
  speaker: string;
  text: string;
  presentation: "visible" | "voice_over" | "off_screen" | "obscured";
  emotion: string;
  pacing: string;
  lipsync: "required" | "not_applicable";
  exemption_reason: string;
};
export type ShotPerformance = {
  scene_purpose: string;
  narrative_purpose: string;
  emotion_rhythm: string;
  characters: PerformanceCharacter[];
  interactions: {
    actor: string;
    target: string;
    action: string;
    reaction: string;
  }[];
  space_start: string;
  space_end: string;
  props: {
    asset: ExactAssetReference;
    holder_start: string;
    holder_end: string;
    position_start: string;
    position_end: string;
  }[];
  continuity_from_shot: number | null;
  continuity_mode: "continuous" | "ellipsis";
  continuity_notes: string;
  motion_requirement: "dynamic_required" | "static_allowed";
  dialogue: PerformanceDialogue[];
};
export type GenerationRoute = { provider: string; model: string };
export type PerformanceSourceLine = {
  line_id?: string;
  speaker: string;
  text: string;
  line_type?: "dialogue" | "narration";
  emotion?: string;
};

export function isShotPerformance(value: unknown): value is ShotPerformance {
  const record = (row: unknown): row is Record<string, unknown> =>
    !!row && typeof row === "object" && !Array.isArray(row);
  const strings = (row: Record<string, unknown>, keys: string[]) =>
    keys.every((key) => typeof row[key] === "string");
  const rows = (
    row: unknown,
    check: (entry: Record<string, unknown>) => boolean,
  ) =>
    Array.isArray(row) && row.every((entry) => record(entry) && check(entry));
  if (!record(value)) return false;
  return (
    strings(value, [
      "scene_purpose",
      "narrative_purpose",
      "emotion_rhythm",
      "space_start",
      "space_end",
      "continuity_notes",
    ]) &&
    ["continuous", "ellipsis"].includes(String(value.continuity_mode)) &&
    ["dynamic_required", "static_allowed"].includes(
      String(value.motion_requirement),
    ) &&
    (value.continuity_from_shot === null ||
      (typeof value.continuity_from_shot === "number" &&
        Number.isInteger(value.continuity_from_shot) &&
        value.continuity_from_shot > 0)) &&
    rows(value.characters, (entry) =>
      strings(entry, [
        "character",
        "objective",
        "emotion_start",
        "emotion_end",
        "action_start",
        "action_end",
        "position_start",
        "position_end",
        "eyeline_start",
        "eyeline_end",
      ]),
    ) &&
    rows(value.interactions, (entry) =>
      strings(entry, ["actor", "target", "action", "reaction"]),
    ) &&
    rows(
      value.props,
      (entry) =>
        record(entry.asset) &&
        strings(entry.asset, ["asset_id", "asset_version_id"]) &&
        strings(entry, [
          "holder_start",
          "holder_end",
          "position_start",
          "position_end",
        ]),
    ) &&
    rows(
      value.dialogue,
      (entry) =>
        strings(entry, [
          "line_id",
          "speaker",
          "text",
          "emotion",
          "pacing",
          "exemption_reason",
        ]) &&
        ["visible", "voice_over", "off_screen", "obscured"].includes(
          String(entry.presentation),
        ) &&
        ["required", "not_applicable"].includes(String(entry.lipsync)),
    )
  );
}

export function emptyPerformanceCharacter(
  character: string,
): PerformanceCharacter {
  return {
    character,
    objective: "",
    emotion_start: "",
    emotion_end: "",
    action_start: "",
    action_end: "",
    position_start: "",
    position_end: "",
    eyeline_start: "",
    eyeline_end: "",
  };
}

export function performanceDialogue(
  lineId: string,
  source?: PerformanceSourceLine,
): PerformanceDialogue {
  const narration = source?.line_type === "narration";
  return {
    line_id: lineId,
    speaker: source?.speaker ?? "",
    text: source?.text ?? "",
    presentation: narration ? "voice_over" : "visible",
    emotion: source?.emotion ?? "",
    pacing: "",
    lipsync: narration ? "not_applicable" : "required",
    exemption_reason: "",
  };
}

export function emptyShotPerformance(
  characters: string[],
  lineIds: string[] = [],
  lines?: PerformanceSourceLine[],
): ShotPerformance {
  return {
    scene_purpose: "",
    narrative_purpose: "",
    emotion_rhythm: "",
    characters: characters.map(emptyPerformanceCharacter),
    interactions: [],
    space_start: "",
    space_end: "",
    props: [],
    continuity_from_shot: null,
    continuity_mode: "ellipsis",
    continuity_notes: "",
    motion_requirement: "dynamic_required",
    dialogue: lineIds.map((id) =>
      performanceDialogue(
        id,
        lines?.find((line) => line.line_id === id),
      ),
    ),
  };
}

export function performanceProblem(
  performance: ShotPerformance | undefined,
): string | null {
  if (!performance) return "请填写表演计划。";
  if (
    [
      performance.scene_purpose,
      performance.narrative_purpose,
      performance.emotion_rhythm,
      performance.space_start,
      performance.space_end,
    ].some((value) => !value.trim())
  )
    return "请补齐场景目的、叙事目的、情绪节奏、空间起止和接续说明。";
  if (
    performance.continuity_mode === "ellipsis" &&
    !performance.continuity_notes.trim()
  )
    return "请说明省略或跳切的接续原因。";
  const hasMotion =
    performance.space_start !== performance.space_end ||
    performance.interactions.length > 0 ||
    performance.characters.some(
      (row) =>
        row.action_start !== row.action_end ||
        row.position_start !== row.position_end ||
        row.eyeline_start !== row.eyeline_end,
    ) ||
    performance.props.some(
      (row) =>
        row.holder_start !== row.holder_end ||
        row.position_start !== row.position_end,
    ) ||
    performance.dialogue.some((line) => line.presentation === "visible");
  if (hasMotion && performance.motion_requirement !== "dynamic_required")
    return "可见对白、人物动作或接续状态变化需要明确选择必须动态。";
  if (
    performance.characters.some((character) =>
      Object.values(character).some((value) => !value.trim()),
    )
  )
    return "请补齐每位出场人物的目标、情绪、动作、位置和视线起止。";
  if (
    performance.interactions.some((interaction) =>
      Object.values(interaction).some((value) => !value.trim()),
    )
  )
    return "请补齐互动双方、行动和反应。";
  if (
    performance.dialogue.some(
      (line) =>
        !line.emotion.trim() ||
        !line.pacing.trim() ||
        (line.lipsync === "not_applicable" && !line.exemption_reason.trim()),
    )
  )
    return "请补齐每句台词的情绪、节奏和口型豁免理由。";
  if (
    performance.props.some(
      (prop) =>
        !prop.asset.asset_id.trim() ||
        !prop.asset.asset_version_id.trim() ||
        !prop.holder_start.trim() ||
        !prop.holder_end.trim() ||
        !prop.position_start.trim() ||
        !prop.position_end.trim(),
    )
  )
    return "请补齐道具的精确资产版本、持有人及位置起止。";
  return null;
}

export function reorderPerformanceShots<
  T extends { shot_no: number; performance?: ShotPerformance },
>(shots: T[]): T[] {
  const numbers = new Map(
    shots.map((shot, index) => [shot.shot_no, index + 1]),
  );
  return shots.map((shot, index) => ({
    ...shot,
    shot_no: index + 1,
    ...(shot.performance
      ? {
          performance: {
            ...shot.performance,
            continuity_from_shot:
              shot.performance.continuity_from_shot === null
                ? null
                : (numbers.get(shot.performance.continuity_from_shot) ??
                  shot.performance.continuity_from_shot),
          },
        }
      : {}),
  }));
}
