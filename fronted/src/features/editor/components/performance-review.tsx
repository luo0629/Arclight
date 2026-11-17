"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { FormLabel, Notice } from "@/components/ui-kit";
import usePagedList from "@/hooks/use-paged-list";
import { api, message } from "@/lib/api/client";
import type { ContentVersion } from "@/types/api";

export type PerformanceReviewInput = {
  intentOnly: boolean;
  comment: string;
};

export function PerformanceIntentFields({
  value,
  onChange,
  disabled,
  showComment = true,
}: {
  value: PerformanceReviewInput;
  onChange: (value: PerformanceReviewInput) => void;
  disabled: boolean;
  showComment?: boolean;
}) {
  return (
    <fieldset className="flex min-w-0 flex-col gap-3 rounded-lg border p-3 text-sm">
      <legend>表演意图与制作条件</legend>
      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={value.intentOnly}
          disabled={disabled}
          onChange={(event) =>
            onChange({ ...value, intentOnly: event.target.checked })
          }
        />
        仅确认表演意图，保留制作阻塞
      </label>
      <p>
        此决定只确认本版表演安排，不代表动态、口型或生成能力已通过验收。
        制作阻塞将随决定保存；须另存新版本并重新审核制作能力后才能解除。
      </p>
      {value.intentOnly && showComment && (
        <FormLabel>
          表演意图确认理由（必填）
          <Textarea
            value={value.comment}
            disabled={disabled}
            required
            maxLength={2000}
            onChange={(event) =>
              onChange({ ...value, comment: event.target.value })
            }
            placeholder="说明为何确认表演意图，并保留当前制作阻塞"
          />
        </FormLabel>
      )}
      {value.intentOnly && !value.comment.trim() && (
        <p role="status">请填写理由后再确认表演意图。</p>
      )}
    </fieldset>
  );
}

type IntentReview = {
  id: string;
  snapshot: { version_id: string };
  decision: {
    actor_id: string;
    created_at: string;
    decision_json: {
      version_id: string;
      decision: "APPROVE" | "REJECT";
      comment: string;
      performance_intent?: {
        scope: "INTENT_ONLY";
        production_ready: false;
        blockers: { code: string; message: string; slot?: number | null }[];
      } | null;
    };
  } | null;
};

export function PerformanceIntentReceipt({
  projectId,
  versionId,
}: {
  projectId: string;
  versionId: string;
}) {
  const [review, setReview] = useState<IntentReview | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setReview(null);
    setLoaded(false);
    setError("");
    void api<IntentReview[]>(
      `/projects/${projectId}/human-reviews?version_id=${versionId}&limit=100`,
      { signal: controller.signal },
    )
      .then((rows) => {
        if (controller.signal.aborted) return;
        setReview(
          rows.find(
            (row) =>
              row.snapshot.version_id === versionId &&
              row.decision?.decision_json.version_id === versionId &&
              row.decision.decision_json.decision === "APPROVE" &&
              row.decision.decision_json.performance_intent?.scope ===
                "INTENT_ONLY" &&
              row.decision.decision_json.performance_intent.production_ready ===
                false,
          ) ?? null,
        );
        setLoaded(true);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [projectId, versionId, attempt]);
  if (error)
    return (
      <Notice tone="error" role="alert">
        表演审核决定读取失败：{error}
        <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>
          重试读取表演审核决定
        </Button>
      </Notice>
    );
  if (!loaded) return <p role="status">正在核对表演审核决定…</p>;
  const decision = review?.decision;
  const intent = decision?.decision_json.performance_intent;
  if (!decision || !intent) return null;
  return (
    <section
      className="flex min-w-0 flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 text-sm"
      aria-label="表演意图审核决定"
    >
      <h4 className="font-medium">仅确认表演意图 · 制作仍阻塞</h4>
      <p>确认理由：{decision.decision_json.comment}</p>
      <p>决定时间：{new Date(decision.created_at).toLocaleString("zh-CN")}</p>
      <p>本次决定保留的制作阻塞：</p>
      <ul className="list-disc space-y-1 pl-5">
        {intent.blockers.map((blocker, index) => (
          <li key={`${blocker.code}:${index}`}>{blocker.message}</li>
        ))}
      </ul>
      <p>
        此精确版本仍不可进入制作。请另存新版本并重新审核制作能力；历史决定与阻塞快照会保留。
      </p>
    </section>
  );
}

export function StoryboardPerformanceApproval({
  episodeId,
  versionId,
  value,
  disabled,
  onChange,
  onReady,
}: {
  episodeId: string;
  versionId: string;
  value: PerformanceReviewInput;
  disabled: boolean;
  onChange: (value: PerformanceReviewInput) => void;
  onReady: (versionId: string, supportsIntent: boolean) => void;
}) {
  const boards = usePagedList<ContentVersion>(
    `/episodes/${episodeId}/storyboards`,
  );
  const { ensure } = boards;
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setError("");
    void ensure(versionId)
      .then((rows) => {
        if (!live) return;
        const exact = rows.find((row) => row.id === versionId);
        if (!exact || exact.episode_id !== episodeId)
          throw new Error("未找到本任务待审的精确分镜版本。");
        onReady(versionId, exact.content_json.schema_version === "3.0");
      })
      .catch((cause: unknown) => {
        if (live) setError(message(cause));
      });
    return () => {
      live = false;
    };
  }, [episodeId, versionId, ensure, onReady, attempt]);
  if (error)
    return (
      <Notice tone="error" role="alert">
        审核版本读取失败：{error}
        <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>
          重试读取审核版本
        </Button>
      </Notice>
    );
  const version = boards.rows.find((row) => row.id === versionId);
  if (!version) return <p role="status">正在核对精确分镜版本…</p>;
  return version.content_json.schema_version === "3.0" ? (
    <PerformanceIntentFields
      value={value}
      onChange={onChange}
      disabled={disabled}
      showComment={false}
    />
  ) : null;
}
