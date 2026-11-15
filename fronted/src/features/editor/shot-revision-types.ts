export const revisionOperations = {
  RECOMPOSE: "重新构图",
  EXPRESSION: "表情调整",
  REPLACE_SCENE: "场景替换",
  REPAIR_HANDS: "手部修复",
  CHANGE_OUTFIT: "换装",
};
export type RevisionOperation = keyof typeof revisionOperations;
export type RevisionIntent = {
  candidate_id: string;
  storyboard_id: string;
  shot_id: string;
  operation: RevisionOperation;
  instruction: string;
  mode: "PROMPT_REGENERATION";
};
export type RevisionSource = {
  candidate_id: string;
  asset_id: string;
  version_id: string;
  version_no: number;
  storyboard_id: string;
  shot_id: string;
  episode_id: string;
  job_id: string;
  input_manifest_id: string;
  provider: string;
  model: string;
  snapshot_digest: string;
};
export type RevisionPreview = {
  intent: RevisionIntent;
  source: RevisionSource;
  mode_description: string;
  quote: { maximum_cost: string; currency: "USD"; pricing_version: string };
  confirmation_digest: string;
  preserved_constraints: string[];
};
export type RevisionCommand = RevisionIntent & { confirmation_digest: string };
export type RevisionReceipt = {
  job_id: string;
  input_manifest_id: string;
  source: RevisionSource;
  status: string;
  mode: "PROMPT_REGENERATION";
};
export type PendingRevision = {
  schema: 1;
  user_id: string;
  key: string;
  command: RevisionCommand;
  preview: RevisionPreview;
  phase: "unknown" | "accepted";
  receipt?: RevisionReceipt;
};
