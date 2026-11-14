import type {
  ClipVisual,
  CropRect,
  TimelineClip,
  TimelineSnapshot,
} from "./types";

export const trackNames = {
  VIDEO: "画面",
  VOICE: "配音",
  SFX: "音效",
  MUSIC: "音乐",
  SUBTITLE: "字幕",
};
export const defaultVisual = (): ClipVisual => ({
  crop: { x: 0, y: 0, width: 10000, height: 10000 },
  motion: { kind: "STATIC", end_crop: null },
  transition: { kind: "CUT", duration_ms: 0 },
});
const integer = (n: number, min = 0, max = 3600000) =>
  Number.isInteger(n) && n >= min && n <= max;
const validCrop = (c: CropRect) =>
  c &&
  [c.x, c.y, c.width, c.height].every(Number.isInteger) &&
  c.x >= 0 &&
  c.y >= 0 &&
  c.width > 0 &&
  c.height > 0 &&
  c.x + c.width <= 10000 &&
  c.y + c.height <= 10000;

/** Reflow picture only. Frozen audio and subtitle cues retain their absolute times. */
export function arrangeVideo(snapshot: TimelineSnapshot): TimelineSnapshot {
  let end = 0;
  const tracks = snapshot.tracks.map((track) =>
    track.kind !== "VIDEO"
      ? track
      : {
          ...track,
          clips: [...track.clips]
            .sort((a, b) => a.ordinal - b.ordinal)
            .map((clip, ordinal) => {
              const visual = clip.visual ?? defaultVisual();
              const next = {
                ...clip,
                ordinal,
                start_ms: end,
                visual:
                  ordinal === 0
                    ? {
                        ...visual,
                        transition: { kind: "CUT" as const, duration_ms: 0 },
                      }
                    : visual,
              };
              end += clip.duration_ms;
              return next;
            }),
        },
  );
  return { ...snapshot, schema_version: 2, duration_ms: end, tracks };
}

export function changeClip(
  snapshot: TimelineSnapshot,
  id: string,
  patch: Partial<TimelineClip>,
) {
  const next = {
    ...snapshot,
    tracks: snapshot.tracks.map((track) => ({
      ...track,
      clips: track.clips.map((clip) =>
        clip.id === id ? { ...clip, ...patch } : clip,
      ),
    })),
  };
  return snapshot.tracks.some(
    (t) => t.kind === "VIDEO" && t.clips.some((c) => c.id === id),
  )
    ? arrangeVideo(next)
    : next;
}

export function moveShot(
  snapshot: TimelineSnapshot,
  id: string,
  delta: number,
) {
  const next = {
    ...snapshot,
    tracks: snapshot.tracks.map((track) => {
      if (track.kind !== "VIDEO") return track;
      const clips = [...track.clips].sort((a, b) => a.ordinal - b.ordinal);
      const index = clips.findIndex((clip) => clip.id === id);
      if (index < 0 || !clips[index + delta]) return track;
      [clips[index], clips[index + delta]] = [
        clips[index + delta],
        clips[index],
      ];
      return {
        ...track,
        clips: clips.map((clip, ordinal) => ({ ...clip, ordinal })),
      };
    }),
  };
  return arrangeVideo(next);
}

export function timelineIssues(snapshot: TimelineSnapshot) {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (!integer(snapshot.duration_ms, 1))
    errors.push("总时长须为 1～3600000 毫秒。");
  for (const track of snapshot.tracks) {
    const clips = [...track.clips].sort((a, b) => a.ordinal - b.ordinal);
    clips.forEach((clip, index) => {
      const name = `${trackNames[track.kind]}片段 ${index + 1}`;
      if (
        !integer(clip.start_ms) ||
        !integer(clip.duration_ms, 1) ||
        !integer(clip.source_start_ms) ||
        clip.start_ms + clip.duration_ms > snapshot.duration_ms
      )
        errors.push(`${name}越过时间轴范围或时长无效。`);
      const previous = clips[index - 1];
      if (
        previous &&
        clip.start_ms < previous.start_ms + previous.duration_ms &&
        track.enabled !== false
      ) {
        if (track.kind === "SUBTITLE")
          errors.push(
            `${name}与前一条字幕重叠。请在下方字幕管理中另存修正版本。`,
          );
        else if (track.kind !== "VIDEO")
          warnings.push(`${name}与前一片段重叠，渲染时会混音。`);
      }
      if (
        clip.audio?.envelope.length &&
        (clip.audio.envelope.length < 2 ||
          clip.audio.envelope[0].offset_ms !== 0 ||
          clip.audio.envelope.some(
            (p, i, points) =>
              !integer(p.offset_ms) ||
              !integer(p.gain_milli, 0, 4000) ||
              (i > 0 && p.offset_ms <= points[i - 1].offset_ms),
          ))
      )
        errors.push(`${name}的音量包络须从零开始并严格递增。`);
      if (
        clip.audio &&
        (clip.audio.fade_in_ms + clip.audio.fade_out_ms > clip.duration_ms ||
          (clip.audio.envelope.length > 0 &&
            clip.audio.envelope.at(-1)!.offset_ms !== clip.duration_ms))
      )
        errors.push(
          `${name}的新时长与已有音量包络或淡入淡出不符。请调整时长或明确缩放自动化。`,
        );
      const v = clip.visual;
      if (!v) return;
      if (!validCrop(v.crop)) errors.push(`${name}裁切视窗须完整位于原图内。`);
      if (v.motion.kind === "LINEAR") {
        const a = v.crop,
          b = v.motion.end_crop;
        if (
          !b ||
          !validCrop(b) ||
          a.width * b.height !== b.width * a.height ||
          (a.x === b.x &&
            a.y === b.y &&
            a.width === b.width &&
            a.height === b.height) ||
          Math.min(a.width, b.width) * 5 < Math.max(a.width, b.width) * 4 ||
          Math.abs(2 * b.x + b.width - 2 * a.x - a.width) * 2 >
            Math.min(a.width, b.width) ||
          Math.abs(2 * b.y + b.height - 2 * a.y - a.height) * 2 >
            Math.min(a.height, b.height)
        )
          errors.push(
            `${name}轻量运镜须保持比例、起止不同、缩放不超过 1.25 倍且平移不超过视窗四分之一。`,
          );
      }
      if (
        v.transition.kind === "FADE" &&
        (!integer(v.transition.duration_ms, 100, 2000) ||
          !previous ||
          v.transition.duration_ms > previous.duration_ms ||
          v.transition.duration_ms > clip.duration_ms)
      )
        errors.push(`${name}转场须为 100～2000 毫秒，且不能超过相邻镜头时长。`);
    });
    if (track.kind === "VIDEO") {
      for (let i = 0; i < clips.length; i++) {
        const incoming = clips[i].visual?.transition.duration_ms ?? 0;
        const outgoing = clips[i + 1]?.visual?.transition.duration_ms ?? 0;
        if (
          Math.ceil(incoming / 2) + Math.floor(outgoing / 2) >
          clips[i].duration_ms
        )
          errors.push(`画面片段 ${i + 1} 的前后转场窗口重叠。`);
      }
    }
  }
  return { errors: [...new Set(errors)], warnings: [...new Set(warnings)] };
}

/** Restore shaped edits only to the same base. The server revalidates every imported exact source. */
export function validDraft(
  value: unknown,
  base: TimelineSnapshot,
): value is TimelineSnapshot {
  if (!value || typeof value !== "object") return false;
  const candidate = value as TimelineSnapshot;
  if (
    candidate.episode_id !== base.episode_id ||
    candidate.storyboard_id !== base.storyboard_id ||
    ![1, 2].includes(candidate.schema_version) ||
    !Array.isArray(candidate.tracks) ||
    candidate.tracks.length !== 5 ||
    !Number.isFinite(candidate.duration_ms) ||
    JSON.stringify(candidate.output_profile) !==
      JSON.stringify(base.output_profile)
  )
    return false;
  return candidate.tracks.every((track, index) => {
    const original = base.tracks[index];
    if (
      !track ||
      track.id !== original.id ||
      track.kind !== original.kind ||
      track.ordinal !== original.ordinal ||
      (track.enabled !== undefined && typeof track.enabled !== "boolean") ||
      !Array.isArray(track.clips) ||
      track.clips.length > 256 ||
      new Set(track.clips.map((c) => c?.id)).size !== track.clips.length
    )
      return false;
    return track.clips.every((clip) => {
      if (
        !clip ||
        typeof clip !== "object" ||
        typeof clip.id !== "string" ||
        typeof clip.source_asset_version_id !== "string" ||
        typeof clip.source_sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(clip.source_sha256) ||
        ![
          clip.ordinal,
          clip.start_ms,
          clip.duration_ms,
          clip.source_start_ms,
        ].every(Number.isFinite)
      )
        return false;
      if (clip.visual) {
        const v = clip.visual;
        const shaped = (rect: CropRect) =>
          rect &&
          [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite);
        if (
          !shaped(v.crop) ||
          !v.motion ||
          !["STATIC", "LINEAR"].includes(v.motion.kind) ||
          (v.motion.kind === "LINEAR" && !shaped(v.motion.end_crop!)) ||
          !v.transition ||
          !["CUT", "FADE"].includes(v.transition.kind) ||
          !Number.isFinite(v.transition.duration_ms)
        )
          return false;
      }
      if (
        clip.audio &&
        (!Array.isArray(clip.audio.envelope) ||
          ![
            clip.audio.gain_milli,
            clip.audio.fade_in_ms,
            clip.audio.fade_out_ms,
          ].every(Number.isFinite) ||
          clip.audio.envelope.some(
            (p) =>
              !p ||
              !Number.isFinite(p.offset_ms) ||
              !Number.isFinite(p.gain_milli),
          ))
      )
        return false;
      return true;
    });
  });
}

/** Place overlapping clips on separate rows without obscuring selectable controls. */
export function clipLanes(clips: TimelineClip[]) {
  const ends: number[] = [];
  const positions = new Map<string, number>();
  for (const clip of [...clips].sort(
    (a, b) => a.start_ms - b.start_ms || a.ordinal - b.ordinal,
  )) {
    let lane = ends.findIndex((end) => end <= clip.start_ms);
    if (lane < 0) lane = ends.length;
    ends[lane] = clip.start_ms + clip.duration_ms;
    positions.set(clip.id, lane);
  }
  return { positions, height: Math.max(1, ends.length) * 38 + 12 };
}
