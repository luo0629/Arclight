"use client";

import { PagePanel } from "@/components/ui-kit";
import {
  EmptyState,
  StatusBadge,
  Notice,
  FormLabel,
} from "@/components/ui-kit";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { useEffect, useState } from "react";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";
import { api, json } from "@/lib/api/client";
import { runErrorMessage } from "@/lib/run-errors";
import type { Run } from "@/types/api";
import CharacterMatching from "@/features/story/components/character-matching";
import ImpactButton from "@/features/dependencies/components/impact-button";
import ScreenwriterProgress from "@/features/tasks/components/screenwriter-progress";
import ArtProgress from "./art-progress";

type Act = (action: () => Promise<unknown>) => Promise<boolean>;
export default function Tasks({
  runs,
  owner,
  writable,
  busy,
  act,
  reload,
  pagination,
}: {
  runs: Run[];
  pagination: React.ReactNode;
  owner: boolean;
  writable: boolean;
  busy: boolean;
  act: Act;
  reload: () => Promise<void>;
}) {
  const [eventRun, setEventRun] = useState("");
  const [comment, setComment] = useState("");
  async function action(
    run: Run,
    command: string,
    decision?: string,
    impact_digest?: string,
  ) {
    const payload =
      command === "resume"
        ? {
            ...run.pending_review,
            decision,
            comment,
            expected_revision: run.revision,
            impact_digest,
          }
        : undefined;
    return act(async () => {
      await api(`/runs/${run.id}/${command}`, {
        ...json("POST", payload),
        ...(command === "restart"
          ? { headers: { "Idempotency-Key": crypto.randomUUID() } }
          : {}),
      });
      await reload();
      setComment("");
    });
  }
  return (
    <PagePanel
      eyebrow="每一步都有迹可循"
      title="创作任务与审核"
      description="创作会在后台继续，不必一直等待。生成完成后，由项目拥有者确认每一版剧本与分镜。"
    >
      {runs.length === 0 ? (
        <EmptyState className="empty compact py-8">
          <h3>还没有创作任务</h3>
          <p>回到创作画布，在故事起点写下灵感并生成剧本。</p>
        </EmptyState>
      ) : (
        <div className="task-list grid min-w-0 gap-4 lg:grid-cols-2">
          {runs.map((run) => (
            <article
              className="task flex min-w-0 flex-col gap-3 rounded-xl border bg-background/60 p-4"
              key={run.id}
            >
              <div className="task-heading flex flex-wrap items-center justify-between gap-2">
                <strong>
                  {run.workflow_type === "art_proposal"
                    ? "美术提案生成"
                    : run.workflow_type === "script_rewrite"
                      ? "剧本局部改写"
                      : run.workflow_type === "story_outline"
                        ? "分集大纲"
                        : run.workflow_type === "story_analysis"
                          ? "故事提炼"
                          : run.workflow_type === "storyboard"
                            ? "分镜创作"
                            : "剧本与分镜创作"}
                </strong>
                <StatusBadge status={run.status} />
              </div>
              <p className="muted text-sm text-muted-foreground">
                {new Date(run.created_at).toLocaleString("zh-CN")} · 修订{" "}
                {run.revision}
              </p>
              <code className="task-id text-muted-foreground">{run.id}</code>
              {[
                "screenwriter/1.0",
                "screenwriter-outline/1.0",
                "screenwriter/2.0",
                "screenwriter-rewrite/1.0",
                "production/1.0",
                "production/2.0",
                "production/3.0",
                "production/4.0",
                "production/5.0",
                "production/6.0",
              ].includes(run.workflow_version ?? "") && (
                <ScreenwriterProgress
                  run={run}
                  writable={writable}
                  busy={busy}
                  act={act}
                  reload={reload}
                />
              )}
              {[
                "director/1.0",
                "director/2.0",
                "director/3.0",
                "production/1.0",
                "production/2.0",
                "production/3.0",
                "production/4.0",
                "production/5.0",
                "production/6.0",
              ].includes(run.workflow_version ?? "") && (
                <ScreenwriterProgress
                  agent="director"
                  run={run}
                  writable={writable}
                  busy={busy}
                  act={act}
                  reload={reload}
                />
              )}
              {run.workflow_type === "art_proposal" && (
                <ArtProgress
                  run={run}
                  writable={writable}
                  busy={busy}
                  act={act}
                  reload={reload}
                />
              )}
              {run.error_code && run.workflow_type !== "art_proposal" && (
                <Notice tone="error" className="error" role="alert">
                  {runErrorMessage(run.error_code)}
                </Notice>
              )}
              {run.pending_review &&
                run.pending_review.resource_type !== "art_proposal" && (
                  <p>
                    等待审核：
                    {run.pending_review.resource_type === "script"
                      ? "剧本"
                      : "分镜"}
                    版本 <code>{run.pending_review.version_id}</code>
                  </p>
                )}
              {owner &&
                writable &&
                run.pending_review &&
                run.pending_review.resource_type !== "art_proposal" && (
                  <>
                    <FormLabel>
                      审核意见
                      <Textarea
                        value={comment}
                        onChange={(event) => setComment(event.target.value)}
                        maxLength={2000}
                        placeholder="记录这次创作决定的原因"
                      />
                    </FormLabel>
                    <div className="button-row flex flex-wrap items-center gap-2">
                      <ImpactButton
                        variant="default"
                        size="lg"
                        className=""
                        disabled={busy}
                        endpoint={`/episodes/${run.episode_id}/${run.pending_review.resource_type}s/${run.pending_review.version_id}/dependency-impact`}
                        onConfirm={(digest) =>
                          action(run, "resume", "APPROVE", digest)
                        }
                      >
                        批准并继续
                      </ImpactButton>
                      <Button
                        variant="outline"
                        size="lg"
                        disabled={busy}
                        onClick={() => void action(run, "resume", "REJECT")}
                      >
                        驳回该版本
                      </Button>
                    </div>
                  </>
                )}
              {run.pending_review?.resource_type === "script" && (
                <CharacterMatching
                  episodeId={run.episode_id}
                  sourceType="scripts"
                  versionId={run.pending_review.version_id}
                  writable={writable}
                  busy={busy}
                />
              )}
              <div className="button-row flex flex-wrap items-center gap-2">
                {writable &&
                  ["QUEUED", "WAITING_REVIEW"].includes(run.status) && (
                    <Button
                      variant="outline"
                      size="lg"
                      disabled={busy}
                      onClick={() => {
                        if (window.confirm("取消此任务？已有版本会保留。"))
                          void action(run, "cancel");
                      }}
                    >
                      取消任务
                    </Button>
                  )}
                {writable &&
                  run.status === "FAILED" &&
                  run.workflow_type !== "art_proposal" && (
                    <Button
                      variant="outline"
                      size="lg"
                      disabled={busy}
                      onClick={() => void action(run, "retry")}
                    >
                      重试任务
                    </Button>
                  )}
                {writable && run.status === "FAILED" && (
                  <Button
                    variant="outline"
                    size="lg"
                    disabled={busy}
                    onClick={() => void action(run, "restart")}
                  >
                    使用当前配置重新发起
                  </Button>
                )}
                <Button
                  variant="outline"
                  size="lg"
                  disabled={busy}
                  onClick={() => setEventRun(run.id)}
                >
                  查看执行记录
                </Button>
              </div>
              {writable && run.status === "FAILED" && (
                <p className="text-xs text-muted-foreground">
                  重试沿用原配置；重新发起会保留原业务输入，使用当前账号配置创建新任务。原调用结果未知时须先核验。
                </p>
              )}
            </article>
          ))}
        </div>
      )}
      {pagination}
      {eventRun && (
        <EventHistory
          key={eventRun}
          runId={eventRun}
          close={() => setEventRun("")}
        />
      )}
    </PagePanel>
  );
}

type RunEvent = {
  id: string;
  event_type: string;
  created_at: string;
  payload_json: unknown;
};
function EventHistory({ runId, close }: { runId: string; close: () => void }) {
  const events = usePagedList<RunEvent>("/runs/" + runId + "/events");
  const { reload } = events;
  useEffect(() => {
    void reload().catch(() => {});
  }, [reload]);
  return (
    <section
      className="event-panel flex min-w-0 flex-col gap-4 rounded-xl border bg-background p-4 [&_ol]:flex [&_ol]:flex-col [&_ol]:gap-3 [&_li]:flex [&_li]:min-w-0 [&_li]:flex-col [&_li]:gap-2 [&_li]:rounded-lg [&_li]:bg-card [&_li]:p-3 [&_time]:text-xs [&_time]:text-muted-foreground"
      aria-label="执行记录"
    >
      <div className="section-heading flex flex-wrap items-center justify-between gap-2">
        <h3>执行记录</h3>
        <Button variant="outline" size="lg" onClick={close}>
          收起记录
        </Button>
      </div>
      <p className="muted text-sm text-muted-foreground">
        任务 {runId} · 按时间从早到晚
      </p>
      {events.loaded && !events.rows.length && (
        <p className="hint text-xs leading-relaxed text-muted-foreground">
          暂无执行记录
        </p>
      )}
      <ol>
        {events.rows.map((event) => (
          <li key={event.id}>
            <time>
              {new Date(event.created_at).toLocaleTimeString("zh-CN")}
            </time>
            <strong>{event.event_type}</strong>
            <details>
              <summary>事件数据</summary>
              <pre>{JSON.stringify(event.payload_json, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ol>
      <ListPagination {...events} label="事件" count={events.rows.length} />
    </section>
  );
}
