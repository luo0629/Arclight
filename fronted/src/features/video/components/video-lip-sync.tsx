import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { readLipSyncOptions, saveLipSync } from "../api";
import type useVideoCandidate from "../hooks/use-video-candidate";
import type {
  LipSyncCue,
  LipSyncOptions,
  MouthRegion,
  VideoCandidate,
} from "../types";
import VideoPreview from "./video-preview";

const bounds = {
  x: ["口部左边", 0, 9999],
  y: ["口部上边", 0, 9999],
  width: ["口部宽度", 1, 4000],
  height: ["口部高度", 1, 3000],
} as const;
const edges = Object.keys(bounds) as (keyof MouthRegion)[];
const poses = [
  ["closed_frame_ms", "闭嘴帧"],
  ["open_frame_ms", "张嘴帧"],
] as const;
const presentationLabels = {
  visible: "可见对白",
  voice_over: "旁白",
  off_screen: "画外音",
  obscured: "口部遮挡",
};
type Draft = {
  audio_version_id: string;
  start_ms: string;
  closed_frame_ms: string;
  open_frame_ms: string;
  mouth: Record<keyof MouthRegion, string>;
};

function region(values: Draft["mouth"] | undefined): MouthRegion | null {
  if (
    !values ||
    !edges.every((key) => {
      const value = Number(values[key]);
      return (
        values[key] &&
        Number.isInteger(value) &&
        value >= bounds[key][1] &&
        value <= bounds[key][2]
      );
    })
  )
    return null;
  const { x, y, width, height } = values;
  const box = {
    x: Number(x),
    y: Number(y),
    width: Number(width),
    height: Number(height),
  };
  return box.x + box.width <= 10000 && box.y + box.height <= 10000 ? box : null;
}

export default function VideoLipSyncPanel({
  candidate,
  state,
  disabled,
  unavailable,
  onChanged,
  onCreated,
}: {
  candidate: VideoCandidate;
  state: ReturnType<typeof useVideoCandidate>;
  disabled: boolean;
  unavailable: (cause: unknown) => void;
  onChanged: () => Promise<void>;
  onCreated?: (id: string) => Promise<void>;
}) {
  const { id, project_id: projectId, lip_sync: receipt } = candidate;
  const [options, setOptions] = useState<LipSyncOptions | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [active, setActive] = useState(0);
  const [rights, setRights] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const preview = useRef<HTMLVideoElement>(null);
  const pending = useRef<AbortController | null>(null);
  const submission = useRef<{ intent: string; key: string } | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const media = candidate.media_check?.video;
  const duration = media?.duration_ms ?? 0;
  const eligible =
    !!candidate.actions?.can_lip_sync &&
    duration <= 15000 &&
    (media?.byte_size ?? 0) <= 16 * 1024 ** 2;
  const required =
    options?.dialogue.filter((line) => line.lipsync === "required") ?? [];
  const exempt =
    options?.dialogue.filter((line) => line.lipsync === "not_applicable") ?? [];

  async function load() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError("");
    try {
      const fresh = await readLipSyncOptions(projectId, id, controller.signal);
      if (controller.signal.aborted) return;
      setOptions(fresh);
      setDrafts(
        fresh.dialogue
          .filter((line) => line.lipsync === "required")
          .map((line) => ({
            audio_version_id:
              line.sources.length === 1 ? line.sources[0].audio_version_id : "",
            start_ms: "0",
            closed_frame_ms: "",
            open_frame_ms: "",
            mouth: { x: "", y: "", width: "", height: "" },
          })),
      );
      setActive(0);
    } catch (cause) {
      if (!controller.signal.aborted) setError(message(cause));
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  function update(index: number, values: Partial<Draft>) {
    setDrafts((items) =>
      items.map((item, position) =>
        position === index ? { ...item, ...values } : item,
      ),
    );
    setError("");
  }
  function markFrame(index: number, field: (typeof poses)[number][0]) {
    const video = preview.current;
    if (!video || video.readyState < 2 || video.seeking) {
      setError("请先读取原候选预览并等待定位到参考画面。");
      return;
    }
    video.pause();
    update(index, {
      [field]: String(
        Math.min(duration - 1, Math.ceil(video.currentTime * 1000)),
      ),
    });
  }
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || !eligible || !state.asset || !options) return;
    const cues: LipSyncCue[] = [];
    let boundary = 0;
    for (const [index, line] of required.entries()) {
      const draft = drafts[index];
      const audio = line.sources.find(
        (item) => item.audio_version_id === draft.audio_version_id,
      );
      const mouth = region(draft.mouth);
      const start = Number(draft.start_ms),
        closed = Number(draft.closed_frame_ms),
        opened = Number(draft.open_frame_ms);
      if (
        !audio ||
        !mouth ||
        ![draft.start_ms, draft.closed_frame_ms, draft.open_frame_ms].every(
          Boolean,
        ) ||
        ![start, closed, opened].every(
          (time) => Number.isInteger(time) && time >= 0 && time < duration,
        ) ||
        closed === opened ||
        start < boundary ||
        start + audio.duration_ms > duration
      ) {
        setError(
          `第 ${index + 1} 句：请选择精确配音，检查顺序、不同参考帧和画面内的口部框。`,
        );
        return;
      }
      cues.push({
        line_id: line.line_id,
        audio_version_id: audio.audio_version_id,
        start_ms: start,
        closed_frame_ms: closed,
        open_frame_ms: opened,
        mouth,
      });
      boundary = start + audio.duration_ms;
    }
    const intent = {
      cues,
      ...(options.requires_video_rights_confirmation
        ? { source_rights_note: rights.trim() }
        : {}),
    };
    if (
      options.requires_video_rights_confirmation &&
      !intent.source_rights_note
    )
      return;
    const fingerprint = JSON.stringify(intent);
    if (submission.current?.intent !== fingerprint)
      submission.current = {
        intent: fingerprint,
        key: globalThis.crypto.randomUUID(),
      };
    const request = {
      ...intent,
      creation_key: submission.current.key,
      expected_revision: state.asset.revision,
    };
    const saved = await state.perform(
      (signal) => saveLipSync(projectId, candidate, request, signal),
      "已另存口型校正候选。请播放检查后重新送审；原候选未改动。",
    );
    if (saved) {
      submission.current = null;
      if (onCreated) await onCreated(saved.id);
      else await onChanged();
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      {state.fresh && (
        <VideoPreview
          projectId={projectId}
          candidate={candidate}
          unavailable={unavailable}
          playerRef={options ? preview : undefined}
          mouth={region(drafts[active]?.mouth)}
        />
      )}
      {receipt ? (
        <section
          aria-label="口型校正回执"
          className="flex flex-col gap-2 border-t pt-3 text-sm"
        >
          <h5 className="font-semibold">
            口型校正 · {receipt.lines.length} 句
          </h5>
          <p>
            请播放候选试听开口与停顿。正式时间轴通过配音轨使用同一音频，不重复混入试听轨。
          </p>
          <ul className="grid gap-1 text-xs text-muted-foreground">
            {receipt.lines.map((line, index) => (
              <li key={line.line_id}>
                第 {index + 1} 句 · {line.start_ms}–
                {line.start_ms + line.duration_ms} ms
              </li>
            ))}
          </ul>
          {onCreated && (
            <Button
              className="self-start"
              type="button"
              variant="outline"
              disabled={disabled}
              onClick={() => void onCreated(receipt.source_candidate_id)}
            >
              打开原候选局部返修
            </Button>
          )}
        </section>
      ) : candidate.lip_sync_required ? (
        <section
          aria-label="可见对白口型校正"
          className="flex flex-col items-start gap-3 border-t pt-3"
        >
          <h5 className="text-sm font-semibold">可见对白口型校正</h5>
          <p className="text-sm text-muted-foreground">
            使用已选配音和原候选的张闭嘴参考帧，另存新候选。适合口部位置较稳定的正面镜头。
          </p>
          {!eligible && candidate.actions?.can_lip_sync && (
            <Notice>
              本机校正仅支持 15 秒、16 MiB 以内的视频。请先准备符合限制的镜头。
            </Notice>
          )}
          {eligible && !options && (
            <Button
              type="button"
              variant="outline"
              disabled={disabled || loading}
              onClick={() => void load()}
            >
              {loading ? "正在读取精确配音…" : "设置口型校正"}
            </Button>
          )}
          {eligible && options && (
            <form
              className="grid w-full min-w-0 gap-4"
              onSubmit={(event) => void submit(event)}
            >
              <p className="text-xs text-muted-foreground">
                预览中暂停并记录参考帧，也可直接填毫秒。口部框按画面万分比标注，5000
                表示 50%。
              </p>
              {required.map((line, index) => {
                const draft = drafts[index];
                return (
                  <fieldset
                    key={line.line_id}
                    disabled={disabled}
                    className="grid min-w-0 gap-3 border-t pt-3"
                    onFocus={() => setActive(index)}
                  >
                    <legend className="text-sm font-medium">
                      第 {index + 1} 句 · {line.speaker}
                    </legend>
                    <p className="break-words text-sm">{line.text}</p>
                    <label className="grid gap-1 text-sm">
                      精确配音版本
                      <NativeSelect
                        aria-label="精确配音版本"
                        className="w-full"
                        required
                        value={draft.audio_version_id}
                        onChange={(event) =>
                          update(index, {
                            audio_version_id: event.target.value,
                          })
                        }
                      >
                        <NativeSelectOption value="">
                          请选择已选配音
                        </NativeSelectOption>
                        {line.sources.map((source, sourceIndex) => (
                          <NativeSelectOption
                            key={source.audio_version_id}
                            value={source.audio_version_id}
                          >
                            配音 {sourceIndex + 1} · {source.name} ·{" "}
                            {source.duration_ms} ms
                          </NativeSelectOption>
                        ))}
                      </NativeSelect>
                    </label>
                    {!line.sources.length && (
                      <Notice>
                        请先在配音区审查并选用本句音频，然后重新读取。
                      </Notice>
                    )}
                    <label className="grid gap-1 text-sm">
                      台词开始（ms）
                      <Input
                        type="number"
                        required
                        min={0}
                        max={duration - 1}
                        value={draft.start_ms}
                        onChange={(event) =>
                          update(index, { start_ms: event.target.value })
                        }
                      />
                    </label>
                    <div className="grid gap-2 sm:grid-cols-2">
                      {poses.map(([field, label]) => (
                        <div key={field} className="grid gap-1">
                          <label className="grid gap-1 text-sm">
                            {label}（ms）
                            <Input
                              type="number"
                              required
                              min={0}
                              max={duration - 1}
                              value={draft[field]}
                              onChange={(event) =>
                                update(index, { [field]: event.target.value })
                              }
                            />
                          </label>
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => markFrame(index, field)}
                          >
                            取当前画面为{label}
                          </Button>
                        </div>
                      ))}
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {edges.map((key) => (
                        <label key={key} className="grid min-w-0 gap-1 text-sm">
                          {bounds[key][0]}
                          <Input
                            type="number"
                            required
                            min={bounds[key][1]}
                            max={bounds[key][2]}
                            value={draft.mouth[key]}
                            onChange={(event) =>
                              update(index, {
                                mouth: {
                                  ...draft.mouth,
                                  [key]: event.target.value,
                                },
                              })
                            }
                          />
                        </label>
                      ))}
                    </div>
                  </fieldset>
                );
              })}
              {exempt.length > 0 && (
                <ul className="grid gap-1 text-xs text-muted-foreground">
                  {exempt.map((line) => (
                    <li key={line.line_id}>
                      {presentationLabels[line.presentation]} · {line.speaker}：
                      {line.exemption_reason}
                    </li>
                  ))}
                </ul>
              )}
              {options.requires_video_rights_confirmation && (
                <label className="grid gap-1 text-sm">
                  原候选可作口型衍生的使用权声明
                  <Textarea
                    disabled={disabled}
                    required
                    maxLength={1000}
                    value={rights}
                    onChange={(event) => setRights(event.target.value)}
                  />
                </label>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  type="submit"
                  disabled={
                    disabled ||
                    !required.length ||
                    required.length > 12 ||
                    required.some((line) => !line.sources.length)
                  }
                >
                  {state.busy ? "正在校正…" : "生成并另存口型候选"}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled={disabled || loading}
                  onClick={() => void load()}
                >
                  重新读取配音
                </Button>
              </div>
            </form>
          )}
          {error && (
            <Notice role="alert" tone="error">
              {error}
            </Notice>
          )}
        </section>
      ) : null}
    </div>
  );
}
