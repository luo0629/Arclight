import { Notice, StatusBadge } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import { runErrorMessage } from "@/lib/run-errors";

export type ConceptRound = {
  job_id: string;
  round_no: number;
  status: string;
  rationale: string;
  actual_cost: string | null;
  reserved_cost: string;
  outcome_uncertain: boolean;
  candidates: {
    ordinal: number;
    asset_id: string | null;
    asset_version_id: string | null;
    status: string | null;
    error_code: string | null;
  }[];
};

export default function ArtConceptRounds({
  projectId,
  rounds,
}: {
  projectId: string;
  rounds: ConceptRound[];
}) {
  return (
    <section aria-label="概念图轮次" className="flex min-w-0 flex-col gap-4">
      <h4 className="font-semibold">概念图轮次</h4>
      {!rounds.length && (
        <p className="text-sm text-muted-foreground">
          计划已冻结，等待申请首轮候选。
        </p>
      )}
      {rounds.map((round) => (
        <article
          key={round.job_id}
          aria-label={`第 ${round.round_no} 轮概念图`}
          className="flex min-w-0 flex-col gap-2"
        >
          <div className="flex flex-wrap items-center gap-2">
            <h5>第 {round.round_no} 轮概念图</h5>
            <StatusBadge status={round.status} />
          </div>
          <p className="whitespace-pre-wrap break-words text-sm">
            依据：{round.rationale}
          </p>
          <p className="text-sm text-muted-foreground">
            实际费用：
            {round.actual_cost === null ? "未知" : `USD ${round.actual_cost}`} ·
            预留：USD {round.reserved_cost}
          </p>
          {round.outcome_uncertain && (
            <Notice>
              原生成结果待核验，保留预留费用。请先处理原任务的对账结果。
            </Notice>
          )}
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            {round.candidates.map((candidate) => (
              <div
                key={candidate.ordinal}
                className="flex min-w-0 flex-col gap-2"
              >
                <p className="text-sm">候选 {candidate.ordinal + 1}</p>
                {candidate.asset_id && candidate.asset_version_id && (
                  <>
                    <VersionImage
                      path={`/projects/${projectId}/assets/${candidate.asset_id}/versions/${candidate.asset_version_id}/image`}
                    />
                    <p className="break-all text-xs text-muted-foreground">
                      精确版本：{candidate.asset_version_id}
                    </p>
                  </>
                )}
                {candidate.error_code && (
                  <Notice tone="error">
                    {runErrorMessage(candidate.error_code)}
                  </Notice>
                )}
              </div>
            ))}
          </div>
        </article>
      ))}
    </section>
  );
}
