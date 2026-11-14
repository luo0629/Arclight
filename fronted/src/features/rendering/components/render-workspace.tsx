"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { isResourceId } from "@/lib/routes";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { ApiError, message } from "@/lib/api/client";
import type { ContentVersion, Episode } from "@/types/api";
import { prepareLineAudioDecisions, renderingApi } from "../api";
import type {
  LineAudioDecision,
  RenderManifest,
  RenderJob,
  RenderProfile,
  Timeline,
} from "../types";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import DeliveryHistory from "./delivery-history";
import DefaultTimeline from "./default-timeline";
import TimelineEditor from "./timeline-editor";
import SubtitleFontPicker from "./subtitle-font-picker";
import { NumberField } from "./timeline-clip-fields";
import LineAudioDecisions, {
  type LineAudioChoice,
} from "./line-audio-decisions";
import RenderJobs from "./render-jobs";
import { date, duration, renderLabel, selectClass } from "./render-format";

const idOf = (row: { id: string }) => row.id;
const versionOf = (row: Timeline) => row.version_no;

export default function RenderWorkspace(props: {
  episode: Episode;
  owner: boolean;
  writable: boolean;
  boards: ContentVersion[];
  boardPagination: ReactNode;
  onOpenAudio?: () => void;
}) {
  const search = useSearchParams();
  const [denied, setDenied] = useState("");
  const [epoch, setEpoch] = useState(0);
  const unavailable = useCallback((cause: unknown) => {
    if (cause instanceof ApiError && [401, 403, 404].includes(cause.status))
      setDenied(message(cause));
  }, []);
  return (
    <section aria-label="渲染与成片" className="flex min-w-0 flex-col gap-5">
      {denied ? (
        <Notice tone="error">
          <h2>当前渲染记录不可访问</h2>
          <p>{denied}</p>
          <p>旧的预览和可操作记录已清空。请确认当前项目权限后重新读取。</p>
          <Button
            variant="outline"
            onClick={() => {
              setDenied("");
              setEpoch((value) => value + 1);
            }}
          >
            重新读取渲染记录
          </Button>
        </Notice>
      ) : (
        <RenderContent
          key={`${props.episode.id}:${props.owner}:${props.writable}:${epoch}:${search.get("render_job") ?? ""}:${search.get("render_manifest") ?? ""}:${search.get("timeline") ?? ""}`}
          {...props}
          unavailable={unavailable}
        />
      )}
    </section>
  );
}

function RenderContent({
  episode,
  owner,
  writable,
  boards,
  boardPagination,
  onOpenAudio,
  unavailable,
}: {
  episode: Episode;
  owner: boolean;
  writable: boolean;
  boards: ContentVersion[];
  boardPagination: ReactNode;
  onOpenAudio?: () => void;
  unavailable: (cause: unknown) => void;
}) {
  const fetchTimelines = useCallback(
    (cursor: number | undefined, signal: AbortSignal) =>
      renderingApi.listTimelines(episode.id, cursor, signal),
    [episode.id],
  );
  const timelines = useRenderList<Timeline, number>(
    episode.id,
    fetchTimelines,
    versionOf,
    unavailable,
  );
  const search = useSearchParams();
  const requestedJob = search.get("render_job");
  const requestedManifest = search.get("render_manifest");
  const requestedTimeline = search.get("timeline");
  const exactId =
    requestedJob && isResourceId(requestedJob)
      ? `job:${requestedJob}`
      : requestedManifest && isResourceId(requestedManifest)
        ? `manifest:${requestedManifest}`
        : requestedTimeline && isResourceId(requestedTimeline)
          ? `timeline:${requestedTimeline}`
          : "";
  const fetchExact = useCallback(
    async (signal: AbortSignal) => {
      if (exactId.startsWith("timeline:")) {
        const timeline = await renderingApi.getTimeline(
          exactId.slice(9),
          signal,
        );
        if (
          timeline.id !== exactId.slice(9) ||
          timeline.snapshot.episode_id !== episode.id
        )
          throw new Error("时间轴不属于当前剧集。");
        return { timeline, manifest: undefined, job: undefined };
      }
      let job: RenderJob | undefined;
      if (exactId.startsWith("job:")) {
        job = await renderingApi.getJob(exactId.slice(4), signal);
        if (job.id !== exactId.slice(4))
          throw new Error("渲染任务标识不一致。");
      }
      const manifestId = job?.manifest_id ?? exactId.slice(9);
      const manifest = await renderingApi.getManifest(manifestId, signal);
      if (
        manifest.id !== manifestId ||
        manifest.project_id !== episode.project_id ||
        manifest.snapshot.timeline.episode_id !== episode.id
      )
        throw new Error("渲染清单不属于当前剧集。");
      const timeline = await renderingApi.getTimeline(
        manifest.snapshot.timeline_id,
        signal,
      );
      if (
        timeline.id !== manifest.snapshot.timeline_id ||
        timeline.snapshot.episode_id !== episode.id
      )
        throw new Error("时间轴不属于当前剧集。");
      return { timeline, manifest, job };
    },
    [exactId, episode.id, episode.project_id],
  );
  const exact = useRenderDetail(
    `${episode.id}:${exactId}`,
    fetchExact,
    unavailable,
    !!exactId,
  );
  const timelineRows =
    exact.data &&
    !timelines.rows.some((row) => row.id === exact.data!.timeline.id)
      ? [exact.data.timeline, ...timelines.rows]
      : timelines.rows;
  const [selectedId, setSelectedId] = useState("");
  const selected = selectedId
    ? timelineRows.find((row) => row.id === selectedId)
    : exactId
      ? exact.data?.timeline
      : timelineRows[0];
  const [refresh, setRefresh] = useState(0);
  const rendered = useCallback(() => setRefresh((value) => value + 1), []);
  function created(timeline: Timeline) {
    timelines.prepend(timeline);
    setSelectedId(timeline.id);
  }
  return (
    <>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h2>渲染与成片</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            把已确认的镜头编排成片，核对精确输入、执行进度与交付报告。每次导出保留独立历史。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={timelines.loading}
          onClick={() => {
            void timelines.load();
            setRefresh((value) => value + 1);
          }}
        >
          刷新渲染记录
        </Button>
      </header>
      <section
        aria-label="时间轴与渲染提交"
        className="flex min-w-0 flex-col gap-4 rounded-xl border bg-card p-4 sm:p-5"
      >
        <header>
          <h3>选择精确时间轴</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            清单会固定本次素材、编码和软件版本；时间轴修改或重新选图后，需要新清单。
          </p>
        </header>
        {timelines.error && <Notice tone="error">{timelines.error}</Notice>}
        {exact.error && <Notice tone="error">{exact.error}</Notice>}
        {exact.loading && <p role="status">正在读取指定渲染任务的精确输入…</p>}
        {timelines.loading && timelines.rows.length === 0 && (
          <p role="status">正在读取时间轴版本…</p>
        )}
        {timelineRows.length > 0 && (
          <label className="flex min-w-0 max-w-xl flex-col gap-2 text-sm font-medium">
            时间轴版本
            <select
              aria-label="时间轴版本"
              className={selectClass}
              value={selected?.id ?? ""}
              disabled={timelines.loading}
              onChange={(event) => setSelectedId(event.target.value)}
            >
              {!selected && (
                <option value="" disabled>
                  请重新选择精确时间轴版本
                </option>
              )}
              {timelineRows.map((timeline) => (
                <option key={timeline.id} value={timeline.id}>
                  v{timeline.version_no} ·{" "}
                  {duration(timeline.snapshot.duration_ms)}
                  {timeline.stale ? " · 素材已失效" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        {selectedId &&
          !selected &&
          !timelines.loading &&
          timelines.rows.length > 0 && (
            <Notice>
              所选时间轴不在当前页，请加载更多或重新选择精确版本。
            </Notice>
          )}
        {timelines.hasMore && (
          <Button
            className="self-start"
            variant="outline"
            disabled={timelines.loading}
            onClick={() => void timelines.load(true)}
          >
            加载更多时间轴
          </Button>
        )}
        {!timelines.loading &&
          !timelines.error &&
          timelines.rows.length === 0 && (
            <Notice>
              还没有时间轴。请从已批准分镜的当前选用镜头创建最小时间轴。
            </Notice>
          )}
        {writable && (
          <DefaultTimeline
            episode={episode}
            boards={boards}
            boardPagination={boardPagination}
            unavailable={unavailable}
            onCreated={created}
          />
        )}
        {selected && (
          <TimelineManifests
            key={selected.id}
            timeline={selected}
            projectId={episode.project_id}
            owner={owner}
            writable={writable}
            unavailable={unavailable}
            refresh={refresh}
            onRendered={rendered}
            onSaved={created}
            onRefresh={() => void timelines.load()}
            onOpenAudio={onOpenAudio}
            initialManifest={
              exact.data?.timeline.id === selected.id
                ? exact.data.manifest
                : undefined
            }
            initialJob={
              exact.data?.timeline.id === selected.id
                ? exact.data.job
                : undefined
            }
          />
        )}
      </section>
      <DeliveryHistory
        episode={episode}
        owner={owner}
        writable={writable}
        unavailable={unavailable}
        refresh={refresh}
      />
    </>
  );
}

function TimelineManifests({
  timeline,
  projectId,
  owner,
  writable,
  unavailable,
  refresh,
  onRendered,
  initialManifest,
  initialJob,
  onSaved,
  onRefresh,
  onOpenAudio,
}: {
  timeline: Timeline;
  projectId: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  refresh: number;
  onRendered: () => void;
  initialManifest?: RenderManifest;
  initialJob?: RenderJob;
  onSaved: (timeline: Timeline) => void;
  onRefresh: () => void;
  onOpenAudio?: () => void;
}) {
  const fetchManifests = useCallback(
    (cursor: string | undefined, signal: AbortSignal) =>
      renderingApi.listManifests(timeline.id, cursor, signal),
    [timeline.id],
  );
  const manifests = useRenderList<RenderManifest, string>(
    timeline.id,
    fetchManifests,
    idOf,
    unavailable,
  );
  useEffect(() => {
    if (refresh) void manifests.load();
  }, [refresh, manifests.load]);
  const [profile, setProfile] = useState<RenderProfile>("PREVIEW");
  const [draftDirty, setDraftDirty] = useState(false);
  const [fontId, setFontId] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [fontSize, setFontSize] = useState(48);
  const [marginBottom, setMarginBottom] = useState(64);
  const [bold, setBold] = useState(false);
  const needsFont = timeline.snapshot.tracks.some(
    (track) =>
      track.kind === "SUBTITLE" &&
      track.enabled !== false &&
      track.clips.length > 0,
  );
  const settingsValid =
    !needsFont ||
    (!!fontId &&
      Number.isInteger(fontSize) &&
      fontSize >= 6 &&
      fontSize <= 144 &&
      Number.isInteger(marginBottom) &&
      marginBottom >= 0 &&
      marginBottom <= 500);
  const manifestRows =
    initialManifest &&
    !manifests.rows.some((row) => row.id === initialManifest.id)
      ? [initialManifest, ...manifests.rows]
      : manifests.rows;
  const [selectedId, setSelectedId] = useState("");
  const selected = selectedId
    ? manifestRows.find((row) => row.id === selectedId)
    : (initialManifest ?? manifestRows[0]);
  const fetchAudio = useCallback(
    (signal: AbortSignal) =>
      prepareLineAudioDecisions(timeline.snapshot.episode_id, timeline, signal),
    [timeline],
  );
  const audio = useRenderDetail(timeline.id, fetchAudio, unavailable, writable);
  const [audioChoices, setAudioChoices] = useState<
    Record<string, LineAudioChoice>
  >({});
  const lineAudioDecisions: LineAudioDecision[] = audio.data
    ? audio.data.lines.flatMap((line) => {
        const choice = audioChoices[line.line_id];
        if (
          !choice ||
          !choice.mode ||
          !choice.reason.trim() ||
          choice.reason.trim().length > 500 ||
          (choice.mode === "SUBTITLE_ONLY" && !line.subtitle_available)
        )
          return [];
        return [
          {
            script_version_id: audio.data!.script_version_id,
            line_id: line.line_id,
            mode: choice.mode,
            reason: choice.reason.trim(),
          },
        ];
      })
    : [];
  const audioComplete =
    !!audio.data &&
    !audio.loading &&
    lineAudioDecisions.length === audio.data.lines.length;
  const manifestBody = {
    profile,
    line_audio_decisions: lineAudioDecisions,
    ...(needsFont
      ? {
          font_file_id: fontId,
          subtitle_style: {
            font_size: fontSize,
            margin_bottom: marginBottom,
            bold,
          },
        }
      : {}),
  };
  const creationScope = `manifest:${timeline.id}:${JSON.stringify(manifestBody)}`;
  const mutation = useRenderMutation(creationScope, writable, unavailable);
  async function createManifest() {
    if (!audioComplete || timeline.stale || draftDirty || !settingsValid)
      return;
    const pending = pendingCreationKey(creationScope);
    await mutation.mutate(
      async (signal) => {
        const next = await renderingApi.createManifest(
          timeline.id,
          { creation_key: pending.key, ...manifestBody },
          signal,
        );
        pending.clear();
        return next;
      },
      (next) => {
        manifests.prepend(next);
        setSelectedId(next.id);
      },
      "不可变渲染清单已创建。请核对精确输入后提交渲染。",
    );
  }
  const visualCount = timeline.snapshot.tracks
    .filter((track) => track.kind === "VIDEO")
    .reduce((count, track) => count + track.clips.length, 0);
  return (
    <div className="flex min-w-0 flex-col gap-4 border-t pt-4">
      <TimelineEditor
        timeline={timeline}
        projectId={projectId}
        owner={owner}
        writable={writable}
        unavailable={unavailable}
        onSaved={onSaved}
        onRefresh={onRefresh}
        onDirty={setDraftDirty}
        onOpenAudio={onOpenAudio}
        refresh={refresh}
      />
      {draftDirty && (
        <Notice>
          时间轴有未保存草稿。请先保存新版本，再创建本次修改的渲染清单；下方历史清单仍对应原保存版本。
        </Notice>
      )}
      <div className="flex flex-wrap items-baseline gap-3">
        <h4>时间轴 v{timeline.version_no}</h4>
        <p className="text-sm text-muted-foreground">
          {visualCount} 个镜头 · {duration(timeline.snapshot.duration_ms)}
        </p>
      </div>
      {timeline.stale && (
        <Notice tone="error">
          此时间轴引用的素材已失效。请核对并创建新时间轴；旧清单和历史成片仍可查看。
        </Notice>
      )}
      <details className="min-w-0">
        <summary>时间轴精确版本与镜头来源</summary>
        <dl className="mt-3 grid gap-2 text-xs [&_dd]:break-all [&_dt]:text-muted-foreground">
          <dt>时间轴 ID</dt>
          <dd>{timeline.id}</dd>
          <dt>时间轴 SHA256</dt>
          <dd>{timeline.content_sha256}</dd>
          <dt>分镜版本</dt>
          <dd>{timeline.snapshot.storyboard_id}</dd>
          <dt>镜头素材版本</dt>
          <dd>
            <ul className="flex flex-col gap-2">
              {timeline.snapshot.tracks
                .filter((track) => track.kind === "VIDEO")
                .flatMap((track) => track.clips)
                .map((clip) => (
                  <li key={clip.id}>
                    {clip.shot_id}
                    <br />
                    {clip.source_asset_version_id}
                    <br />
                    {clip.source_sha256}
                  </li>
                ))}
            </ul>
          </dd>
        </dl>
      </details>
      {writable && (
        <>
          {audio.loading && (
            <p role="status">正在核对精确剧本与台词声音来源…</p>
          )}
          {audio.error && (
            <Notice tone="error">
              {audio.error}
              <Button
                variant="outline"
                disabled={audio.loading || mutation.busy}
                onClick={() => void audio.load()}
              >
                重新核对台词声音来源
              </Button>
            </Notice>
          )}
          {audio.data && (
            <LineAudioDecisions
              preparation={audio.data}
              choices={audioChoices}
              disabled={mutation.busy || audio.loading}
              onChange={(lineId, choice) =>
                setAudioChoices((previous) => ({
                  ...previous,
                  [lineId]: choice,
                }))
              }
            />
          )}
          {needsFont && (
            <details
              open={settingsOpen}
              onToggle={(event) => setSettingsOpen(event.currentTarget.open)}
            >
              <summary>字幕渲染设置与授权字体</summary>
              {settingsOpen && (
                <div className="mt-3 flex min-w-0 max-w-xl flex-col gap-3">
                  <SubtitleFontPicker
                    projectId={projectId}
                    value={fontId}
                    onChange={setFontId}
                    disabled={mutation.busy}
                    unavailable={unavailable}
                  />
                  <div className="grid grid-cols-2 gap-3">
                    <NumberField
                      label="字幕字号"
                      min={6}
                      max={144}
                      value={fontSize}
                      onChange={setFontSize}
                      disabled={mutation.busy}
                    />
                    <NumberField
                      label="字幕底部边距"
                      max={500}
                      value={marginBottom}
                      onChange={setMarginBottom}
                      disabled={mutation.busy}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={bold}
                      disabled={mutation.busy}
                      onChange={(e) => setBold(e.target.checked)}
                    />
                    字幕加粗
                  </label>
                  <p className="text-xs text-muted-foreground">
                    字体与样式随本次清单冻结；输出尺寸和帧率沿用项目规格，正式版使用正式编码配置。
                  </p>
                </div>
              )}
            </details>
          )}
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-0 flex-1 flex-col gap-2 text-sm font-medium sm:max-w-xs">
              输出用途
              <select
                aria-label="输出用途"
                className={selectClass}
                value={profile}
                disabled={mutation.busy}
                onChange={(event) =>
                  setProfile(event.target.value as RenderProfile)
                }
              >
                <option value="PREVIEW">预览版</option>
                <option value="FORMAL">正式版</option>
              </select>
            </label>
            <Button
              disabled={
                mutation.busy ||
                timeline.stale ||
                !audioComplete ||
                draftDirty ||
                !settingsValid
              }
              onClick={() => void createManifest()}
            >
              {profile === "PREVIEW" ? "创建预览清单" : "创建正式清单"}
            </Button>
          </div>
        </>
      )}
      {writable && (
        <p className="text-xs text-muted-foreground">
          网络结果不明时可原样重试，沿用同一创建标识；创建成功后再次操作会另建清单。
        </p>
      )}
      {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
      {manifests.error && <Notice tone="error">{manifests.error}</Notice>}
      {manifests.loading && manifestRows.length === 0 && (
        <p role="status">正在读取渲染清单…</p>
      )}
      {manifestRows.length > 0 && (
        <label className="flex min-w-0 flex-col gap-2 text-sm font-medium">
          渲染清单
          <select
            aria-label="渲染清单"
            className={selectClass}
            value={selected?.id ?? ""}
            disabled={manifests.loading || mutation.busy}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            {!selected && (
              <option value="" disabled>
                请重新选择精确渲染清单
              </option>
            )}
            {manifestRows.map((manifest) => (
              <option key={manifest.id} value={manifest.id}>
                {renderLabel(manifest.snapshot.output.name)} ·{" "}
                {date(manifest.snapshot.created_at)} · {manifest.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      )}
      {selectedId &&
        !selected &&
        !manifests.loading &&
        manifestRows.length > 0 && (
          <Notice>所选清单不在当前页，请加载更多或重新选择精确清单。</Notice>
        )}
      {manifests.hasMore && (
        <Button
          className="self-start"
          variant="outline"
          disabled={manifests.loading}
          onClick={() => void manifests.load(true)}
        >
          加载更多渲染清单
        </Button>
      )}
      {!manifests.loading && !manifests.error && manifestRows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          本时间轴还没有渲染清单。
        </p>
      )}
      {selected && (
        <>
          <section
            aria-label="当前渲染清单"
            className="flex min-w-0 flex-col gap-3 rounded-xl border bg-muted/30 p-4"
          >
            <h4>{renderLabel(selected.snapshot.output.name)}清单</h4>
            <p className="text-sm">
              {selected.snapshot.output.width} ×{" "}
              {selected.snapshot.output.height}，{selected.snapshot.output.fps}{" "}
              fps，{selected.snapshot.output.video_codec} /{" "}
              {selected.snapshot.output.audio_codec}
            </p>
            <details className="min-w-0">
              <summary>清单和全部精确输入</summary>
              <dl className="mt-3 grid gap-2 text-xs [&_dd]:break-all [&_dt]:text-muted-foreground">
                <dt>清单 ID</dt>
                <dd>{selected.id}</dd>
                <dt>清单 SHA256</dt>
                <dd>{selected.content_sha256}</dd>
                <dt>时间轴 ID</dt>
                <dd>{selected.snapshot.timeline_id}</dd>
                <dt>时间轴 SHA256</dt>
                <dd>{selected.snapshot.timeline_sha256}</dd>
                <dt>渲染器版本</dt>
                <dd>{selected.snapshot.software.renderer_version}</dd>
              </dl>
              <ul className="mt-3 flex min-w-0 flex-col gap-2 text-xs">
                {selected.snapshot.sources.map((source) => (
                  <li key={source.asset_version_id} className="break-all">
                    {source.asset_type} / {source.asset_version_id}
                    <br />
                    {source.sha256}
                  </li>
                ))}
              </ul>
              <pre>
                {JSON.stringify(
                  {
                    font: selected.snapshot.font,
                    output: selected.snapshot.output,
                    software: selected.snapshot.software,
                    line_audio_decisions:
                      selected.snapshot.line_audio_decisions,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          </section>
          <RenderJobs
            key={selected.id}
            manifest={selected}
            owner={owner}
            writable={writable}
            unavailable={unavailable}
            refresh={refresh}
            onRendered={onRendered}
            initialJob={
              initialJob?.manifest_id === selected.id ? initialJob : undefined
            }
          />
        </>
      )}
    </div>
  );
}
