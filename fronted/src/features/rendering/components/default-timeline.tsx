"use client";

import { useCallback, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import type { ContentVersion, Episode } from "@/types/api";
import { prepareTimelineSelection, renderingApi } from "../api";
import type { ShotSelection, Timeline } from "../types";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import { selectClass } from "./render-format";

export default function DefaultTimeline({
  episode,
  boards,
  boardPagination,
  unavailable,
  onCreated,
}: {
  episode: Episode;
  boards: ContentVersion[];
  boardPagination: ReactNode;
  unavailable: (cause: unknown) => void;
  onCreated: (timeline: Timeline) => void;
}) {
  const [boardId, setBoardId] = useState("");
  const approved = boards.filter(
    (board) => board.status === "APPROVED" && board.episode_id === episode.id,
  );
  const board = approved.find((row) => row.id === boardId);
  return (
    <details className="min-w-0 border-t pt-4">
      <summary>从已批准分镜创建默认时间轴</summary>
      <div className="mt-4 flex min-w-0 flex-col gap-4">
        <p className="max-w-2xl text-sm text-muted-foreground">
          每个镜头固定 3
          秒，按分镜顺序使用当前已选用且审查通过的图片。请先核对完整清单；本次操作创建独立时间轴版本。
        </p>
        {approved.length === 0 && (
          <Notice>
            当前页没有已批准分镜。请先批准分镜，或加载更多分镜版本。
          </Notice>
        )}
        <label className="flex min-w-0 max-w-xl flex-col gap-2 text-sm font-medium">
          已批准分镜版本
          <select
            aria-label="已批准分镜版本"
            className={selectClass}
            value={board?.id ?? ""}
            onChange={(event) => setBoardId(event.target.value)}
          >
            <option value="">请选择精确分镜版本</option>
            {approved.map((row) => (
              <option key={row.id} value={row.id}>
                分镜 v{row.version_no} · {row.id}
              </option>
            ))}
          </select>
        </label>
        {boardPagination}
        {board && (
          <PreparedTimeline
            key={`${episode.id}:${board.id}`}
            episode={episode}
            board={board}
            unavailable={unavailable}
            onCreated={onCreated}
          />
        )}
      </div>
    </details>
  );
}

function PreparedTimeline({
  episode,
  board,
  unavailable,
  onCreated,
}: {
  episode: Episode;
  board: ContentVersion;
  unavailable: (cause: unknown) => void;
  onCreated: (timeline: Timeline) => void;
}) {
  const fetchPreparation = useCallback(
    (signal: AbortSignal) =>
      prepareTimelineSelection(
        episode.project_id,
        episode.id,
        board.id,
        signal,
      ),
    [episode.project_id, episode.id, board.id],
  );
  const prepared = useRenderDetail(
    board.id,
    fetchPreparation,
    unavailable,
    false,
  );
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState(false);
  const selected: ShotSelection[] = (prepared.data?.shots ?? []).flatMap(
    (shot) =>
      choices[shot.shot_id] &&
      shot.options.some(
        (option) => option.asset_version_id === choices[shot.shot_id],
      )
        ? [{ shot_id: shot.shot_id, asset_version_id: choices[shot.shot_id] }]
        : [],
  );
  const scope = `timeline:${episode.id}:${board.id}:3000:${JSON.stringify(selected)}`;
  const mutation = useRenderMutation(scope, true, unavailable);
  async function prepare() {
    setConfirmed(false);
    setChoices({});
    const next = await prepared.load();
    if (next)
      setChoices(
        Object.fromEntries(
          next.selected_shots.map((shot) => [
            shot.shot_id,
            shot.asset_version_id,
          ]),
        ),
      );
  }
  const complete =
    !!prepared.data &&
    prepared.data.blocking_issues.length === 0 &&
    selected.length === prepared.data.shots.length &&
    selected.length > 0;
  async function create() {
    if (!complete || !confirmed || prepared.loading) return;
    const pending = pendingCreationKey(scope);
    await mutation.mutate(
      async (signal) => {
        const timeline = await renderingApi.createTimeline(
          episode.id,
          {
            creation_key: pending.key,
            storyboard_id: board.id,
            default_duration_ms: 3000,
            selected_shots: selected,
          },
          signal,
        );
        pending.clear();
        return timeline;
      },
      (timeline) => {
        setConfirmed(false);
        onCreated(timeline);
      },
      "默认时间轴已创建。请核对该时间轴并创建渲染清单。",
    );
  }
  return (
    <section
      aria-label="默认时间轴镜头核对"
      className="flex min-w-0 flex-col gap-4"
    >
      <p className="break-all text-xs text-muted-foreground">
        分镜版本：{board.id}
      </p>
      <Button
        className="self-start"
        variant="outline"
        disabled={prepared.loading || mutation.busy}
        onClick={() => void prepare()}
      >
        {prepared.loading ? "正在核对当前镜头…" : "核对当前选用镜头"}
      </Button>
      {prepared.error && <Notice tone="error">{prepared.error}</Notice>}
      {prepared.data && (
        <>
          <p className="text-sm">
            共 {prepared.data.shots.length} 个镜头，每镜头 3
            秒。以下版本来自现有选用结果。
          </p>
          {prepared.data.blocking_issues.length > 0 && (
            <Notice tone="error">
              <ul className="list-inside list-disc">
                {prepared.data.blocking_issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </Notice>
          )}
          <ol className="flex min-w-0 flex-col gap-3">
            {prepared.data.shots.map((shot) => (
              <li
                key={shot.shot_id}
                className="flex min-w-0 flex-col gap-2 rounded-lg border p-3 text-sm"
              >
                <h5>镜头 {shot.shot_no}</h5>
                <p>{shot.description}</p>
                <p className="break-all text-xs text-muted-foreground">
                  镜头 ID：{shot.shot_id}
                </p>
                {shot.options.length === 0 ? (
                  <p className="text-destructive">
                    缺少有效的已选用图片，无法创建时间轴。
                  </p>
                ) : shot.options.length === 1 ? (
                  <p className="break-all text-xs">
                    已选用图片 v{shot.options[0].version_no}：
                    {shot.options[0].asset_version_id}
                  </p>
                ) : (
                  <>
                    <p className="text-sm text-muted-foreground">
                      当前有多个已选用版本，请明确本次时间轴使用哪一个。
                    </p>
                    <label className="flex min-w-0 flex-col gap-2">
                      镜头 {shot.shot_no} 的已选用版本
                      <select
                        aria-label={`镜头 ${shot.shot_no} 的已选用版本`}
                        className={selectClass}
                        value={choices[shot.shot_id] ?? ""}
                        disabled={mutation.busy}
                        onChange={(event) => {
                          setChoices((previous) => ({
                            ...previous,
                            [shot.shot_id]: event.target.value,
                          }));
                          setConfirmed(false);
                        }}
                      >
                        <option value="">请选择精确素材版本</option>
                        {shot.options.map((option) => (
                          <option
                            key={option.asset_version_id}
                            value={option.asset_version_id}
                          >
                            {option.description} v{option.version_no} ·{" "}
                            {option.asset_version_id}
                          </option>
                        ))}
                      </select>
                    </label>
                  </>
                )}
              </li>
            ))}
          </ol>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={!complete || mutation.busy || prepared.loading}
              onChange={(event) => setConfirmed(event.target.checked)}
              className="mt-1 size-4 shrink-0 accent-primary"
            />
            我已核对全部镜头及精确素材版本
          </label>
          <Button
            className="self-start"
            disabled={
              !complete || !confirmed || mutation.busy || prepared.loading
            }
            onClick={() => void create()}
          >
            创建默认时间轴
          </Button>
        </>
      )}
      {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
    </section>
  );
}
