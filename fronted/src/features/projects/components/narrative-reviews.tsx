"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { LoadingState, Notice } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import { episodePath, isResourceId } from "@/lib/routes";
import { useProject } from "../project-context";

type Location = {
  episode_id: string;
  episode_no: number;
  script_version_id: string;
  scene_id: string;
  line_id: string | null;
  shot_id: string | null;
  candidate_id: string | null;
  time_ms: number | null;
};
type Issue = {
  severity: "WARNING" | "BLOCKER";
  summary: string;
  location: Location;
  evidence_ids: string[];
};
type Review = {
  question?: {
    loop_id: string;
    question_id: string;
    input_sha256: string;
    request: { question: string; options: string[] };
  } | null;
  status: string;
  blocking_code: string | null;
  error_code: string | null;
  work: {
    project_id: string;
    run_id: string;
    request: Record<string, unknown>;
    evidence: {
      id: string;
      kind: string;
      location: Location | null;
      value: unknown;
    }[];
  };
  report: {
    report_id: string;
    input_sha256: string;
    conclusion: string;
    limitations: string[];
    previous_report_id: string | null;
    assessment: {
      summary: string;
      checks: {
        category: string;
        status: string;
        summary: string;
        evidence_ids: string[];
        issues: Issue[];
      }[];
    } | null;
  } | null;
  human_review: {
    id: string;
    request_sha256: string;
    status: string;
    blocking_code: string | null;
    snapshot: { version_id: string };
    decision: {
      id: string;
      decision_json: { decision: string; comment: string };
    } | null;
    resume: { id: string; result_json: { status: string } } | null;
  } | null;
};
const categories: Record<string, string> = {
  CAUSALITY: "因果",
  MOTIVATION_RELATIONSHIP: "人物动机与关系",
  KNOWLEDGE: "角色知情",
  FORESHADOWING: "伏笔生命周期",
  APPEARANCE_VOICE: "外观与声音变化",
  PERFORMANCE: "镜头表演",
};
const scopes: Record<string, string> = {
  SCENE: "场景",
  ADJACENT_EPISODES: "相邻集",
  SERIES: "全剧",
};
const evidenceLabels: Record<string, string> = {
  PLAN: "连续剧规划",
  SCENE: "场景剧本",
  AUTHOR_STATE: "故事事实",
  CHARACTER_STATE: "角色知情",
  STORYBOARD: "分镜",
  VIDEO: "视频",
  AUDIO: "声音",
};
const initial = JSON.stringify(
  {
    scope: "SCENE",
    baseline: {
      series_plan_version_id: "",
      ledger_version_id: null,
      expected_ledger_head_revision: 0,
    },
    targets: [{ script_version_id: "", scene_ids: [""] }],
  },
  null,
  2,
);

export default function NarrativeReviews() {
  const { project, owner, writable } = useProject();
  const params = useSearchParams();
  const requested = params.get("narrative_run");
  const [selected, setSelected] = useState(
    requested && isResourceId(requested) ? requested : "",
  );
  const [draft, setDraft] = useState(initial);
  const [history, setHistory] = useState<
    { run_id: string; status: string; scope: string }[]
  >([]);
  const [offset, setOffset] = useState(0);
  const [value, setValue] = useState<Review | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const [refresh, setRefresh] = useState(0);
  const creation = useRef({ draft: "", key: "" });
  const working = useRef(false);
  const id = useId();
  const endpoint = `/projects/${project.id}/narrative-reviews`;
  useEffect(() => {
    if (requested && isResourceId(requested)) setSelected(requested);
  }, [requested]);
  useEffect(() => {
    const abort = new AbortController();
    setError("");
    void api<typeof history>(`${endpoint}?limit=20&offset=${offset}`, {
      signal: abort.signal,
    })
      .then((rows) => {
        if (!abort.signal.aborted) setHistory(rows);
      })
      .catch((e: unknown) => {
        if (!abort.signal.aborted) setError(message(e));
      });
    return () => abort.abort();
  }, [endpoint, offset, refresh]);
  useEffect(() => {
    const abort = new AbortController();
    setValue(null);
    setAnswer("");
    if (selected)
      void api<Review>(`${endpoint}/${selected}`, { signal: abort.signal })
        .then((row) => {
          if (
            !abort.signal.aborted &&
            row.work.run_id === selected &&
            row.work.project_id === project.id
          )
            setValue(row);
        })
        .catch((e: unknown) => {
          if (!abort.signal.aborted) setError(message(e));
        });
    return () => abort.abort();
  }, [endpoint, selected, refresh, project.id]);
  async function mutate(path: string, body: unknown) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError("");
    try {
      await api(path, json("POST", body));
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      working.current = false;
      setBusy(false);
    }
  }
  async function create() {
    if (!writable || working.current) return;
    working.current = true;
    setBusy(true);
    setError("");
    try {
      const body = JSON.parse(draft) as Record<string, unknown>;
      if (creation.current.draft !== draft)
        creation.current = { draft, key: crypto.randomUUID() };
      const run = await api<{ run_id: string }>(
        endpoint,
        json("POST", { ...body, creation_key: creation.current.key }),
      );
      setSelected(run.run_id);
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(
        e instanceof SyntaxError
          ? "请求不是有效 JSON，请核对括号与版本编号。"
          : message(e),
      );
    } finally {
      working.current = false;
      setBusy(false);
    }
  }
  return (
    <section
      aria-label="叙事连续性审查"
      className="flex min-w-0 flex-col gap-4 border-b pb-6"
    >
      <h3 className="text-lg font-semibold">叙事连续性审查</h3>
      <p className="text-sm text-muted-foreground">
        按精确版本检查场景、相邻集或全剧。机器报告仅提供建议，不能替代人工连续观片或整剧交付批准。
      </p>
      <details>
        <summary className="cursor-pointer">发起精确叙事审查</summary>
        <div className="mt-3 flex flex-col gap-3">
          <p className="text-sm">
            从原工作区复制版本编号。全剧范围必须提供规划中的全部剧集；相邻集须提供连续两集且不指定
            scene_ids。可在各目标中显式添加
            storyboard_version_id、video_candidate_ids、audio_version_ids。
          </p>
          <Field>
            <FieldLabel htmlFor={`${id}-request`}>
              精确送审请求（JSON）
            </FieldLabel>
            <Textarea
              id={`${id}-request`}
              value={draft}
              maxLength={200000}
              rows={12}
              disabled={!writable || busy}
              onChange={(e) => setDraft(e.target.value)}
            />
          </Field>
          <Notice>
            发起后使用当前模型服务。缺少视频或声音证据将明确转人工，不自动选用最新版本。
          </Notice>
          <Button disabled={!writable || busy} onClick={() => void create()}>
            开始叙事审查
          </Button>
        </div>
      </details>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => setRefresh((n) => n + 1)}
        >
          刷新叙事审查
        </Button>
        <Button
          variant="outline"
          disabled={!offset || busy}
          onClick={() => setOffset(offset - 20)}
        >
          上一页叙事审查
        </Button>
        <Button
          variant="outline"
          disabled={history.length < 20 || busy}
          onClick={() => setOffset(offset + 20)}
        >
          下一页叙事审查
        </Button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      <ul className="flex flex-col gap-2" aria-label="叙事审查历史">
        {history.map((row, index) => (
          <li key={row.run_id}>
            <Button
              variant="ghost"
              aria-pressed={selected === row.run_id}
              onClick={() => setSelected(row.run_id)}
            >
              {scopes[row.scope] ?? row.scope} · {row.status} · 第 {index + 1}{" "}
              条
            </Button>
          </li>
        ))}
      </ul>
      {selected && !value && !error && (
        <LoadingState>正在读取精确叙事报告…</LoadingState>
      )}
      {value && (
        <div aria-label="精确叙事报告" className="flex min-w-0 flex-col gap-3">
          <p>
            运行状态：{value.status} · 机器结论：
            {value.report?.conclusion ?? "尚未产生报告，请稍后刷新"}
          </p>
          {(value.blocking_code || value.error_code) && (
            <Notice tone="error">
              当前受阻：{value.blocking_code ?? value.error_code}
              。旧报告仅供查阅，请核对输入后重新送审。
            </Notice>
          )}
          {value.report?.assessment && (
            <>
              <p>{value.report.assessment.summary}</p>
              {value.report.assessment.checks.map((check) => (
                <section key={check.category} className="border-t pt-3">
                  <h4 className="font-semibold">
                    {categories[check.category]} · {check.status}
                  </h4>
                  <p>{check.summary}</p>
                  {check.issues.map((issue, i) => (
                    <div key={i} className="mt-2 text-sm">
                      <p>
                        {issue.severity === "BLOCKER" ? "阻塞" : "警告"}：
                        {issue.summary}
                      </p>
                      <p>
                        第 {issue.location.episode_no} 集
                        {issue.location.time_ms !== null
                          ? ` · 采样 ${issue.location.time_ms} ms`
                          : ""}
                      </p>
                      <Link
                        className="underline"
                        href={`${episodePath(project.id, issue.location.episode_id, "list")}&node=script:${issue.location.script_version_id}`}
                      >
                        查看精确剧本
                      </Link>
                    </div>
                  ))}
                </section>
              ))}
            </>
          )}
          {value.question && (
            <section
              aria-label="叙事审查补证问题"
              className="flex flex-col gap-3"
            >
              <Notice>
                {value.question.request.question} 这只是补证回答，不是内容批准。
              </Notice>
              <Field>
                <FieldLabel htmlFor={`${id}-answer`}>
                  补充叙事审查证据
                </FieldLabel>
                {value.question.request.options.length ? (
                  <ToggleGroup
                    type="single"
                    variant="outline"
                    aria-label="补充叙事审查证据"
                    value={answer}
                    onValueChange={setAnswer}
                    disabled={!writable || busy || !!value.blocking_code}
                  >
                    {value.question.request.options.map((option) => (
                      <ToggleGroupItem key={option} value={option}>
                        {option}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                ) : (
                  <Textarea
                    id={`${id}-answer`}
                    value={answer}
                    maxLength={1000}
                    disabled={!writable || busy || !!value.blocking_code}
                    onChange={(e) => setAnswer(e.target.value)}
                  />
                )}
              </Field>
              <Button
                disabled={
                  !writable || busy || !!value.blocking_code || !answer.trim()
                }
                onClick={() =>
                  void mutate(
                    `/runs/${selected}/continuity/${value.question!.loop_id}/answers`,
                    {
                      question_id: value.question!.question_id,
                      input_sha256: value.question!.input_sha256,
                      decision: "ANSWER",
                      answer: answer.trim(),
                    },
                  )
                }
              >
                提交补证回答并继续审查
              </Button>
              <Button
                variant="outline"
                disabled={!writable || busy || !!value.blocking_code}
                onClick={() =>
                  void mutate(
                    `/runs/${selected}/continuity/${value.question!.loop_id}/answers`,
                    {
                      question_id: value.question!.question_id,
                      input_sha256: value.question!.input_sha256,
                      decision: "STOP",
                    },
                  )
                }
              >
                停止本次叙事审查
              </Button>
            </section>
          )}
          {value.report && (
            <NarrativeHuman
              key={value.human_review?.id ?? selected}
              value={value}
              allowed={owner && writable && !value.blocking_code}
              busy={busy}
              mutate={mutate}
            />
          )}
          <details>
            <summary className="cursor-pointer">
              精确输入、事实来源与覆盖范围
            </summary>
            <ul className="mt-2 space-y-1 text-sm">
              {value.work.evidence.map((evidence) => {
                const location = evidence.location;
                return (
                  <li key={evidence.id}>
                    {evidenceLabels[evidence.kind] ?? "审查证据"}
                    {location && (
                      <>
                        {" · "}
                        <Link
                          className="underline"
                          href={`${episodePath(project.id, location.episode_id, "list")}&node=script:${location.script_version_id}`}
                        >
                          第 {location.episode_no} 集剧本
                        </Link>
                        {location.time_ms !== null &&
                          ` · ${location.time_ms} ms`}
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          </details>
          {value.report && writable && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                setDraft(
                  JSON.stringify(
                    {
                      ...value.work.request,
                      previous_report_id: value.report!.report_id,
                    },
                    null,
                    2,
                  ),
                )
              }
            >
              引用本报告准备改稿重审
            </Button>
          )}
        </div>
      )}
    </section>
  );
}

function NarrativeHuman({
  value,
  allowed,
  busy,
  mutate,
}: {
  value: Review;
  allowed: boolean;
  busy: boolean;
  mutate: (path: string, body: unknown) => Promise<void>;
}) {
  const [comment, setComment] = useState("");
  const [viewed, setViewed] = useState(false);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const id = useId();
  const report = value.report!;
  const human = value.human_review;
  const issues =
    report.assessment?.checks.flatMap((check) => check.issues) ?? [];
  const warnings = issues.filter((issue) => issue.severity === "WARNING");
  const needs = [
    ...report.limitations.map((text, i) => ({ key: `limit-${i}`, text })),
    ...warnings.map((issue, i) => ({
      key: `warning-${i}`,
      text: issue.summary,
    })),
  ];
  const episodes = [
    ...new Set(
      value.work.evidence
        .filter((item) => item.kind === "SCENE")
        .map((item) => item.location!.episode_id),
    ),
  ];
  const canApprove =
    !issues.some((issue) => issue.severity === "BLOCKER") &&
    viewed &&
    needs.every((item) => resolutions[item.key]?.trim());
  async function decide(approve: boolean) {
    if (!human) return;
    await mutate(`/human-reviews/${human.id}/decisions`, {
      version_id: human.snapshot.version_id,
      request_sha256: human.request_sha256,
      decision: approve ? "APPROVE" : "REJECT",
      comment: comment.trim(),
      ...(approve
        ? {
            narrative_content: {
              report_id: report.report_id,
              input_sha256: report.input_sha256,
              continuous_viewing_confirmed: true,
              episode_ids: episodes,
              limitation_resolutions: report.limitations.map((_, i) =>
                resolutions[`limit-${i}`].trim(),
              ),
              warning_resolutions: warnings.map((_, i) =>
                resolutions[`warning-${i}`].trim(),
              ),
            },
          }
        : {}),
    });
  }
  return (
    <section
      aria-label="叙事人工内容批准"
      className="flex flex-col gap-3 border-t pt-4"
    >
      <h4 className="font-semibold">人工连续观片与内容确认</h4>
      {human?.blocking_code && (
        <Notice tone="error">审核受阻：{human.blocking_code}</Notice>
      )}
      {human?.status === "WAITING_REVIEW" && (
        <>
          {needs.map((item) => (
            <Field key={item.key}>
              <FieldLabel htmlFor={`${id}-${item.key}`}>{item.text}</FieldLabel>
              <Textarea
                id={`${id}-${item.key}`}
                maxLength={2000}
                value={resolutions[item.key] ?? ""}
                disabled={!allowed || busy}
                placeholder="记录本项人工核对与处理依据"
                onChange={(e) =>
                  setResolutions((old) => ({
                    ...old,
                    [item.key]: e.target.value,
                  }))
                }
              />
            </Field>
          ))}
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={viewed}
              disabled={!allowed || busy}
              onChange={(e) => setViewed(e.target.checked)}
            />
            我已连续观看并试听本次范围的全部剧集／场景，核对六类叙事问题。
          </label>
          <Field>
            <FieldLabel htmlFor={`${id}-comment`}>
              内容处理意见（必填）
            </FieldLabel>
            <Textarea
              id={`${id}-comment`}
              maxLength={2000}
              value={comment}
              disabled={!allowed || busy}
              onChange={(e) => setComment(e.target.value)}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={!allowed || busy || !comment.trim() || !canApprove}
              onClick={() => void decide(true)}
            >
              保存本次内容批准
            </Button>
            <Button
              variant="outline"
              disabled={!allowed || busy || !comment.trim()}
              onClick={() => void decide(false)}
            >
              拒绝并要求改稿重审
            </Button>
          </div>
          {issues.some((issue) => issue.severity === "BLOCKER") && (
            <Notice>阻塞问题必须改稿并重审，不能直接批准。</Notice>
          )}
        </>
      )}
      {human?.decision && (
        <p>
          已保存决定：{human.decision.decision_json.decision} ·{" "}
          {human.decision.decision_json.comment}
        </p>
      )}
      {human?.status === "DECIDED" && human.decision && (
        <Button
          variant="outline"
          disabled={!allowed || busy}
          onClick={() =>
            void mutate(`/human-reviews/${human.id}/resume`, {
              decision_id: human.decision!.id,
              request_sha256: human.request_sha256,
            })
          }
        >
          确认决定并结束本次审查
        </Button>
      )}
      {human?.resume && (
        <p>
          本次审查处理结果：{human.resume.result_json.status}
          。未改变素材选用或整剧交付状态。
        </p>
      )}
      {!allowed && (
        <p className="text-sm text-muted-foreground">
          当前仅可查阅；处理需要项目拥有者权限与有效输入。
        </p>
      )}
    </section>
  );
}
