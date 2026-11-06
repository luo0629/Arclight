"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Notice } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import type { Run } from "@/types/api";
import DirectorRevisionDetails, {
  type DirectorCheck,
  type DirectorRevision,
} from "./director-revision-details";

type Scene = {
  scene_id: string;
  scene_no: number;
  title: string;
  action: string;
  dialogue: { line_id: string; speaker: string; text: string }[];
};
type Working = {
  scenes?: {
    scene_id: string;
    dramatic_goal: string;
    emotion: string;
    staging: string[];
    rhythm: string[];
    estimated_duration_ms: number;
  }[];
  shots?: {
    shot_no: number;
    duration_ms: number;
    visual_description: string;
    characters: string[];
    dialogue_line_ids: string[];
  }[];
  decision?: "ACCEPT" | "REVISE" | "ESCALATE";
  reason?: string;
  checks?: string[];
  issues?: Issue[];
  objective?: string;
  goals?: string[];
  plan?: {
    mode: string;
    scenes?: {
      scene_id: string;
      title: string;
      goal: string;
      beats: string[];
      estimated_duration_ms: number;
    }[];
    outline?: {
      episodes: {
        episode_no: number;
        title: string;
        goal: string;
        conflict: string;
        turning_point: string;
        ending_hook: string;
      }[];
    };
  };
  scene?: Scene;
  estimated_duration_ms?: number;
  evidence?: {
    manifest_id: string;
    source_id: string;
    content_sha256: string;
  }[];
};
type Issue = {
  category: string;
  scene_id: string | null;
  line_id: string | null;
  problem: string;
  evidence: string;
  suggestion: string;
};
type RuleIssue = { code: string; message: string };
type Revision = {
  round: number;
  remaining_patches: number;
  source_sha256: string;
  deterministic_issues: RuleIssue[];
  previous_issues: Issue[];
  working_script: { title: string; scenes: Scene[] };
  patch_checks: {
    accepted: boolean;
    patch_sha256: string;
    issues: RuleIssue[];
  }[];
};
type Operation = {
  observations?: {
    tool: string;
    feedback: {
      status: string;
      result?: {
        materials?: { source_kind: string; source_id: string }[];
        missing?: { reason: string }[];
      } & Partial<DirectorCheck>;
    };
  }[];
  loop_id: string;
  kind: "plan" | "scene" | "critique" | "director_check";
  revision?: Revision;
  director_revision?: DirectorRevision;
  scene_no: number | null;
  status: string;
  result: Working | null;
  stop_code: string | null;
  model_turns: number;
  question_id: string | null;
  input_sha256: string;
  human_request: { question: string; reason: string; options: string[] } | null;
};
type Progress = { operations: Operation[]; can_cancel: boolean };

export default function ScreenwriterProgress({
  run,
  writable,
  busy,
  act,
  reload,
  agent = "screenwriter",
}: {
  agent?: "screenwriter" | "director";
  run: Run;
  writable: boolean;
  busy: boolean;
  act: (action: () => Promise<unknown>) => Promise<boolean>;
  reload: () => Promise<void>;
}) {
  const [data, setData] = useState<Progress | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const result = await api<Progress>(`/runs/${run.id}/${agent}`);
        if (!cancelled) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(message(e));
      }
      if (!cancelled && ["QUEUED", "RUNNING"].includes(run.status))
        timer = setTimeout(() => void load(), 1500);
    }
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [run.id, run.status, run.revision, refresh, agent]);
  const title =
    agent === "director" ? "导演计划与镜头工作稿" : "编剧计划与工作稿";
  return (
    <section aria-label={title} className="flex min-w-0 flex-col gap-3">
      <h4>{title}</h4>
      <p className="text-sm text-muted-foreground">
        工作片段会逐段保存；完整候选通过校验后进入下方审核。
      </p>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {!data && !error && <p role="status">正在读取创作进度…</p>}
      {data && data.operations.length === 0 && (
        <p role="status">任务排队后开始调查与规划。</p>
      )}
      {data?.operations.map((operation) => (
        <details
          key={operation.loop_id}
          open={operation.status === "WAITING_HUMAN"}
        >
          <summary>
            {operation.kind === "plan"
              ? agent === "director"
                ? "场景调度与节奏计划"
                : "故事计划"
              : operation.kind === "critique"
                ? `评价与修订 ${(operation.revision?.round ?? 0) + 1}`
                : operation.kind === "director_check"
                  ? `导演检查与修订 ${(operation.director_revision?.round ?? 0) + 1}`
                  : `场景 ${operation.scene_no}`}{" "}
            ·{" "}
            {operation.status === "COMPLETED"
              ? "已保存"
              : operation.status === "WAITING_HUMAN"
                ? run.status === "CANCELLED"
                  ? "已停止"
                  : "等待回答"
                : operation.status === "STOPPED"
                  ? "已停止"
                  : operation.status === "RECOVERY_REQUIRED"
                    ? "等待核验原调用"
                    : "创作中"}
          </summary>
          <div className="flex flex-col gap-3 py-3">
            {operation.revision && <RevisionDetails operation={operation} />}
            {operation.director_revision && (
              <DirectorRevisionDetails
                revision={operation.director_revision}
                decision={operation.result?.decision}
                reason={operation.result?.reason}
                checks={(operation.observations ?? [])
                  .filter(
                    (item) =>
                      item.tool === "check_director_storyboard" &&
                      item.feedback.status === "SUCCEEDED" &&
                      item.feedback.result?.check_sha256,
                  )
                  .map((item) => item.feedback.result as DirectorCheck)}
              />
            )}
            {operation.result?.objective && <p>{operation.result.objective}</p>}
            {operation.result?.scenes && (
              <ol className="list-decimal pl-5">
                {operation.result.scenes.map((scene) => (
                  <li key={scene.scene_id}>
                    <strong>{scene.dramatic_goal}</strong> ·{" "}
                    {scene.estimated_duration_ms / 1000} 秒
                    <p>情绪：{scene.emotion}</p>
                    <p>调度：{scene.staging.join("；")}</p>
                    <p>节奏：{scene.rhythm.join("；")}</p>
                  </li>
                ))}
              </ol>
            )}
            {operation.result?.shots && (
              <ol className="list-decimal pl-5">
                {operation.result.shots.map((shot) => (
                  <li key={shot.shot_no}>
                    <strong>场景内镜头 {shot.shot_no}</strong> ·{" "}
                    {shot.duration_ms / 1000} 秒<p>{shot.visual_description}</p>
                    <p>
                      出镜：{shot.characters.join("、") || "无"} · 台词引用{" "}
                      {shot.dialogue_line_ids.length} 条
                    </p>
                  </li>
                ))}
              </ol>
            )}
            {!!operation.observations?.length && (
              <details>
                <summary>
                  调查与工具反馈（{operation.observations.length}）
                </summary>
                <ul>
                  {operation.observations.map((observation, i) => (
                    <li key={i}>
                      调查 {i + 1}：
                      {observation.feedback.status === "SUCCEEDED"
                        ? "成功"
                        : "未成功"}
                      {observation.feedback.result?.materials?.map((item) => (
                        <p key={item.source_id}>
                          {(
                            {
                              SCRIPT: "精确剧本",
                              VISUAL: "视觉规范",
                              ASSET: "精确资产",
                              CAPABILITY: "生产能力声明",
                            } as Record<string, string>
                          )[item.source_kind] ?? item.source_kind}
                        </p>
                      ))}
                      {observation.feedback.result?.missing?.map(
                        (item, index) => (
                          <p key={index}>缺少材料：{item.reason}</p>
                        ),
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {operation.result?.goals && (
              <ul className="list-disc pl-5">
                {operation.result.goals.map((goal, i) => (
                  <li key={i}>{goal}</li>
                ))}
              </ul>
            )}
            {operation.result?.plan?.scenes && (
              <ol className="list-decimal pl-5">
                {operation.result.plan.scenes.map((scene) => (
                  <li key={scene.scene_id}>
                    <strong>{scene.title}</strong> ·{" "}
                    {scene.estimated_duration_ms / 1000} 秒<p>{scene.goal}</p>
                    <p>{scene.beats.join("；")}</p>
                  </li>
                ))}
              </ol>
            )}
            {operation.result?.plan?.outline?.episodes && (
              <ol className="list-decimal pl-5">
                {operation.result.plan.outline.episodes.map((episode) => (
                  <li key={episode.episode_no}>
                    <strong>{episode.title}</strong>
                    <p>{episode.goal}</p>
                    <p>冲突：{episode.conflict}</p>
                    <p>转折：{episode.turning_point}</p>
                    <p>结尾：{episode.ending_hook}</p>
                  </li>
                ))}
              </ol>
            )}
            {operation.result?.scene && (
              <>
                <strong>{operation.result.scene.title}</strong>
                <p>{operation.result.scene.action}</p>
                {operation.result.scene.dialogue.map((line) => (
                  <p key={line.line_id}>
                    {line.speaker}：{line.text}
                  </p>
                ))}
              </>
            )}
            {operation.result?.evidence?.length ? (
              <details>
                <summary>
                  调查来源（{operation.result.evidence.length}）
                </summary>
                <ul>
                  {operation.result.evidence.map((evidence, i) => (
                    <li key={i} className="break-all text-xs">
                      来源 {evidence.source_id} · 调查 {evidence.manifest_id} ·
                      SHA-256 {evidence.content_sha256}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
            {operation.stop_code && (
              <Notice>
                停止原因：{operation.stop_code}
                。可使用本任务的重试入口；已有成功片段会保留。
              </Notice>
            )}
            {operation.human_request && (
              <Answer
                key={operation.question_id}
                operation={operation}
                run={run}
                disabled={!writable || busy || run.status !== "RUNNING"}
                canStop={data.can_cancel}
                stop={() =>
                  act(async () => {
                    await api(`/runs/${run.id}/cancel`, json("POST"));
                    setRefresh((value) => value + 1);
                    await reload();
                  })
                }
                submit={(answer) =>
                  act(async () => {
                    await api(
                      `/runs/${run.id}/${agent}/${operation.loop_id}/answers`,
                      json("POST", {
                        question_id: operation.question_id,
                        input_sha256: operation.input_sha256,
                        decision: "ANSWER",
                        answer,
                      }),
                    );
                    setRefresh((value) => value + 1);
                    await reload();
                  })
                }
              />
            )}
          </div>
        </details>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setRefresh((value) => value + 1)}
      >
        刷新创作进度
      </Button>
    </section>
  );
}

const categories: Record<string, string> = {
  CAUSALITY: "因果",
  MOTIVATION: "人物动机",
  DIALOGUE: "对白",
  SETTING: "设定",
  DETERMINISTIC: "规则检查",
};
function RevisionDetails({ operation }: { operation: Operation }) {
  const revision = operation.revision!;
  const issues = operation.result?.issues ?? revision.previous_issues;
  return (
    <>
      <p>剩余自动修订：{revision.remaining_patches} 轮</p>
      {operation.result?.checks && (
        <p>
          已检查：
          {operation.result.checks
            .map((item) => categories[item] ?? item)
            .join("、")}
        </p>
      )}
      {operation.result?.decision && (
        <p>
          {operation.result.decision === "ACCEPT"
            ? "评价与规则检查通过，交完整稿审核。"
            : operation.result.decision === "REVISE"
              ? "已采用范围内补丁，继续检查。"
              : "保留问题，请你决定。"}
        </p>
      )}
      {issues.length > 0 && (
        <ul className="list-disc pl-5">
          {issues.map((issue, index) => (
            <li key={index}>
              <strong>
                {categories[issue.category] ?? issue.category}：{issue.problem}
              </strong>
              <p>
                位置：
                {issue.scene_id
                  ? (() => {
                      const scene = revision.working_script.scenes.find(
                        (item) => item.scene_id === issue.scene_id,
                      );
                      const line = scene?.dialogue.find(
                        (item) => item.line_id === issue.line_id,
                      );
                      return `场景 ${scene?.scene_no} · ${scene?.title}${line ? ` · 台词：${line.text}` : ""}`;
                    })()
                  : "全稿规则"}
              </p>
              <p>依据：{issue.evidence}</p>
              <p>建议：{issue.suggestion}</p>
            </li>
          ))}
        </ul>
      )}
      {revision.deterministic_issues.map((issue, index) => (
        <Notice key={index}>{issue.message}</Notice>
      ))}
      {revision.patch_checks.map((check, index) => (
        <div key={check.patch_sha256 + index}>
          <p>
            补丁检查 {index + 1}：{check.accepted ? "通过" : "未通过"}
          </p>
          {check.issues.map((issue, i) => (
            <Notice key={i}>{issue.message}</Notice>
          ))}
        </div>
      ))}
      <details>
        <summary>查看本轮工作稿</summary>
        <strong>{revision.working_script.title}</strong>
        {revision.working_script.scenes.map((scene) => (
          <section
            key={scene.scene_no}
            aria-label={`工作场景 ${scene.scene_no}`}
          >
            <h5>
              {scene.scene_no} · {scene.title}
            </h5>
            <p>{scene.action}</p>
            {scene.dialogue.map((line) => (
              <p key={line.line_id}>
                {line.speaker}：{line.text}
              </p>
            ))}
          </section>
        ))}
        <p className="break-all text-xs">
          工作稿 SHA-256：{revision.source_sha256}
        </p>
      </details>
    </>
  );
}

function Answer({
  operation,
  run,
  disabled,
  canStop,
  stop,
  submit,
}: {
  operation: Operation;
  run: Run;
  disabled: boolean;
  canStop: boolean;
  stop: () => Promise<boolean>;
  submit: (answer: string) => Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const question = operation.human_request!;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && answer.trim()) void submit(answer);
      }}
    >
      <FieldGroup>
        <Notice>{question.question}</Notice>
        <Field data-disabled={disabled}>
          <FieldLabel htmlFor={`answer-${run.id}-${operation.loop_id}`}>
            创作问题回答
          </FieldLabel>
          {question.options.length ? (
            <ToggleGroup
              type="single"
              variant="outline"
              aria-label="创作问题回答"
              disabled={disabled}
              value={answer}
              onValueChange={setAnswer}
            >
              {question.options.map((option) => (
                <ToggleGroupItem key={option} value={option}>
                  {option}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : (
            <Textarea
              id={`answer-${run.id}-${operation.loop_id}`}
              value={answer}
              disabled={disabled}
              maxLength={1000}
              required
              onChange={(e) => setAnswer(e.target.value)}
            />
          )}
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={disabled || !answer.trim()}>
            提交回答并继续
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={disabled || !canStop}
            onClick={() => void stop()}
          >
            停止本次创作
          </Button>
        </div>
      </FieldGroup>
    </form>
  );
}
