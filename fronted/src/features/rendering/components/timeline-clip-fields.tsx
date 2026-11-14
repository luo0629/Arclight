"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ClipVisual, CropRect, TimelineClip } from "../types";
import { defaultVisual } from "../timeline-editing";
import { selectClass } from "./render-format";

export function NumberField({
  label,
  value,
  onChange,
  min = 0,
  max = 3600000,
  step = 1,
  disabled,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-sm">
      {label}
      <Input
        type="number"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : ""}
        disabled={disabled}
        onChange={(event) =>
          onChange(event.target.value === "" ? 0 : Number(event.target.value))
        }
      />
    </label>
  );
}

function CropFields({
  title,
  crop,
  disabled,
  onChange,
}: {
  title: string;
  crop: CropRect;
  disabled: boolean;
  onChange: (value: CropRect) => void;
}) {
  return (
    <fieldset className="grid min-w-0 grid-cols-2 gap-2" disabled={disabled}>
      <legend className="mb-2 text-sm font-medium">
        {title}（原图百分比）
      </legend>
      {(
        [
          ["x", "左侧"],
          ["y", "顶部"],
          ["width", "宽度"],
          ["height", "高度"],
        ] as const
      ).map(([key, label]) => (
        <NumberField
          key={key}
          label={`${title}${label}（%）`}
          value={crop[key] / 100}
          min={key === "width" || key === "height" ? 0.01 : 0}
          max={100}
          step={0.01}
          onChange={(value) =>
            onChange({ ...crop, [key]: Math.round(value * 100) })
          }
        />
      ))}
    </fieldset>
  );
}

export default function TimelineClipFields({
  clip,
  kind,
  disabled,
  onChange,
  onOpenAudio,
}: {
  clip: TimelineClip;
  kind: string;
  disabled: boolean;
  onChange: (patch: Partial<TimelineClip>) => void;
  onOpenAudio?: () => void;
}) {
  const visual = clip.visual ?? defaultVisual();
  const setVisual = (patch: Partial<ClipVisual>) =>
    onChange({ visual: { ...visual, ...patch } });
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {kind === "SUBTITLE" ? (
        <>
          <p className="text-sm text-muted-foreground">
            字幕文字与时点绑定已保存的精确字幕版本。请在下方字幕管理中另存、批准并加入时间轴。
          </p>
          {onOpenAudio && (
            <Button variant="outline" onClick={onOpenAudio}>
              编辑字幕文字与时点
            </Button>
          )}
        </>
      ) : (
        <fieldset
          className="grid min-w-0 grid-cols-2 gap-2"
          disabled={disabled}
        >
          {kind !== "VIDEO" && (
            <NumberField
              label="片段开始（毫秒）"
              value={clip.start_ms}
              onChange={(start_ms) => onChange({ start_ms })}
            />
          )}
          <NumberField
            label="片段时长（毫秒）"
            min={1}
            value={clip.duration_ms}
            onChange={(duration_ms) => onChange({ duration_ms })}
          />
          {kind !== "VIDEO" && (
            <NumberField
              label="源音频入点（毫秒）"
              value={clip.source_start_ms}
              onChange={(source_start_ms) => onChange({ source_start_ms })}
            />
          )}
        </fieldset>
      )}
      {kind === "VIDEO" && (
        <>
          <CropFields
            title="起始裁切"
            crop={visual.crop}
            disabled={disabled}
            onChange={(crop) => setVisual({ crop })}
          />
          <label className="flex flex-col gap-1 text-sm">
            轻量运镜
            <select
              aria-label="轻量运镜"
              className={selectClass}
              value={visual.motion.kind}
              disabled={disabled}
              onChange={(event) =>
                setVisual({
                  motion:
                    event.target.value === "STATIC"
                      ? { kind: "STATIC", end_crop: null }
                      : { kind: "LINEAR", end_crop: { ...visual.crop } },
                })
              }
            >
              <option value="STATIC">静止画面</option>
              <option value="LINEAR">线性推拉／平移</option>
            </select>
          </label>
          {visual.motion.kind === "LINEAR" && (
            <CropFields
              title="结束裁切"
              crop={visual.motion.end_crop ?? visual.crop}
              disabled={disabled}
              onChange={(end_crop) =>
                setVisual({ motion: { kind: "LINEAR", end_crop } })
              }
            />
          )}
          <label className="flex flex-col gap-1 text-sm">
            进入转场
            <select
              aria-label="进入转场"
              className={selectClass}
              value={visual.transition.kind}
              disabled={disabled || clip.ordinal === 0}
              onChange={(event) =>
                setVisual({
                  transition:
                    event.target.value === "CUT"
                      ? { kind: "CUT", duration_ms: 0 }
                      : { kind: "FADE", duration_ms: 200 },
                })
              }
            >
              <option value="CUT">硬切</option>
              <option value="FADE">经过黑场淡出／淡入</option>
            </select>
          </label>
          {visual.transition.kind === "FADE" && (
            <NumberField
              label="转场时长（毫秒）"
              value={visual.transition.duration_ms}
              min={100}
              max={2000}
              disabled={disabled}
              onChange={(duration_ms) =>
                setVisual({ transition: { kind: "FADE", duration_ms } })
              }
            />
          )}
          <p className="text-xs text-muted-foreground">
            调整顺序或时长会顺延画面，声音和字幕保持原时点。首镜头固定硬切。
          </p>
        </>
      )}
      {clip.audio && (
        <div className="flex flex-col gap-2 text-sm">
          <p>
            音量 {clip.audio.gain_milli / 10}%，淡入 {clip.audio.fade_in_ms}{" "}
            毫秒、淡出 {clip.audio.fade_out_ms} 毫秒，
            {clip.audio.envelope.length} 个包络点。
          </p>
          <Button
            variant="outline"
            disabled={disabled || clip.duration_ms < 1}
            onClick={() => {
              const audio = clip.audio!;
              const end =
                audio.envelope.at(-1)?.offset_ms ??
                Math.max(
                  clip.duration_ms,
                  audio.fade_in_ms + audio.fade_out_ms,
                );
              const ratio = clip.duration_ms / Math.max(1, end);
              onChange({
                audio: {
                  ...audio,
                  fade_in_ms: Math.round(audio.fade_in_ms * ratio),
                  fade_out_ms: Math.round(audio.fade_out_ms * ratio),
                  envelope: audio.envelope.map((p) => ({
                    ...p,
                    offset_ms: Math.round(p.offset_ms * ratio),
                  })),
                },
              });
            }}
          >
            按新时长缩放自动化
          </Button>
        </div>
      )}
      {kind === "VOICE" && onOpenAudio && (
        <Button variant="outline" onClick={onOpenAudio}>
          编辑台词与配音来源
        </Button>
      )}
      <details>
        <summary>片段精确来源</summary>
        <dl className="mt-2 grid gap-1 text-xs [&_dd]:break-all [&_dt]:text-muted-foreground">
          <dt>素材版本</dt>
          <dd>{clip.source_asset_version_id}</dd>
          <dt>素材 SHA256</dt>
          <dd>{clip.source_sha256}</dd>
          {clip.shot_id && (
            <>
              <dt>镜头</dt>
              <dd>{clip.shot_id}</dd>
            </>
          )}
          {clip.line_id && (
            <>
              <dt>台词</dt>
              <dd>{clip.line_id}</dd>
              <dt>剧本版本</dt>
              <dd>{clip.script_version_id}</dd>
            </>
          )}
          {clip.audio_version_id && (
            <>
              <dt>字幕来源音频</dt>
              <dd>{clip.audio_version_id}</dd>
            </>
          )}
        </dl>
      </details>
    </div>
  );
}
