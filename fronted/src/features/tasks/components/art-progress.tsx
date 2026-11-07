"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import { runErrorMessage } from "@/lib/run-errors";
import type { Run } from "@/types/api";
import AgentAnswer, { type HumanOperation } from "./agent-answer";
import ArtConceptRounds, { type ConceptRound } from "./art-concept-rounds";

export type ArtDelivery = {
  proposal_id: string;
  version_id: string;
  review_request_id: string;
};
type Operation = HumanOperation & {
  status: string;
  stop_code: string | null;
  model_turns: number;
  result: {
    draft: {
      assessments: {
        option_id: string;
        strengths: string;
        tradeoffs: string;
      }[];
    };
  } | null;
};
type Progress = {
  project_id: string;
  concept_plan?: {
    plan: {
      maximum_rounds: number;
      maximum_candidates: number;
      maximum_cost: string;
    };
  } | null;
  concept_rounds?: ConceptRound[];
  operations: Operation[];
  delivery: ArtDelivery | null;
  can_cancel: boolean;
};

export default function ArtProgress({
  run,
  writable,
  busy,
  act,
  reload,
  onView,
}: {
  run: Run;
  writable: boolean;
  busy: boolean;
  act: (action: () => Promise<unknown>) => Promise<boolean>;
  reload: () => Promise<void>;
  onView?: (delivery: ArtDelivery) => void;
}) {
  const [data, setData] = useState<Progress | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const result = await api<Progress>(`/runs/${run.id}/art`);
        if (live) {
          setData(result);
          setError("");
        }
      } catch (error) {
        if (live) setError(message(error));
      }
      if (live && ["QUEUED", "RUNNING"].includes(run.status))
        timer = setTimeout(() => void load(), 1500);
    }
    void load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [run.id, run.status, run.revision, refresh]);
  async function command(suffix: string, body?: object) {
    return act(async () => {
      await api(`/runs/${run.id}/${suffix}`, json("POST", body));
      setRefresh((value) => value + 1);
      await reload();
    });
  }
  return (
    <section
      aria-label="美术调查与方案进度"
      className="flex min-w-0 flex-col gap-3"
    >
      <h4 className="font-semibold">美术调查与方案进度</h4>
      <p className="text-sm text-muted-foreground">
        调查与方案会保存到本次任务。正式变更须由项目拥有者审核。
      </p>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {!data && !error && <p role="status">正在读取美术进度…</p>}
      {data?.operations.length === 0 && (
        <p role="status">排队后开始调查视觉规范与参考素材。</p>
      )}
      {data?.operations.map((operation) => (
        <div key={operation.loop_id} className="flex min-w-0 flex-col gap-3">
          <p>
            {operation.status === "COMPLETED"
              ? "方案已保存"
              : operation.status === "WAITING_HUMAN"
                ? run.status === "CANCELLED"
                  ? "创作已停止"
                  : "等待补充信息"
                : operation.status === "WAITING_EXTERNAL"
                  ? run.status === "CANCELLED"
                    ? "创作已停止"
                    : "等待概念图生成与媒体检查"
                  : operation.status === "STOPPED"
                    ? "决策已停止"
                    : "正在调查与比较"}{" "}
            · 已完成 {operation.model_turns} 轮模型交流
          </p>
          {operation.stop_code && (
            <Notice>{runErrorMessage(operation.stop_code)}</Notice>
          )}
          {operation.result && (
            <details>
              <summary>查看方案比较依据</summary>
              <ol className="flex list-decimal flex-col gap-2 pl-5">
                {operation.result.draft.assessments.map((assessment) => (
                  <li key={assessment.option_id}>
                    <p>优势：{assessment.strengths}</p>
                    <p>取舍：{assessment.tradeoffs}</p>
                  </li>
                ))}
              </ol>
            </details>
          )}
          {operation.human_request && (
            <AgentAnswer
              key={operation.question_id}
              operation={operation}
              run={run}
              disabled={!writable || busy || run.status !== "RUNNING"}
              canStop={data.can_cancel}
              stop={() => command("cancel")}
              submit={(answer) =>
                command(`art/${operation.loop_id}/answers`, {
                  question_id: operation.question_id,
                  input_sha256: operation.input_sha256,
                  decision: "ANSWER",
                  answer,
                })
              }
            />
          )}
        </div>
      ))}
      {data?.concept_plan && (
        <>
          <p className="text-sm text-muted-foreground">
            冻结计划：最多 {data.concept_plan.plan.maximum_rounds} 轮 /{" "}
            {data.concept_plan.plan.maximum_candidates} 张 · 总预算 USD{" "}
            {data.concept_plan.plan.maximum_cost}
          </p>
          <ArtConceptRounds
            projectId={data.project_id}
            rounds={data.concept_rounds ?? []}
          />
        </>
      )}
      {data?.delivery &&
        (onView ? (
          <Button variant="outline" onClick={() => onView(data.delivery!)}>
            查看生成提案
          </Button>
        ) : (
          <p className="text-sm text-muted-foreground">
            可在项目视觉资产的“美术提案与审核”查看精确方案并审核。
          </p>
        ))}
      {run.error_code && (
        <Notice tone="error" role="alert">
          {runErrorMessage(run.error_code)}
        </Notice>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setRefresh((value) => value + 1);
            void reload().catch((error) => setError(message(error)));
          }}
        >
          刷新美术进度
        </Button>
        {writable &&
          (["QUEUED", "WAITING_REVIEW"].includes(run.status) ||
            (run.status === "RUNNING" && data?.can_cancel)) && (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void command("cancel")}
            >
              取消美术生成
            </Button>
          )}
        {writable &&
          run.status === "FAILED" &&
          !data?.operations.some((op) => op.status === "STOPPED") && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void command("retry")}
            >
              恢复美术任务
            </Button>
          )}
      </div>
    </section>
  );
}
