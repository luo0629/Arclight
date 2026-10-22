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
import usePagedList from "./usePagedList";
import ListPagination from "./ListPagination";
import { api, json, type Run } from "./api";

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
  async function action(run: Run, command: string, decision?: string) {
    const payload =
      command === "resume"
        ? {
            ...run.pending_review,
            decision,
            comment,
            expected_revision: run.revision,
          }
        : undefined;
    await act(async () => {
      await api(`/runs/${run.id}/${command}`, json("POST", payload));
      await reload();
      setComment("");
    });
  }
  return (
    <PagePanel
      eyebrow="每一步都有迹可循"
      title="创作任务与审核"
      description="生成在后台 Worker 中执行。等待审核时，只有项目 Owner 可以批准或驳回对应的具体版本。"
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
                <strong>剧本与分镜创作</strong>
                <StatusBadge status={run.status} />
              </div>
              <p className="muted text-sm text-muted-foreground">
                {new Date(run.created_at).toLocaleString("zh-CN")} · 修订{" "}
                {run.revision}
              </p>
              <code className="task-id text-muted-foreground">{run.id}</code>
              {run.error_code && (
                <Notice tone="error" className="error">
                  错误代码：{run.error_code}。请检查服务配置和 Worker 日志。
                </Notice>
              )}
              {run.pending_review && (
                <p>
                  等待审核：
                  {run.pending_review.resource_type === "script"
                    ? "剧本"
                    : "分镜"}
                  版本 <code>{run.pending_review.version_id}</code>
                </p>
              )}
              {owner && writable && run.pending_review && (
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
                    <Button
                      variant="default"
                      size="lg"
                      className=""
                      disabled={busy}
                      onClick={() => void action(run, "resume", "APPROVE")}
                    >
                      批准并继续
                    </Button>
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
                {writable && run.status === "FAILED" && (
                  <Button
                    variant="outline"
                    size="lg"
                    disabled={busy}
                    onClick={() => void action(run, "retry")}
                  >
                    重试任务
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
