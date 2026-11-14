"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { api } from "@/lib/api/client";
import { audioApi } from "@/features/audio/api";
import type {
  AudioHistory,
  AudioLine,
  AudioSnapshot,
} from "@/features/audio/types";
import useAudioPage from "@/features/audio/use-audio-page";
import { prepareLineAudioDecisions } from "../api";
import type { ClipAudio, Timeline, TimelineClip } from "../types";
import useRenderDetail from "../hooks/use-render-detail";
import { selectClass } from "./render-format";

const identity = (row: { id: string }) => row.id;
const lineIdentity = (row: AudioLine) => row.line_id;
const historyIdentity = (row: AudioHistory) => row.binding.id;
const usable = (row: { status: string; stale?: boolean }) =>
  ["APPROVED", "LOCKED"].includes(row.status) && !row.stale;
function newClip(
  version: string,
  checksum: string,
  duration: number,
): TimelineClip {
  return {
    id: crypto.randomUUID(),
    ordinal: 0,
    start_ms: 0,
    duration_ms: duration,
    source_start_ms: 0,
    source_asset_version_id: version,
    source_sha256: checksum,
    shot_id: null,
    script_version_id: null,
    line_id: null,
    audio_version_id: null,
    review_id: null,
    review_storyboard_id: null,
    review_shot_id: null,
  };
}

export default function TimelineSources({
  timeline,
  projectId,
  unavailable,
  onAdd,
}: {
  timeline: Timeline;
  projectId: string;
  unavailable: (cause: unknown) => void;
  onAdd: (kind: "VOICE" | "MUSIC" | "SFX", clip: TimelineClip) => void;
}) {
  return (
    <section
      aria-label="加入精确声音素材"
      className="grid min-w-0 gap-5 lg:grid-cols-2"
    >
      <VoiceSources
        timeline={timeline}
        unavailable={unavailable}
        onAdd={onAdd}
      />
      <MusicSources
        timeline={timeline}
        projectId={projectId}
        unavailable={unavailable}
        onAdd={onAdd}
      />
    </section>
  );
}

function VoiceSources({
  timeline,
  unavailable,
  onAdd,
}: Omit<Parameters<typeof TimelineSources>[0], "projectId">) {
  const episode = timeline.snapshot.episode_id;
  const fetchExact = useCallback(
    (signal: AbortSignal) =>
      prepareLineAudioDecisions(episode, timeline, signal),
    [episode, timeline],
  );
  const exact = useRenderDetail(timeline.id, fetchExact, unavailable);
  const fetchPage = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.snapshots(episode, offset, signal),
    [episode],
  );
  const snapshots = useAudioPage<AudioSnapshot>(
    episode,
    fetchPage,
    identity,
    unavailable,
  );
  const options = snapshots.rows.filter(
    (row) =>
      row.episode_id === episode &&
      row.script_version_id === exact.data?.script_version_id,
  );
  const [selectedId, select] = useState("");
  const selected = options.find((row) => row.id === selectedId) ?? options[0];
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <h4>加入已批准配音</h4>
      <p className="text-xs text-muted-foreground">
        只使用本时间轴精确剧本的句级音频，加入后可调整时点与源入点。
      </p>
      <select
        aria-label="装配配音快照"
        className={selectClass}
        value={selected?.id ?? ""}
        onChange={(e) => select(e.target.value)}
      >
        <option value="" disabled>
          请选择匹配剧本的配音快照
        </option>
        {options.map((row) => (
          <option key={row.id} value={row.id}>
            {row.line_count} 句 · {row.id.slice(0, 8)}
          </option>
        ))}
      </select>
      {snapshots.hasMore && (
        <Button
          variant="outline"
          disabled={snapshots.loading}
          onClick={() => void snapshots.load(true)}
        >
          加载更多配音快照
        </Button>
      )}
      {(snapshots.error || exact.error) && (
        <Notice tone="error">{snapshots.error || exact.error}</Notice>
      )}
      {selected && (
        <VoiceLines
          key={selected.id}
          snapshot={selected}
          timeline={timeline}
          unavailable={unavailable}
          onAdd={onAdd}
        />
      )}
    </div>
  );
}

function VoiceLines({
  snapshot,
  timeline,
  unavailable,
  onAdd,
}: Omit<Parameters<typeof VoiceSources>[0], "episode"> & {
  snapshot: AudioSnapshot;
}) {
  const fetchLines = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.lines(snapshot.id, offset, signal),
    [snapshot.id],
  );
  const lines = useAudioPage(
    snapshot.id,
    fetchLines,
    lineIdentity,
    unavailable,
  );
  const [lineId, setLine] = useState("");
  const selected =
    lines.rows.find((row) => row.line_id === lineId) ?? lines.rows[0];
  const fetchHistory = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      selected
        ? audioApi.history(snapshot.id, selected.line_id, offset, signal)
        : Promise.resolve([]),
    [snapshot.id, selected],
  );
  const history = useAudioPage(
    `${snapshot.id}:${selected?.line_id ?? ""}`,
    fetchHistory,
    historyIdentity,
    unavailable,
  );
  const options = history.rows.filter(
    (row) =>
      usable(row.version) &&
      row.binding.snapshot_id === snapshot.id &&
      row.binding.line_id === selected?.line_id,
  );
  const [versionId, setVersion] = useState("");
  const version =
    options.find((row) => row.version.id === versionId) ?? options[0];
  return (
    <>
      <select
        aria-label="装配配音台词"
        className={selectClass}
        value={selected?.line_id ?? ""}
        onChange={(e) => setLine(e.target.value)}
      >
        {!selected && <option value="">暂无台词</option>}
        {lines.rows.map((row) => (
          <option key={row.line_id} value={row.line_id}>
            {row.speaker} · {row.text.slice(0, 32)}
          </option>
        ))}
      </select>
      {lines.hasMore && (
        <Button
          variant="outline"
          disabled={lines.loading}
          onClick={() => void lines.load(true)}
        >
          加载更多装配台词
        </Button>
      )}
      <select
        aria-label="装配配音版本"
        className={selectClass}
        value={version?.version.id ?? ""}
        onChange={(e) => setVersion(e.target.value)}
      >
        {!version && <option value="">本句暂无已批准音频</option>}
        {options.map((row) => (
          <option key={row.version.id} value={row.version.id}>
            配音 v{row.version.version_no} · {row.binding.duration_ms} 毫秒
          </option>
        ))}
      </select>
      {history.hasMore && (
        <Button
          variant="outline"
          disabled={history.loading}
          onClick={() => void history.load(true)}
        >
          加载更多装配配音版本
        </Button>
      )}
      {(lines.error || history.error) && (
        <Notice tone="error">{lines.error || history.error}</Notice>
      )}
      <Button
        variant="outline"
        disabled={!version || !selected || history.loading}
        onClick={() => {
          if (!version || !selected) return;
          onAdd("VOICE", {
            ...newClip(
              version.version.id,
              version.version.checksum,
              Math.min(
                version.binding.duration_ms,
                timeline.snapshot.duration_ms,
              ),
            ),
            script_version_id: snapshot.script_version_id,
            line_id: selected.line_id,
          });
        }}
      >
        将此句配音加入草稿
      </Button>
    </>
  );
}

type MusicAsset = {
  id: string;
  project_id: string;
  name: string;
  asset_type: string;
  current_version_id: string | null;
  status: string;
};
function MusicSources({
  timeline,
  projectId,
  unavailable,
  onAdd,
}: Parameters<typeof TimelineSources>[0]) {
  const fetchPage = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      api<MusicAsset[]>(
        `/projects/${encodeURIComponent(projectId)}/assets?asset_type=MUSIC&limit=30&offset=${offset ?? 0}`,
        { signal },
      ),
    [projectId],
  );
  const assets = useAudioPage(projectId, fetchPage, identity, unavailable);
  const options = assets.rows.filter(
    (row) =>
      row.project_id === projectId &&
      row.asset_type === "MUSIC" &&
      row.current_version_id &&
      !["ARCHIVED", "REJECTED", "STALE"].includes(row.status),
  );
  const [selectedId, select] = useState("");
  const selected = options.find((row) => row.id === selectedId) ?? options[0];
  const fetchVersion = useCallback(
    async (signal: AbortSignal) => {
      const row = await api<{
        id: string;
        asset_id: string;
        checksum: string;
        status: string;
        stale: boolean;
        audio_resource: {
          purpose_role: ClipAudio["purpose_role"];
          validation: { duration_ms: number };
        };
      }>(
        `/projects/${encodeURIComponent(projectId)}/assets/${selected!.id}/versions/${selected!.current_version_id}`,
        { signal },
      );
      if (
        row.id !== selected!.current_version_id ||
        row.asset_id !== selected!.id ||
        !row.audio_resource
      )
        throw new Error("声音素材与当前选用版本不一致。");
      return row;
    },
    [projectId, selected],
  );
  const version = useRenderDetail(
    `${projectId}:${selected?.current_version_id ?? ""}`,
    fetchVersion,
    unavailable,
    !!selected,
  );
  const [shotId, setShot] = useState("");
  const shots = timeline.snapshot.tracks.find((t) => t.kind === "VIDEO")!.clips;
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <h4>加入音乐与音效</h4>
      <select
        aria-label="装配音乐音效素材"
        className={selectClass}
        value={selected?.id ?? ""}
        onChange={(e) => select(e.target.value)}
      >
        {!selected && <option value="">暂无已选用声音素材</option>}
        {options.map((row) => (
          <option key={row.id} value={row.id}>
            {row.name}
          </option>
        ))}
      </select>
      {assets.hasMore && (
        <Button
          variant="outline"
          disabled={assets.loading}
          onClick={() => void assets.load(true)}
        >
          加载更多声音素材
        </Button>
      )}
      <select
        aria-label="音效关联镜头"
        className={selectClass}
        value={shotId}
        onChange={(e) => setShot(e.target.value)}
      >
        <option value="">不指定镜头</option>
        {shots.map((clip) => (
          <option key={clip.id} value={clip.shot_id!}>
            镜头 {clip.ordinal + 1}
          </option>
        ))}
      </select>
      {(assets.error || version.error) && (
        <Notice tone="error">{assets.error || version.error}</Notice>
      )}
      <Button
        variant="outline"
        disabled={
          !version.data ||
          version.data.id !== selected?.current_version_id ||
          !usable(version.data) ||
          version.loading
        }
        onClick={() => {
          const row = version.data;
          if (!row) return;
          const role = row.audio_resource.purpose_role;
          onAdd(role === "BACKGROUND" ? "MUSIC" : "SFX", {
            ...newClip(
              row.id,
              row.checksum,
              Math.min(
                row.audio_resource.validation.duration_ms,
                timeline.snapshot.duration_ms,
              ),
            ),
            shot_id: shotId || null,
            audio: {
              purpose_role: role,
              gain_milli: 1000,
              fade_in_ms: 0,
              fade_out_ms: 0,
              envelope: [],
            },
          });
        }}
      >
        将此音乐或音效加入草稿
      </Button>
    </div>
  );
}
