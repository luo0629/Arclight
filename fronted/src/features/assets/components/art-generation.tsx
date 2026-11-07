"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice, StatusBadge } from "@/components/ui-kit";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { useAuth } from "@/features/auth/auth-context";
import { api, json, message } from "@/lib/api/client";
import type { Episode, ContentVersion, Run } from "@/types/api";
import ArtProgress, {
  type ArtDelivery,
} from "@/features/tasks/components/art-progress";

export default function ArtGeneration({
  projectId,
  writable,
  onView,
}: {
  projectId: string;
  writable: boolean;
  onView: (delivery: ArtDelivery) => void;
}) {
  const episodes = usePagedList<Episode>(`/projects/${projectId}/episodes`);
  const { user } = useAuth();
  const storageKey = `arclight:art-run:${user.id}:${projectId}`;
  const [episodeId, setEpisodeId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  const operation = useRef(false);
  useEffect(() => {
    live.current = true;
    void episodes.reload().catch(() => {});
    try {
      const id = sessionStorage.getItem(storageKey);
      if (id)
        void api<Run>(`/runs/${id}`)
          .then((row) => {
            if (live.current) {
              setRun(row);
              setEpisodeId(row.episode_id);
            }
          })
          .catch(() => {});
    } catch {
      /* Run history remains available from the server. */
    }
    return () => {
      live.current = false;
    };
  }, [episodes.reload, storageKey]);
  function select(row: Run) {
    setRun(row);
    try {
      sessionStorage.setItem(storageKey, row.id);
    } catch {
      /* Optional selection. */
    }
  }
  async function reload() {
    if (!run) return;
    const row = await api<Run>(`/runs/${run.id}`);
    if (live.current)
      setRun((current) => (current?.id === row.id ? row : current));
  }
  useEffect(() => {
    if (!run || !["QUEUED", "RUNNING", "WAITING_REVIEW"].includes(run.status))
      return;
    let cancelled = false;
    const timer = setInterval(() => {
      void api<Run>(`/runs/${run.id}`)
        .then((row) => {
          if (!cancelled) setRun(row);
        })
        .catch((e) => {
          if (!cancelled) setError(message(e));
        });
    }, 1500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [run?.id, run?.status]);
  async function act(action: () => Promise<unknown>) {
    if (operation.current) return false;
    operation.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (error) {
      if (live.current) setError(message(error));
      return false;
    } finally {
      operation.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <section
      aria-label="生成美术提案"
      className="flex min-w-0 flex-col gap-4 border-y py-4"
    >
      <h3 className="font-semibold">生成美术提案</h3>
      <p className="text-sm text-muted-foreground">
        选择已批准的精确剧本，描述视觉目标。调查、澄清与提案会持续保存在同一任务中。
      </p>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <Field>
        <FieldLabel htmlFor="art-episode">美术任务剧集</FieldLabel>
        <ChoiceSelect
          id="art-episode"
          value={episodeId}
          onValueChange={setEpisodeId}
          placeholder="选择剧集"
          options={episodes.rows.map((episode) => ({
            value: episode.id,
            label: `第 ${episode.episode_no} 集 · ${episode.title}`,
          }))}
        />
      </Field>
      <ListPagination
        {...episodes}
        count={episodes.rows.length}
        label="美术任务剧集"
      />
      {episodeId && (
        <EpisodeGeneration
          key={episodeId}
          episodeId={episodeId}
          storageKey={storageKey}
          writable={writable}
          busy={busy}
          act={act}
          onRun={select}
        />
      )}
      {run && (
        <div
          className="flex min-w-0 flex-col gap-3"
          aria-label="当前美术生成任务"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4>当前美术生成任务</h4>
            <StatusBadge status={run.status} />
          </div>
          <p className="text-sm text-muted-foreground">
            {new Date(run.created_at).toLocaleString("zh-CN")}
          </p>
          <ArtProgress
            key={run.id}
            run={run}
            writable={writable}
            busy={busy}
            act={act}
            reload={reload}
            onView={onView}
          />
        </div>
      )}
    </section>
  );
}

function EpisodeGeneration({
  episodeId,
  storageKey,
  writable,
  busy,
  act,
  onRun,
}: {
  episodeId: string;
  storageKey: string;
  writable: boolean;
  busy: boolean;
  act: (action: () => Promise<unknown>) => Promise<boolean>;
  onRun: (run: Run) => void;
}) {
  const scripts = usePagedList<ContentVersion>(
    `/episodes/${episodeId}/scripts`,
  );
  const runs = usePagedList<Run>(`/episodes/${episodeId}/runs`);
  const [scriptId, setScriptId] = useState("");
  const [goal, setGoal] = useState("");
  const [constraints, setConstraints] = useState("");
  const [minimum, setMinimum] = useState(2);
  const [notice, setNotice] = useState("");
  const pending = useRef<{ key: string; fingerprint: string } | null>(null);
  useEffect(() => {
    void scripts.reload().catch(() => {});
    void runs.reload().catch(() => {});
    try {
      const value = JSON.parse(
        sessionStorage.getItem(storageKey + ":request") ?? "null",
      );
      if (
        value?.episodeId === episodeId &&
        typeof value.body?.task?.visual_goal === "string"
      ) {
        setScriptId(value.body.script_version_id);
        setGoal(value.body.task.visual_goal);
        setConstraints(value.body.task.constraints.join("\n"));
        setMinimum(value.body.task.minimum_reference_images);
        pending.current = {
          key: value.key,
          fingerprint: JSON.stringify(value.body),
        };
        setNotice("已恢复尚未取得运行回执的请求，可用原请求重试。");
      }
    } catch {
      /* A corrupt optional draft grants no write permission. */
    }
  }, [scripts.reload, runs.reload, storageKey, episodeId]);
  const approved = scripts.rows.filter(
    (script) => script.status === "APPROVED",
  );
  async function start() {
    await act(async () => {
      const body = {
        script_version_id: scriptId,
        task: {
          visual_goal: goal,
          constraints: [
            ...new Set(
              constraints
                .split("\n")
                .map((s) => s.trim())
                .filter(Boolean),
            ),
          ],
          minimum_reference_images: minimum,
        },
      };
      const fingerprint = JSON.stringify(body);
      if (pending.current?.fingerprint !== fingerprint)
        pending.current = { key: crypto.randomUUID(), fingerprint };
      const key = pending.current.key;
      // Save the logical request before sending; a lost response reuses its key.
      try {
        sessionStorage.setItem(
          storageKey + ":request",
          JSON.stringify({ episodeId, key, body }),
        );
      } catch {
        /* Keep the same key in memory; saved runs remain in server history. */
      }
      const result = await api<{ run_id: string }>(
        `/episodes/${episodeId}/art-runs`,
        { ...json("POST", body), headers: { "Idempotency-Key": key } },
      );
      const run = await api<Run>(`/runs/${result.run_id}`);
      onRun(run);
      try {
        sessionStorage.removeItem(storageKey + ":request");
      } catch {
        /* Optional local request draft. */
      }
      pending.current = null;
      setNotice("");
      await runs.reload().catch(() => {
        setNotice("任务已创建，历史列表暂未更新，可刷新列表。");
      });
    });
  }
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {notice && <Notice role="status">{notice}</Notice>}
      {writable && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (scriptId && goal.trim() && !busy) void start();
          }}
        >
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="art-script">美术输入剧本</FieldLabel>
              <ChoiceSelect
                id="art-script"
                value={scriptId}
                onValueChange={setScriptId}
                disabled={busy}
                placeholder="选择已批准版本"
                options={approved.map((script) => ({
                  value: script.id,
                  label: `v${script.version_no} · ${String(script.content_json.title ?? "剧本")}`,
                }))}
              />
            </Field>
            <ListPagination
              {...scripts}
              count={scripts.rows.length}
              label="美术输入剧本版本"
            />
            {scripts.loaded && !approved.length && (
              <Notice>
                当前页没有已批准剧本。请先批准剧本，或继续加载版本。
              </Notice>
            )}
            <Field>
              <FieldLabel htmlFor="art-goal">生成视觉目标</FieldLabel>
              <Textarea
                id="art-goal"
                required
                maxLength={4000}
                value={goal}
                disabled={busy}
                onChange={(e) => setGoal(e.target.value)}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="art-constraints">
                视觉约束（每行一项）
              </FieldLabel>
              <Textarea
                id="art-constraints"
                maxLength={4000}
                value={constraints}
                disabled={busy}
                onChange={(e) => setConstraints(e.target.value)}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="art-minimum">最少参考图片</FieldLabel>
              <Input
                id="art-minimum"
                type="number"
                min={0}
                max={8}
                required
                value={minimum}
                disabled={busy}
                onChange={(e) => setMinimum(Number(e.target.value))}
              />
              <p className="text-sm text-muted-foreground">
                范围 0–8。文字视觉方案可设为 0；必要参考不足时会请求补充。
              </p>
            </Field>
            <Button type="submit" disabled={busy || !scriptId || !goal.trim()}>
              开始美术生成
            </Button>
          </FieldGroup>
        </form>
      )}
      <h4 className="font-semibold">已保存的美术任务</h4>
      {runs.rows
        .filter((run) => run.workflow_type === "art_proposal")
        .map((run) => (
          <Button
            key={run.id}
            variant="outline"
            disabled={busy}
            onClick={() => onRun(run)}
            className="h-auto justify-between gap-3 whitespace-normal text-left"
          >
            {new Date(run.created_at).toLocaleString("zh-CN")}
            <StatusBadge status={run.status} />
          </Button>
        ))}
      <ListPagination {...runs} count={runs.rows.length} label="美术任务历史" />
    </div>
  );
}
