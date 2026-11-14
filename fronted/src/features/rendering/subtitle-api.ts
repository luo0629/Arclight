import "client-only";

import { api, json } from "@/lib/api/client";
import type { AssetVersion } from "@/types/api";
import type { TimelineClip } from "./types";

export type SubtitleCue = {
  cue_no: number;
  text: string;
  start_ms: number;
  duration_ms: number;
  line_id: string;
  script_version_id: string;
  audio_asset_version_id: string;
};
export type SubtitleTrack = {
  id: string;
  asset_id: string;
  version_no: number;
  status: string;
  stale: boolean;
  checksum: string;
  timeline_id: string;
  cues: SubtitleCue[];
  source_cues?: SubtitleCue[] | null;
  clips: TimelineClip[];
  next_offset?: number;
};
export type SubtitleChange = {
  cue_no: number;
  text: string;
  start_ms: number;
  end_ms: number;
};
const id = encodeURIComponent;
const assetPath = (project: string, asset: string) =>
  `/projects/${id(project)}/assets/${id(asset)}`;
export const subtitleApi = {
  async list(episode: string, offset: number | undefined, signal: AbortSignal) {
    const rows = await api<SubtitleTrack[]>(
      `/episodes/${id(episode)}/subtitle-tracks?limit=30&offset=${offset ?? 0}`,
      { signal },
    );
    return rows.map((row, index) => ({
      ...row,
      next_offset: (offset ?? 0) + index + 1,
    }));
  },
  async detail(
    project: string,
    episode: string,
    versionId: string,
    signal: AbortSignal,
  ) {
    const track = await api<SubtitleTrack>(
      `/subtitle-tracks/${id(versionId)}`,
      { signal },
    );
    const timeline = await api<{ snapshot: { episode_id: string } }>(
      `/timelines/${id(track.timeline_id)}`,
      { signal },
    );
    const asset = await api<{
      id: string;
      project_id: string;
      revision: number;
      status: string;
      asset_type: string;
    }>(assetPath(project, track.asset_id), { signal });
    const version = await api<AssetVersion>(
      `${assetPath(project, track.asset_id)}/versions/${id(track.id)}`,
      { signal },
    );
    if (
      track.id !== versionId ||
      timeline.snapshot.episode_id !== episode ||
      asset.project_id !== project ||
      asset.id !== track.asset_id ||
      asset.asset_type !== "SUBTITLE" ||
      version.id !== track.id ||
      version.asset_id !== track.asset_id ||
      version.checksum !== track.checksum
    )
      throw new Error("字幕来源与当前剧集或精确版本不一致。");
    return { track, asset, version };
  },
  generate: (
    episode: string,
    timeline: string,
    key: string,
    signal: AbortSignal,
  ) =>
    api<SubtitleTrack>(`/episodes/${id(episode)}/subtitle-tracks`, {
      ...json("POST", { creation_key: key, timeline_id: timeline }),
      signal,
    }),
  edit: (
    source: string,
    revision: number,
    changes: SubtitleChange[],
    key: string,
    signal: AbortSignal,
  ) =>
    api<SubtitleTrack>(`/subtitle-tracks/${id(source)}/edits`, {
      ...json("POST", {
        creation_key: key,
        expected_revision: revision,
        changes,
      }),
      signal,
    }),
  state: (
    project: string,
    track: SubtitleTrack,
    revision: number,
    status: string,
    signal: AbortSignal,
  ) =>
    api<AssetVersion>(
      `${assetPath(project, track.asset_id)}/versions/${id(track.id)}/state`,
      {
        ...json("PATCH", { expected_revision: revision, status }),
        signal,
      },
    ),
};
