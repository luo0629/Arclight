import type {
  AssetMetadata,
  CharacterProfile,
  SceneProfile,
  PropProfile,
  VisualAssetType,
} from "@/types/api";

export const artKinds = {
  CHARACTER: "角色",
  SCENE: "场景",
  PROP: "道具",
  STYLE: "风格",
  VISUAL_BIBLE: "视觉规范",
} as const;
export const bibleFields = {
  art_style: "画风描述",
  line_style: "线条风格",
  render_style: "渲染风格",
  palette: "主要配色",
  lighting: "光照原则",
  composition: "构图偏好",
  prohibited: "禁止元素",
  negative_prompt: "视觉负面提示词",
} as const;
export type BibleField = keyof typeof bibleFields;
export type BibleBaseline = {
  version_id: string | null;
  version_no: number;
  fields: Record<BibleField, string>;
  locked_fields: string[];
  content_sha256: string;
};
export type ArtTarget =
  | { mode: "NEW" }
  | {
      mode: "EXISTING";
      asset_id: string;
      revision: number;
      lock_version: number;
      current_version_id: string | null;
      content_sha256: string;
    };
export type ArtDesign = { design_id: string } & (
  | {
      kind: VisualAssetType;
      name: string;
      target: ArtTarget;
      metadata?: Partial<AssetMetadata> | null;
      profile?: Partial<CharacterProfile & SceneProfile & PropProfile> | null;
    }
  | {
      kind: "VISUAL_BIBLE";
      version_id: string | null;
      version_no: number;
      content_sha256: string;
      fields: Partial<Record<BibleField, string>>;
    }
);
export type ArtSource =
  | { kind: "MANUAL"; source_id: string; statement: string }
  | {
      kind: "VERSION";
      source_id: string;
      resource_type: "SCRIPT" | "VISUAL_BIBLE" | "ASSET_VERSION";
      resource_id: string;
      content_sha256: string;
    };
export type ArtOption = {
  option_id: string;
  title: string;
  rationale: string;
  designs: ArtDesign[];
  source_ids: string[];
  reference_ids?: string[];
  prompts: {
    fragment_id: string;
    kind: "POSITIVE" | "NEGATIVE";
    text: string;
    design_ids: string[];
    source_ids: string[];
    reference_ids?: string[];
  }[];
};
export type ArtProposal = {
  schema_version?: "art-proposal/1";
  project_id: string;
  episode_id?: string | null;
  origin: "MANUAL" | "AGENT";
  visual_goal: string;
  constraints?: string[];
  sources: ArtSource[];
  references?: {
    reference_id: string;
    source_id: string;
    asset: { asset_id: string; asset_version_id: string };
    asset_type: string;
    usage: string;
    purpose: string;
  }[];
  options: ArtOption[];
  preferred_option_id?: string | null;
  questions?: {
    question_id: string;
    question: string;
    design_id?: string | null;
  }[];
  lock_conflicts?: unknown[];
  requires_human_review?: true;
};
export type ArtChange = {
  design_id: string;
  target_type: keyof typeof artKinds;
  target_id: string | null;
  field_path: string;
  before: unknown;
  proposed: unknown;
  protection: "NONE" | "APPROVED" | "LOCKED";
};
export type ArtComparison = {
  proposal_sha256: string;
  comparison_sha256: string;
  options: { option_id: string; changes: ArtChange[] }[];
  unresolved_question_ids: string[];
};
export type ArtVersion = {
  id: string;
  proposal_id: string;
  version_no: number;
  created_by: string;
  created_at: string;
  input_sha256: string;
  proposal_json: ArtProposal;
  comparison_json: ArtComparison;
  baseline_json: Record<string, unknown>;
};
export type ArtRecord = {
  id: string;
  project_id: string;
  revision: number;
  archived: boolean;
  current_version_no: number;
  version: ArtVersion;
  review_confirmation: {
    schema_version: "art-confirmation/1";
    comparison_sha256: string;
    option_id: string;
    confirmed_changes: { design_id: string; field_path: string }[];
  } | null;
  review: {
    id: string;
    request_sha256: string;
    status: string;
    blocking_code: string | null;
    snapshot: { version_id: string };
    decision: {
      id: string;
      actor_id: string;
      created_at: string;
      decision_json: { decision: "APPROVE" | "REJECT"; comment: string };
    } | null;
    resume: { result_json: { status: string } } | null;
  } | null;
  outputs: {
    id: string;
    target_type: keyof typeof artKinds;
    asset_id: string | null;
    visual_bible_version_id: string | null;
    version_no: number;
    created_by: string;
    created_at: string;
  }[];
};

export function artStatus(row: ArtRecord) {
  if (row.archived) return "已归档";
  if (row.review?.resume)
    return row.review.resume.result_json.status === "APPROVED"
      ? "已批准"
      : "已拒绝";
  if (row.review?.blocking_code) return "输入已变化";
  if (row.review?.decision) return "决定待生效";
  return row.review ? "待人工审核" : "提案草稿";
}

export function changeLabel(change: ArtChange) {
  const labels: Record<string, string> = {
    reference: "概念图参考",
    approval_and_rights: "批准精确候选并确认使用授权",
    asset: "新建设定",
    name: "名称",
    metadata: "资料",
    description: "简介",
    profile: "设定",
    holder: "持有角色",
    asset_id: "引用资产",
    asset_version_id: "引用图片版本",
    default_outfit_id: "默认服装",
    standard_image_id: "标准参考图",
    episode_ids: "指定剧集",
    fixed_features: "固定特征",
    variable_features: "可变特征",
    expressions: "表情",
    layout: "空间布局",
    time_of_day: "时间段",
    environment: "环境",
    appearance: "外观",
    story_state: "剧情状态",
    ...bibleFields,
  };
  return change.field_path
    .split(".")
    .filter((part) => part !== "fields")
    .map((part) => labels[part] ?? part)
    .join(" / ");
}

export function valueText(value: unknown, path = ""): string {
  if (value === null || value === undefined) return "无";
  const field = path.split(".").at(-1) ?? "";
  if (field === "episode_ids" && Array.isArray(value))
    return `已指定 ${value.length} 集`;
  if (/(?:^|_)id$/.test(field)) return "已指定";
  if (typeof value !== "object") return String(value) || "空白";
  if (Array.isArray(value))
    return value.map((item) => valueText(item)).join("、") || "无";
  if ("asset_version_id" in value && "rights_confirmed" in value) {
    return value.rights_confirmed === true
      ? "批准作为正式参考并确认使用授权"
      : "尚未批准作为正式参考，使用授权待确认";
  }
  if ("asset_id" in value && "asset_version_id" in value)
    return "已绑定图片版本";
  const labels: Record<string, string> = {
    name: "名称",
    metadata: "资料",
    description: "简介",
    tags: "标签",
    appearance: "外观",
    profile: "设定",
    usage_scope: "使用范围",
    kind: "范围",
    episode_ids: "剧集",
    fixed_features: "固定特征",
    variable_features: "可变特征",
    expressions: "表情",
    hairstyle: "发型",
    clothing: "服装",
    age_range: "年龄段",
    build: "体态",
    colors: "色彩",
    materials: "材质",
    ...bibleFields,
  };
  return Object.entries(value)
    .filter(([key]) => key !== "id")
    .map(
      ([key, item]) =>
        `${labels[key] ?? key}：${item === "PROJECT" ? "整个项目" : valueText(item, key)}`,
    )
    .join("；");
}
