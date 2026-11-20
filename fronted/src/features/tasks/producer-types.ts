export type ProducerLimits = {
  schema_version?: 1;
  max_steps?: number;
  max_calls?: number;
  max_model_calls?: number;
  max_tool_calls?: number;
  max_agent_calls?: number;
  max_duration_seconds?: number;
  max_delegations?: number;
  max_delegation_depth?: number;
  maximum_cost?: string | null;
};

export type ProducerStartPayload = { goal: string; limits?: ProducerLimits };
export type SeriesEpisode = {
  episode_id: string;
  requires_episode_ids: string[];
};
export type SeriesStartPayload = ProducerStartPayload & {
  scope: {
    series_plan_version_id: string;
    ledger_version_id: string | null;
    expected_ledger_head_revision: number;
    episodes: SeriesEpisode[];
    confirmation: string;
  };
};
export type SeriesControlPayload = {
  action: "PAUSE" | "RESUME";
  expected_revision: number;
  reason: string;
};
export type ProducerSeriesProgress = {
  scope: {
    request: SeriesStartPayload["scope"];
    target_episode_count: number;
    episodes: (SeriesEpisode & {
      episode_no: number;
      target_duration_ms: number;
    })[];
  };
  paused: boolean;
  completion_current: boolean;
  whole_series_complete: boolean;
  completion_blocking_code: string | null;
  completion: {
    payload: {
      scope: "BATCH" | "SERIES";
      deliveries: { episode_id: string; artifact_id: string }[];
      narrative_report_id: string;
    };
  } | null;
};
export type ProducerCommandPayload =
  | {
      action: "DELEGATE";
      expected_revision: number;
      plan_id: string;
      plan_sha256: string;
      task_key: string;
    }
  | {
      action: "REPLAN";
      expected_revision: number;
      observation: {
        base_plan_id: string;
        base_plan_sha256: string;
        trigger: "FAILURE" | "STALE" | "BUDGET_CHANGED" | "RECHECK";
        reason: string;
      };
    }
  | { action: "RECOVER"; expected_revision: number; command_id: string }
  | { action: "CANCEL"; expected_revision: number };

export type ProducerAnswerPayload = {
  question_id: string;
  input_sha256: string;
} & ({ decision: "ANSWER"; answer: string } | { decision: "STOP" });

export type ProducerReceipt = {
  command_id: string;
  revision: number;
  action: string;
  status: string;
  error_code: string | null;
};

export type ProducerPlan = {
  plan_id: string;
  plan_sha256: string;
  version_no: number;
  summary: string;
  previous_plan_id: string | null;
  reason: string | null;
  tasks: {
    task_key: string;
    episode_id?: string;
    kind: "screenwriter" | "director" | "art_director" | "continuity_reviewer";
    reason: string;
    depends_on: string[];
    child_run_id: string | null;
    status: string | null;
    error_code: string | null;
    unknown_external_result: boolean;
    can_delegate: boolean;
  }[];
};

export type ProducerOperation = {
  loop_id: string;
  kind: "plan" | "replan";
  status: string;
  stop_code: string | null;
  model_turns: number;
  question_id: string | null;
  input_sha256: string;
  human_request: { question: string; reason: string; options: string[] } | null;
  can_answer: boolean;
};

export type ProducerProgress = {
  run_id: string;
  episode_id: string;
  project_id: string;
  status: string;
  revision: number;
  goal: string;
  can_operate: boolean;
  readonly_reason: string | null;
  series?: ProducerSeriesProgress | null;
  can_recover: boolean;
  can_replan: boolean;
  can_cancel: boolean;
  pending_command: ProducerReceipt | null;
  commands: ProducerReceipt[];
  plans: ProducerPlan[];
  operations: ProducerOperation[];
  budget: {
    limits: ProducerLimits;
    deadline_at: string | null;
    actual_cost: string;
    pending_estimate: string;
    unknown_cost: boolean;
    model_calls: number;
    tool_calls: number;
    delegations: number;
  };
};

export type ProducerAccepted = { run_id: string; status: string };
