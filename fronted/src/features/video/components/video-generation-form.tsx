"use client";

import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Notice } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import useVideoGeneration from "../hooks/use-video-generation";

type Props = {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shotId: string;
  description: string;
  writable: boolean;
  onChanged: () => Promise<void>;
  unavailable: (cause: unknown) => void;
};
function Fields(props: Props) {
  const state = useVideoGeneration(props);
  const id = useId();
  const [frameId, setFrameId] = useState("");
  const [prompt, setPrompt] = useState(props.description);
  const [width, setWidth] = useState(1280),
    [height, setHeight] = useState(720),
    [seconds, setSeconds] = useState(6);
  const [confirmed, setConfirmed] = useState(false);
  const config = state.configuration,
    caps = config?.capabilities;
  const disabled = !props.writable || state.busy || state.blocked;
  const frozen = !!state.pending;
  const frame = state.frames.find((row) => row.id === frameId);
  useEffect(() => {
    if (!caps) return;
    const size = caps.sizes[0];
    if (size) {
      setWidth(size.width);
      setHeight(size.height);
    }
    if (caps.max_video_seconds)
      setSeconds((value) => Math.min(value, caps.max_video_seconds!));
  }, [caps]);
  return (
    <section
      aria-label="单镜头视频生成"
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-3 text-sm"
    >
      <h5 className="font-semibold">为当前精确镜头生成视频</h5>
      {state.error && (
        <Notice role="alert" tone="error">
          {state.error}
        </Notice>
      )}
      <p className="break-all">
        账号视频模型：{config?.model ?? "正在读取"} · 配置{" "}
        {config?.configuration_version ?? "—"}
      </p>
      {config && !config.enabled && (
        <Notice>{config.message ?? "请先配置有可信报价的视频服务。"}</Notice>
      )}
      <Button
        variant="outline"
        disabled={disabled || frozen}
        onClick={() => void state.refresh()}
      >
        刷新视频模型与首帧
      </Button>
      <label htmlFor={id + "-frame"}>已批准首帧精确版本</label>
      <NativeSelect
        id={id + "-frame"}
        disabled={disabled || frozen}
        value={frameId}
        onChange={(event) => setFrameId(event.target.value)}
      >
        <NativeSelectOption value="">选择首帧版本</NativeSelectOption>
        {state.frames
          .filter(
            (row) => !row.stale && ["APPROVED", "LOCKED"].includes(row.status),
          )
          .map((row) => (
            <NativeSelectOption key={row.id} value={row.id}>
              {row.asset_name} · v{row.version_no} · {row.id}
            </NativeSelectOption>
          ))}
      </NativeSelect>
      {state.hasMore && (
        <Button
          variant="outline"
          disabled={disabled || frozen}
          onClick={() => void state.loadMore()}
        >
          加载更多首帧版本
        </Button>
      )}
      {frame && (
        <VersionImage
          key={frame.id}
          compact
          path={`/projects/${props.projectId}/assets/${frame.asset_id}/versions/${frame.id}/image`}
          alt="视频生成首帧精确原图"
          onUnavailable={props.unavailable}
        />
      )}
      <label htmlFor={id + "-prompt"}>镜头视频提示词</label>
      <Textarea
        id={id + "-prompt"}
        value={prompt}
        maxLength={12000}
        disabled={disabled || frozen}
        onChange={(event) => setPrompt(event.target.value)}
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label>
          视频宽度
          <Input
            type="number"
            min={caps?.min_width ?? 1}
            max={caps?.max_width}
            step={caps?.dimension_multiple ?? 1}
            value={width}
            disabled={disabled || frozen}
            onChange={(event) => setWidth(Number(event.target.value))}
          />
        </label>
        <label>
          视频高度
          <Input
            type="number"
            min={caps?.min_height ?? 1}
            max={caps?.max_height}
            step={caps?.dimension_multiple ?? 1}
            value={height}
            disabled={disabled || frozen}
            onChange={(event) => setHeight(Number(event.target.value))}
          />
        </label>
        <label>
          视频时长（秒）
          <Input
            type="number"
            min={1}
            max={caps?.max_video_seconds ?? 30}
            step={1}
            value={seconds}
            disabled={disabled || frozen}
            onChange={(event) => setSeconds(Number(event.target.value))}
          />
        </label>
      </div>
      <p className="text-xs text-muted-foreground">
        首帧、分镜和当前镜头会按精确版本冻结。生成结果须另行完成视频时序审查后才能正式选用。
      </p>
      {!state.pending ? (
        <Button
          disabled={disabled || !config?.enabled || !frameId || !prompt.trim()}
          onClick={() =>
            void state.prepare({ frameId, prompt, width, height, seconds })
          }
        >
          估算此镜头视频费用
        </Button>
      ) : (
        <section
          aria-label="视频费用确认与提交结果"
          className="flex min-w-0 flex-col gap-2 break-all"
        >
          <p>
            冻结模型：{state.pending.request.spec.model} · 最高费用{" "}
            {state.pending.maximum_cost} USD
          </p>
          <p>
            精确首帧：
            {
              state.pending.request.inputs.find(
                (input) => input.dependency_type === "REFERENCE_DEPENDENCY",
              )?.source_asset_version_id
            }
          </p>
          <p className="whitespace-pre-wrap">
            冻结提示词：{state.pending.request.spec.prompt}
          </p>
          {state.pending.phase === "PREPARED" && (
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={disabled}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              我确认本次视频最高费用 {state.pending.maximum_cost} USD
            </label>
          )}
          {state.pending.phase !== "ACCEPTED" ? (
            <Button
              disabled={
                disabled || (state.pending.phase === "PREPARED" && !confirmed)
              }
              onClick={() => void state.submit()}
            >
              {state.pending.phase === "UNKNOWN"
                ? "核验原视频生成提交"
                : "确认费用并生成镜头视频"}
            </Button>
          ) : (
            <p role="status">
              视频任务已受理：{state.pending.job_id} ·{" "}
              {state.jobStatus || "正在核对"}
            </p>
          )}
          {state.pending.phase === "UNKNOWN" && (
            <Notice>
              提交结果待确认，继续时沿用同一任务标识、冻结请求和费用。
            </Notice>
          )}
          {(state.pending.phase === "PREPARED" ||
            ["SUCCEEDED", "FAILED", "CANCELLED"].includes(state.jobStatus)) && (
            <Button
              variant="outline"
              disabled={disabled}
              onClick={() => {
                state.reset();
                setConfirmed(false);
              }}
            >
              {state.pending.phase === "PREPARED"
                ? "重新编辑视频请求"
                : "准备另一个视频任务"}
            </Button>
          )}
        </section>
      )}
    </section>
  );
}

export default function VideoGenerationForm(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <Button
        variant="outline"
        disabled={!props.writable}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "收起镜头视频生成" : "为此镜头生成视频"}
      </Button>
      {open && <Fields {...props} />}
    </div>
  );
}
