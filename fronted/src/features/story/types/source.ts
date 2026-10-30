export type Source = {
  id: string;
  episode_id: string;
  import_no: number;
  original_name: string;
  source_type: string;
  media_type: string;
  encoding: string;
  byte_size: number;
  sha256: string;
  source_note: string;
  rights_note: string;
  created_by: string;
  created_at: string;
  provenance: {
    source_id: string;
    root_source_id: string;
    parent_source_id: string | null;
    version_no: number;
    change_note: string;
    origin_kind: "FILE" | "INLINE" | "TEXT";
    rights_status: "CONFIRMED" | "UNDECLARED";
    object_ref: {
      backend: "database" | "local" | "s3";
      key: string;
      sha256: string;
      byte_size: number;
    };
  };
};
export type SourceDetail = Source & { text: string };
