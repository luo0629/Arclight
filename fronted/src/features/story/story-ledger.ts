export type SceneTime = {
  id: string;
  episode_id: string;
  episode_no: number;
  script_version_id: string;
  scene_id: string;
  scene_no: number;
  segment_no: number;
  story_time: number;
};
export type LedgerKind =
  | "EVENT"
  | "RELATIONSHIP"
  | "KNOWLEDGE"
  | "INJURY"
  | "COSTUME"
  | "PROP_OWNERSHIP";
export type LedgerStatus = "AUTHOR_PLANNED" | "PROPOSED" | "CONFIRMED";
export type LedgerEntry = {
  id: string;
  kind: LedgerKind;
  status: LedgerStatus;
  subject_id: string | null;
  target_id: string | null;
  state_key: string;
  value: string;
  story_start: number;
  story_end: number | null;
  disclosure_start: string;
  disclosure_end: string | null;
  source_script_version_id: string | null;
  source_scene_id: string | null;
  known_by: {
    character_id: string;
    story_start: number;
    story_end: number | null;
  }[];
  causes: string[];
};
export type LedgerContent = {
  schema_version: "1.0";
  scene_times: SceneTime[];
  entries: LedgerEntry[];
};
export type LedgerHead = {
  revision: number;
  latest_version_id: string | null;
  approved_version_id: string | null;
  approval_id: string | null;
};
export type LedgerVersion = {
  id: string;
  project_id: string;
  version_no: number;
  base_version_id: string | null;
  content: LedgerContent;
  content_sha256: string;
  source_manifest: Record<string, unknown>;
  created_by: string;
  created_at: string;
  approval: LedgerApproval | null;
};
export type LedgerVersionSummary = Pick<
  LedgerVersion,
  | "id"
  | "project_id"
  | "version_no"
  | "base_version_id"
  | "content_sha256"
  | "created_by"
  | "created_at"
  | "approval"
> & { source_manifest_sha256: string };
export type LedgerApproval = {
  id: string;
  version_id: string;
  content_sha256: string;
  source_manifest_sha256: string;
  approved_by: string;
  approved_at: string;
};
export type LedgerSource = {
  episode_id: string;
  episode_no: number;
  script_version_id: string;
  version_no: number;
  content_sha256: string;
  episode_title: string;
  registry_version_id: string;
  scenes: {
    scene_id: string;
    scene_no: number;
    title: string;
    dialogue_count: number;
  }[];
};
export type LedgerPage<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};
export type LedgerQuery = {
  episode_id: string;
  script_version_id: string;
  scene_id: string;
  segment_no: number;
  perspective: "AUTHOR" | "CHARACTER";
  character_id?: string;
  include_planned: boolean;
  include_proposed: boolean;
};
export type LedgerResult = {
  version_id: string;
  version_no: number;
  content_sha256: string;
  approval_id: string | null;
  target: SceneTime;
  perspective: "AUTHOR" | "CHARACTER";
  character_id: string | null;
  facts: Omit<LedgerEntry, "known_by">[];
  planned: Omit<LedgerEntry, "known_by">[];
  proposals: Omit<LedgerEntry, "known_by">[];
  total: number;
  limit: number;
  offset: number;
  returned_count: number;
  next_offset: number | null;
};
export const ledgerKinds: Record<LedgerKind, string> = {
  EVENT: "剧情事件",
  RELATIONSHIP: "关系",
  KNOWLEDGE: "角色知情",
  INJURY: "伤势",
  COSTUME: "服装",
  PROP_OWNERSHIP: "道具归属",
};
export const ledgerStatuses: Record<LedgerStatus, string> = {
  AUTHOR_PLANNED: "作者计划",
  PROPOSED: "待核实提议",
  CONFIRMED: "确认记录（须版本批准）",
};
export const emptyLedger = (): LedgerContent => ({
  schema_version: "1.0",
  scene_times: [],
  entries: [],
});
export function sceneLabel(scene: SceneTime) {
  return `第 ${scene.episode_no} 集 / 第 ${scene.scene_no} 场 / 片段 ${scene.segment_no}（故事时点 ${scene.story_time}）`;
}
export function isLedgerContent(value: unknown): value is LedgerContent {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<LedgerContent>;
  const optionalString = (v: unknown) => v === null || typeof v === "string";
  const optionalInteger = (v: unknown) => v === null || Number.isInteger(v);
  return (
    row.schema_version === "1.0" &&
    Array.isArray(row.scene_times) &&
    row.scene_times.every(
      (scene) =>
        !!scene &&
        typeof scene.id === "string" &&
        typeof scene.episode_id === "string" &&
        typeof scene.script_version_id === "string" &&
        typeof scene.scene_id === "string" &&
        Number.isInteger(scene.episode_no) &&
        Number.isInteger(scene.scene_no) &&
        Number.isInteger(scene.segment_no) &&
        Number.isInteger(scene.story_time),
    ) &&
    Array.isArray(row.entries) &&
    row.entries.every(
      (entry) =>
        !!entry &&
        typeof entry.id === "string" &&
        Object.hasOwn(ledgerKinds, entry.kind) &&
        Object.hasOwn(ledgerStatuses, entry.status) &&
        typeof entry.state_key === "string" &&
        typeof entry.value === "string" &&
        typeof entry.disclosure_start === "string" &&
        optionalString(entry.subject_id) &&
        optionalString(entry.target_id) &&
        optionalString(entry.disclosure_end) &&
        optionalString(entry.source_script_version_id) &&
        optionalString(entry.source_scene_id) &&
        Number.isInteger(entry.story_start) &&
        optionalInteger(entry.story_end) &&
        Array.isArray(entry.causes) &&
        entry.causes.every((cause) => typeof cause === "string") &&
        Array.isArray(entry.known_by) &&
        entry.known_by.every(
          (knowledge) =>
            !!knowledge &&
            typeof knowledge.character_id === "string" &&
            Number.isInteger(knowledge.story_start) &&
            optionalInteger(knowledge.story_end),
        ),
    )
  );
}
