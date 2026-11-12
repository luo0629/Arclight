export type VisualAssetType = "CHARACTER" | "SCENE" | "PROP" | "STYLE";
export type AssetVersion = {
  dependency_manifest_id?: string | null;
  id: string;
  asset_id: string;
  version_no: number;
  checksum: string;
  created_by: string;
  created_at: string;
  status: string;
  stale: boolean;
  revision: number;
  archived_from_status: string | null;
  media: {
    media_type: string;
    byte_size: number;
    width: number;
    height: number;
    original_name: string;
    source_note: string;
    rights_note: string;
  } | null;
  asset_snapshot: VisualAsset | null;
  restored_from_version_id: string | null;
  restoration_note: string | null;
  provenance: {
    kind: "MANUAL_IMPORT" | "EXTERNAL_DECLARATION";
    verification: string;
    width: number;
    height: number;
    generation: {
      provider: string;
      model: string;
      prompt: string;
      negative_prompt: string;
      prompt_version: string | null;
      seed: number | null;
      parameters: Record<string, string | number | boolean>;
    } | null;
    references: (ExactAssetReference & {
      asset_name: string;
      version_no: number;
      checksum: string;
    })[];
  } | null;
};
export type ExactAssetReference = {
  asset_id: string;
  asset_version_id: string;
};
export type SceneProfile = {
  layout: string;
  time_of_day: string;
  lighting: string;
  palette: string;
  environment: string;
  reference: ExactAssetReference | null;
};
export type PropProfile = {
  appearance: string;
  story_state: string;
  holder: ExactAssetReference | null;
};
export type ReferenceImage = {
  id: string;
  asset_id: string;
  asset_name: string;
  asset_type: string;
  version_no: number;
  status: string;
  stale: boolean;
};
export type CharacterProfile = {
  fixed_features: string[];
  variable_features: string[];
  expressions: string[];
  outfits: { id: string; name: string; description: string }[];
  default_outfit_id: string | null;
  images: {
    id: string;
    label: string;
    kind: "INITIAL" | "ANGLE";
    angle: string;
    source: ExactAssetReference;
  }[];
  standard_image_id: string | null;
};
export type AssetMetadata = {
  description: string;
  tags: string[];
  appearance: Record<
    | "age_range"
    | "appearance"
    | "hairstyle"
    | "clothing"
    | "build"
    | "expressions"
    | "colors"
    | "materials",
    string
  >;
  usage_scope: { kind: "PROJECT" | "EPISODES"; episode_ids: string[] };
};
export type VisualAsset = {
  id: string;
  project_id: string;
  asset_type: VisualAssetType;
  name: string;
  status: string;
  current_version_id: string | null;
  lock_version: number;
  revision: number;
  metadata: AssetMetadata;
  creation_source: { kind: "MANUAL" | "IMPORTED"; note: string } | null;
  created_by: string | null;
  character_profile: CharacterProfile | null;
  scene_profile: SceneProfile | null;
  prop_profile: PropProfile | null;
  created_at: string;
  updated_at: string;
};
export type StoryKind = "WORLD" | "CHARACTER" | "LOCATION" | "PROP" | "SCENE";
export type StoryRelation = {
  relation: "governs" | "located_in" | "appears_in" | "uses";
  target_id: string;
};
export type StoryEntry = {
  id: string;
  project_id: string;
  kind: StoryKind;
  name: string;
  aliases: string[];
  description: string;
  rules: string[];
  relations: StoryRelation[];
  asset: { asset_id: string; asset_version_id: string } | null;
  archived: boolean;
  revision: number;
  created_by: string;
};
export type User = { id: string; email: string; display_name: string };
export type Session = { access_token: string; user: User };
export type ProjectStorySettings = {
  schema_version: 1;
  description: string;
  genre: string;
  target_audience: string;
  content_restrictions: string;
  default_episode_duration_ms: number;
  target_episode_count: number;
  episode_plan: string;
};
export type Project = {
  story_settings: ProjectStorySettings;
  id: string;
  name: string;
  owner_id: string;
  archived: boolean;
  budget_limit: string;
  style_guide: Record<string, unknown>;
  output_profile: Record<string, unknown>;
};
export type Episode = {
  id: string;
  project_id: string;
  episode_no: number;
  title: string;
  status: string;
  target_duration_ms: number;
};
export type ContentVersion = {
  id: string;
  episode_id: string;
  version_no: number;
  content_json: Record<string, unknown>;
  status: string;
  run_id: string | null;
  script_version_id?: string;
};
export type Run = {
  workflow_version?: string;
  episode_id: string;
  workflow_type: string;
  id: string;
  status: string;
  revision: number;
  pending_review:
    | {
        resource_type: "script" | "storyboard" | "art_proposal";
        review_request_id?: string;
        version_id: string;
      }
    | {
        resource_type: "continuity_review";
        destination?: string;
        candidate_id?: string;
        review_id?: string;
      }
    | null;
  error_code: string | null;
  created_at: string;
};
export type Member = {
  user_id: string;
  email: string;
  display_name: string;
  role: "editor" | "viewer";
};
export type GraphNode = {
  id: string;
  kind: string;
  resource_id?: string;
  title: string;
  status: string;
  version_no?: number;
  duration_ms?: number;
  description?: string;
  asset_id?: string;
  asset_type?: string;
  stale?: boolean;
  current?: boolean;
  checksum?: string;
  dependency_manifest_id?: string | null;
  preview_path?: string | null;
};
export type Layout = {
  revision: number;
  positions: Record<string, { x: number; y: number }>;
  viewport: { x: number; y: number; zoom: number };
};
export type Graph = {
  nodes: GraphNode[];
  edges: {
    id: string;
    source: string;
    target: string;
    relation?: string;
    dependency_id?: string;
    dependency_type?: string;
    dependency_manifest_id?: string | null;
    label?: string;
  }[];
  layout: Layout;
};
