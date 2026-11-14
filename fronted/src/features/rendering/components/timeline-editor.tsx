"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { ApiError } from "@/lib/api/client";
import { useSessionDraft } from "@/providers/draft-provider";
import { renderingApi } from "../api";
import type { Timeline, TimelineClip } from "../types";
import {
  changeClip,
  clipLanes,
  moveShot,
  timelineIssues,
  trackNames,
  validDraft,
} from "../timeline-editing";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import TimelineClipFields from "./timeline-clip-fields";
import TimelinePreview from "./timeline-preview";
import TimelineSubtitles from "./timeline-subtitles";
import TimelineSources from "./timeline-sources";
import { duration } from "./render-format";

export default function TimelineEditor({
  timeline,
  projectId,
  owner,
  writable,
  unavailable,
  onSaved,
  onRefresh,
  onDirty,
  onOpenAudio,
  refresh,
}: {
  timeline: Timeline;
  projectId: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  onSaved: (timeline: Timeline) => void;
  onRefresh: () => void;
  onDirty: (value: boolean) => void;
  onOpenAudio?: () => void;
  refresh: number;
}) {
  const [open, setOpen] = useState(false);
  const [subtitleOpen, setSubtitleOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const draft = useSessionDraft({
    id: `timeline:${timeline.id}:${timeline.content_sha256}`,
    episodeId: timeline.snapshot.episode_id,
    label: `时间轴 v${timeline.version_no}`,
    initial: timeline.snapshot,
    validate: (value): value is typeof timeline.snapshot =>
      validDraft(value, timeline.snapshot),
  });
  const snapshot = draft.value;
  useEffect(() => {
    onDirty(draft.dirty);
  }, [draft.dirty, onDirty]);
  const [selection, select] = useState(
    snapshot.tracks.find((t) => t.kind === "VIDEO")?.clips[0]?.id ?? "",
  );
  const [seek, setSeek] = useState({ ms: 0, request: 0 });
  const [playhead, setPlayhead] = useState(0);
  const track = snapshot.tracks.find((t) =>
    t.clips.some((c) => c.id === selection),
  );
  const clip = track?.clips.find((c) => c.id === selection);
  const issues = timelineIssues(snapshot);
  const layouts = new Map(
    snapshot.tracks.map((t) => [t.id, clipLanes(t.clips)]),
  );
  const scope = `timeline-save:${timeline.id}:${JSON.stringify(snapshot)}`;
  const mutation = useRenderMutation(
    scope,
    writable && !timeline.stale,
    unavailable,
  );
  const [conflict, setConflict] = useState(false);
  const disabled = !writable || timeline.stale || mutation.busy;
  function update(patch: Partial<TimelineClip>) {
    if (!disabled && clip) draft.update(changeClip(snapshot, clip.id, patch));
  }
  async function save() {
    if (!draft.dirty || issues.errors.length || draft.invalid) return;
    const pending = pendingCreationKey(scope);
    await mutation.mutate(
      async (signal) => {
        try {
          const saved = await renderingApi.saveTimeline(
            timeline.id,
            {
              creation_key: pending.key,
              expected_version_no: timeline.version_no,
              snapshot,
            },
            signal,
          );
          if (
            saved.snapshot.episode_id !== snapshot.episode_id ||
            saved.based_on_timeline_id !== timeline.id ||
            saved.version_no !== timeline.version_no + 1
          )
            throw new Error(
              "保存结果与当前时间轴版本不一致。草稿已保留，请刷新后核对。",
            );
          pending.clear();
          return saved;
        } catch (cause) {
          if (
            !signal.aborted &&
            cause instanceof ApiError &&
            cause.status === 409 &&
            cause.code === "VERSION_CONFLICT"
          ) {
            setConflict(true);
            throw new Error(
              "时间轴已有新版本，当前草稿已保留。请刷新版本列表并核对最新版本，避免覆盖他人修改。",
            );
          }
          throw cause;
        }
      },
      (saved) => {
        draft.clear();
        onSaved(saved);
      },
      "新时间轴版本已保存。请创建新的预览清单。",
    );
  }
  return (
    <details
      className="min-w-0 border-t pt-4"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>时间轴编辑器{draft.dirty ? "（有本机草稿）" : ""}</summary>
      {open && (
        <div
          className="mt-4 flex min-w-0 flex-col gap-4"
          role="region"
          aria-label="时间轴编辑器"
        >
          <header className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h4>时间轴 v{timeline.version_no}</h4>
              <p className="text-sm text-muted-foreground">
                {duration(snapshot.duration_ms)}，五类轨道，选择片段查看参数。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {writable && (
                <>
                  <Button
                    disabled={
                      disabled ||
                      !draft.dirty ||
                      draft.invalid ||
                      issues.errors.length > 0
                    }
                    onClick={() => void save()}
                  >
                    保存时间轴新版本
                  </Button>
                  <Button
                    variant="outline"
                    disabled={mutation.busy || !draft.dirty}
                    onClick={() => {
                      draft.clear();
                      setConflict(false);
                    }}
                  >
                    放弃本机时间轴草稿
                  </Button>
                </>
              )}
              {onOpenAudio && (
                <Button variant="outline" onClick={onOpenAudio}>
                  前往音频与字幕
                </Button>
              )}
            </div>
          </header>
          {!writable && <Notice>当前为只读，可查看轨道和预览。</Notice>}
          {draft.recovered && writable && (
            <Notice>已恢复此精确时间轴版本的本机草稿，尚未保存到项目。</Notice>
          )}
          {draft.invalid && (
            <Notice tone="error">
              本机时间轴草稿损坏或来源不匹配。请放弃此草稿后重新编辑。
            </Notice>
          )}
          {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
          {conflict && (
            <Button
              className="self-start"
              variant="outline"
              onClick={onRefresh}
            >
              刷新时间轴版本列表
            </Button>
          )}
          {issues.errors.length > 0 && (
            <Notice tone="error">
              <ul>
                {issues.errors.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </Notice>
          )}
          {issues.warnings.length > 0 && (
            <Notice>
              <ul>
                {issues.warnings.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </Notice>
          )}
          <div className="grid min-w-0 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(17rem,22rem)]">
            <div className="flex min-w-0 flex-col gap-4">
              <div
                className="overflow-x-auto rounded-lg border"
                tabIndex={0}
                aria-label="五轨时间线，可横向滚动"
              >
                <div className="min-w-[34rem]">
                  <div className="ml-28 flex justify-between border-b px-3 py-2 text-xs text-muted-foreground">
                    <span>0 秒</span>
                    <span>{duration(snapshot.duration_ms / 2)}</span>
                    <span>{duration(snapshot.duration_ms)}</span>
                  </div>
                  {[...snapshot.tracks]
                    .sort((a, b) => a.ordinal - b.ordinal)
                    .map((row) => (
                      <div
                        key={row.id}
                        className="flex min-h-20 border-b last:border-0"
                      >
                        <div className="flex w-28 shrink-0 flex-col justify-center gap-1 border-r bg-muted/30 px-3 py-2">
                          {row.kind === "VIDEO" ? (
                            <span className="text-sm font-medium">画面</span>
                          ) : (
                            <label className="flex items-center gap-2 text-sm font-medium">
                              <input
                                type="checkbox"
                                aria-label={`启用${trackNames[row.kind]}轨道`}
                                checked={row.enabled !== false}
                                disabled={disabled}
                                onChange={(event) =>
                                  draft.update({
                                    ...snapshot,
                                    tracks: snapshot.tracks.map((t) =>
                                      t.id === row.id
                                        ? {
                                            ...t,
                                            enabled: event.target.checked,
                                          }
                                        : t,
                                    ),
                                  })
                                }
                              />
                              {trackNames[row.kind]}
                            </label>
                          )}
                          <span className="text-xs text-muted-foreground">
                            {row.clips.length} 个片段
                            {row.enabled === false ? " · 已关闭" : ""}
                          </span>
                        </div>
                        <div
                          className="relative min-w-0 flex-1"
                          style={{
                            minHeight: `${layouts.get(row.id)!.height}px`,
                          }}
                        >
                          <div
                            className="pointer-events-none absolute inset-y-0 z-10 w-px bg-primary"
                            style={{
                              left: `${Math.min(100, (playhead / Math.max(1, snapshot.duration_ms)) * 100)}%`,
                            }}
                          />
                          {[...row.clips]
                            .sort((a, b) => a.ordinal - b.ordinal)
                            .map((item, index) => (
                              <button
                                key={item.id}
                                type="button"
                                aria-label={`${trackNames[row.kind]}片段 ${index + 1}，${item.start_ms} 至 ${item.start_ms + item.duration_ms} 毫秒`}
                                aria-pressed={selection === item.id}
                                className={`absolute h-8 min-w-8 truncate rounded border px-2 text-left text-xs focus-visible:z-20 ${selection === item.id ? "border-primary bg-accent text-accent-foreground" : "border-input bg-secondary text-secondary-foreground"} ${row.enabled === false ? "opacity-50" : ""}`}
                                style={{
                                  left: `${(item.start_ms / Math.max(1, snapshot.duration_ms)) * 100}%`,
                                  width: `${Math.max(2, (item.duration_ms / Math.max(1, snapshot.duration_ms)) * 100)}%`,
                                  top: `${6 + layouts.get(row.id)!.positions.get(item.id)! * 38}px`,
                                }}
                                onClick={() => {
                                  select(item.id);
                                  setSeek((s) => ({
                                    ms: item.start_ms,
                                    request: s.request + 1,
                                  }));
                                }}
                              >
                                {index + 1} · {duration(item.duration_ms)}
                              </button>
                            ))}
                          {row.clips.length === 0 && (
                            <p className="px-3 py-5 text-xs text-muted-foreground">
                              暂无片段
                            </p>
                          )}
                        </div>
                      </div>
                    ))}
                </div>
              </div>
              <TimelinePreview
                timeline={timeline}
                projectId={projectId}
                seekMs={seek}
                onTimeChange={setPlayhead}
                dirty={draft.dirty}
                unavailable={unavailable}
                refresh={refresh}
              />
            </div>
            {clip && track && (
              <aside
                aria-label="选中片段参数"
                className="flex min-w-0 flex-col gap-3 border-t pt-4 xl:border-t-0 xl:border-l xl:pt-0 xl:pl-5"
              >
                <h4>
                  {trackNames[track.kind]}片段 {clip.ordinal + 1}
                </h4>
                {track.kind === "VIDEO" && (
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      disabled={disabled || clip.ordinal === 0}
                      onClick={() =>
                        draft.update(moveShot(snapshot, clip.id, -1))
                      }
                    >
                      向前移动镜头
                    </Button>
                    <Button
                      variant="outline"
                      disabled={
                        disabled || clip.ordinal === track.clips.length - 1
                      }
                      onClick={() =>
                        draft.update(moveShot(snapshot, clip.id, 1))
                      }
                    >
                      向后移动镜头
                    </Button>
                  </div>
                )}
                {track.kind !== "VIDEO" &&
                  track.kind !== "SUBTITLE" &&
                  writable && (
                    <Button
                      variant="outline"
                      disabled={disabled}
                      onClick={() => {
                        draft.update({
                          ...snapshot,
                          tracks: snapshot.tracks.map((t) =>
                            t.id === track.id
                              ? {
                                  ...t,
                                  clips: t.clips
                                    .filter((c) => c.id !== clip.id)
                                    .map((c, ordinal) => ({ ...c, ordinal })),
                                }
                              : t,
                          ),
                        });
                        select("");
                      }}
                    >
                      从草稿移除此声音片段
                    </Button>
                  )}
                <TimelineClipFields
                  clip={clip}
                  kind={track.kind}
                  disabled={disabled}
                  onChange={update}
                  onOpenAudio={
                    track.kind === "SUBTITLE"
                      ? () => setSubtitleOpen(true)
                      : onOpenAudio
                  }
                />
              </aside>
            )}
          </div>
          {writable && (
            <details
              open={sourcesOpen}
              onToggle={(event) => setSourcesOpen(event.currentTarget.open)}
            >
              <summary>加入配音、音乐与音效</summary>
              {sourcesOpen && (
                <fieldset className="mt-4 min-w-0" disabled={disabled}>
                  <TimelineSources
                    timeline={timeline}
                    projectId={projectId}
                    unavailable={unavailable}
                    onAdd={(kind, item) => {
                      if (disabled) return;
                      draft.update({
                        ...snapshot,
                        tracks: snapshot.tracks.map((t) =>
                          t.kind === kind && t.clips.length < 256
                            ? {
                                ...t,
                                clips: [
                                  ...t.clips,
                                  { ...item, ordinal: t.clips.length },
                                ],
                              }
                            : t,
                        ),
                      });
                      select(item.id);
                    }}
                  />
                </fieldset>
              )}
            </details>
          )}
          <details
            open={subtitleOpen}
            onToggle={(event) => setSubtitleOpen(event.currentTarget.open)}
          >
            <summary>管理字幕文字、时点与版本</summary>
            {subtitleOpen && (
              <TimelineSubtitles
                timeline={timeline}
                projectId={projectId}
                owner={owner}
                writable={writable && !timeline.stale && !mutation.busy}
                unavailable={unavailable}
                onApply={(clips) =>
                  draft.update({
                    ...snapshot,
                    tracks: snapshot.tracks.map((t) =>
                      t.kind === "SUBTITLE" ? { ...t, clips } : t,
                    ),
                  })
                }
              />
            )}
          </details>
        </div>
      )}
    </details>
  );
}
