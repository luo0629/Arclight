"use client";

import { useId, useState } from "react";
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { FormLabel, Notice } from "@/components/ui-kit";
import type { ShotExtensions } from "./shot-details";
import { reorderPerformanceShots } from "../shot-performance";

export type StoryboardTableShot = ShotExtensions & {
  scene_asset?: unknown;
  location_asset?: unknown;
  props?: unknown[];
  shot_no: number;
  scene_no: number;
  duration_ms: number;
  shot_size: string;
  visual_description: string;
  characters: string[];
  camera_movement: string;
  subtitle: string;
};

const sizes = { wide: "远景", full: "全景", medium: "中景", close_up: "特写" };
const movements = {
  static: "固定",
  push_in: "推近",
  pull_out: "拉远",
  pan: "摇镜",
};
const transitions = {
  cut: "切换",
  fade: "淡入淡出",
  dissolve: "叠化",
  none: "无",
};
const fields = {
  shot_size: "景别",
  camera_movement: "运镜",
  duration_ms: "时长（秒）",
  transition_out: "转场",
  visual_description: "画面描述",
  subtitle: "字幕",
  notes: "备注",
};
type BulkField = keyof typeof fields;
const optionsFor = (field: BulkField): Record<string, string> | undefined =>
  field === "shot_size"
    ? sizes
    : field === "camera_movement"
      ? movements
      : field === "transition_out"
        ? transitions
        : undefined;

function Options({ options }: { options: Record<string, string> }) {
  return Object.entries(options).map(([value, label]) => (
    <NativeSelectOption key={value} value={value}>
      {label}
    </NativeSelectOption>
  ));
}

export default function StoryboardTable({
  shots,
  editable,
  onChange,
}: {
  shots: StoryboardTableShot[];
  editable: boolean;
  onChange: (shots: StoryboardTableShot[]) => void;
}) {
  const id = useId();
  // These identities belong only to this editing view, never to the saved contract.
  const [keys, setKeys] = useState<string[]>(() =>
    shots.map(() => crypto.randomUUID()),
  );
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [dragged, setDragged] = useState<string | null>(null);
  const [field, setField] = useState<BulkField>("shot_size");
  const [value, setValue] = useState("medium");
  const [announcement, setAnnouncement] = useState("");
  const options = optionsFor(field);
  const selectedCount = keys.filter((key) => selected.has(key)).length;
  const invalid =
    field === "duration_ms"
      ? !value.trim() ||
        !Number.isFinite(Number(value)) ||
        Number(value) < 0.5 ||
        Number(value) > 30
      : field === "visual_description"
        ? !value.trim()
        : false;

  function patch(index: number, change: Partial<StoryboardTableShot>) {
    if (!editable) return;
    onChange(
      shots.map((shot, i) => (i === index ? { ...shot, ...change } : shot)),
    );
  }

  function move(from: number, to: number) {
    if (
      !editable ||
      from < 0 ||
      to < 0 ||
      from >= shots.length ||
      to >= shots.length ||
      from === to
    )
      return;
    const ordered = [...shots];
    const orderedKeys = [...keys];
    ordered.splice(to, 0, ordered.splice(from, 1)[0]);
    orderedKeys.splice(to, 0, orderedKeys.splice(from, 1)[0]);
    setKeys(orderedKeys);
    onChange(reorderPerformanceShots(ordered));
    setAnnouncement(`镜头 ${from + 1} 已移至第 ${to + 1} 位，序号已更新。`);
  }

  function apply() {
    if (!editable || !selectedCount || invalid) return;
    const next =
      field === "duration_ms" ? Math.round(Number(value) * 1000) : value;
    onChange(
      shots.map((shot, index) =>
        selected.has(keys[index]) ? { ...shot, [field]: next } : shot,
      ),
    );
    setAnnouncement(
      `已修改 ${selectedCount} 个镜头的${fields[field]}，请保存为新版本。`,
    );
  }

  return (
    <section
      className="flex min-w-0 flex-col gap-3"
      aria-label="分镜表格工作区"
      data-storyboard-table=""
    >
      <p className="text-sm text-muted-foreground">
        共 {shots.length}{" "}
        个镜头。修改暂存为草稿，另存后等待审核；顺序变更在批准后更新受影响的时间轴与成片。
      </p>
      {editable && (
        <fieldset className="flex min-w-0 flex-col gap-3 rounded-lg border bg-secondary/30 p-3">
          <legend className="px-1 text-sm font-medium">
            批量编辑 · 已选择 {selectedCount} 个镜头
          </legend>
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <FormLabel htmlFor={`${id}-field`}>
              批量修改字段
              <NativeSelect
                id={`${id}-field`}
                aria-label="批量修改字段"
                value={field}
                onChange={(event) => {
                  const next = event.target.value as BulkField;
                  setField(next);
                  setValue(
                    Object.keys(optionsFor(next) ?? {})[0] ??
                      (next === "duration_ms" ? "3" : ""),
                  );
                }}
              >
                <Options options={fields} />
              </NativeSelect>
            </FormLabel>
            <FormLabel htmlFor={`${id}-value`}>
              批量修改值
              {options ? (
                <NativeSelect
                  id={`${id}-value`}
                  aria-label="批量修改值"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                >
                  <Options options={options} />
                </NativeSelect>
              ) : field === "duration_ms" ? (
                <Input
                  id={`${id}-value`}
                  aria-label="批量修改值"
                  type="number"
                  min="0.5"
                  max="30"
                  step="0.001"
                  value={value}
                  aria-invalid={invalid}
                  onChange={(event) => setValue(event.target.value)}
                />
              ) : (
                <Textarea
                  id={`${id}-value`}
                  aria-label="批量修改值"
                  value={value}
                  maxLength={field === "visual_description" ? 3000 : 2000}
                  aria-invalid={invalid}
                  onChange={(event) => setValue(event.target.value)}
                />
              )}
            </FormLabel>
          </div>
          {invalid && (
            <Notice tone="error">
              {field === "duration_ms"
                ? "时长须在 0.5–30 秒之间。"
                : "画面描述不能为空。"}
            </Notice>
          )}
          <Button
            variant="outline"
            disabled={!selectedCount || invalid}
            onClick={apply}
          >
            应用到 {selectedCount} 个镜头
          </Button>
        </fieldset>
      )}
      <p
        className="text-sm text-muted-foreground"
        role="status"
        aria-live="polite"
      >
        {announcement}
      </p>
      <div
        className="max-w-full overflow-x-auto rounded-lg border focus-visible:outline-2 focus-visible:outline-ring"
        role="region"
        aria-label="分镜表格滚动区域"
        tabIndex={0}
      >
        <table
          className="w-full min-w-[52rem] text-left text-sm"
          aria-label="分镜镜头表"
        >
          <thead className="border-b bg-secondary/50">
            <tr className="[&>th]:p-3 [&>th]:font-medium">
              {editable && (
                <th scope="col">
                  <label className="flex size-11 cursor-pointer items-center justify-center">
                    {" "}
                    <Input
                      type="checkbox"
                      className="h-5 w-5"
                      aria-label="选择全部镜头"
                      checked={selectedCount === shots.length}
                      ref={(element) => {
                        if (element)
                          element.indeterminate =
                            selectedCount > 0 && selectedCount < shots.length;
                      }}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked ? new Set(keys) : new Set(),
                        )
                      }
                    />
                  </label>
                </th>
              )}
              <th scope="col">镜头／场景</th>
              {editable && <th scope="col">调整顺序</th>}
              <th scope="col">画面描述</th>
              <th scope="col">时长（秒）</th>
              <th scope="col">景别／运镜</th>
              <th scope="col">字幕</th>
            </tr>
          </thead>
          <tbody>
            {shots.map((shot, index) => (
              <tr
                key={keys[index]}
                className={`border-b align-top last:border-0 [&>td]:p-3 [&>th]:p-3 ${selected.has(keys[index]) && editable ? "bg-accent/15" : ""}`}
                data-shot-number={shot.shot_no}
                onDragOver={(event) => {
                  if (editable && dragged) {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                  }
                }}
                onDrop={(event) => {
                  if (
                    !editable ||
                    !dragged ||
                    event.dataTransfer.getData("text/plain") !== dragged
                  )
                    return;
                  event.preventDefault();
                  move(keys.indexOf(dragged), index);
                  setDragged(null);
                }}
              >
                {editable && (
                  <td>
                    <label className="flex size-11 cursor-pointer items-center justify-center">
                      {" "}
                      <Input
                        type="checkbox"
                        className="h-5 w-5"
                        aria-label={`选择镜头 ${shot.shot_no}`}
                        checked={selected.has(keys[index])}
                        onChange={(event) =>
                          setSelected((previous) => {
                            const next = new Set(previous);
                            if (event.target.checked) next.add(keys[index]);
                            else next.delete(keys[index]);
                            return next;
                          })
                        }
                      />
                    </label>
                  </td>
                )}
                <th scope="row" className="min-w-24 font-medium">
                  镜头 {String(shot.shot_no).padStart(2, "0")}
                  <span className="mt-1 block font-normal text-muted-foreground">
                    场景 {shot.scene_no}
                  </span>
                  <span className="mt-1 block font-normal text-muted-foreground">
                    {shot.characters.join("、")}
                  </span>
                </th>
                {editable && (
                  <td>
                    <div className="flex flex-col items-start gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-11"
                        draggable={editable}
                        aria-label={`拖动镜头 ${shot.shot_no}`}
                        title="拖动到目标镜头，也可使用上移或下移"
                        onDragStart={(event) => {
                          if (!editable) {
                            event.preventDefault();
                            return;
                          }
                          setDragged(keys[index]);
                          event.dataTransfer.effectAllowed = "move";
                          event.dataTransfer.setData("text/plain", keys[index]);
                        }}
                        onDragEnd={() => setDragged(null)}
                      >
                        <GripVertical aria-hidden="true" />
                      </Button>
                      <Button
                        variant="outline"
                        size="icon"
                        className="size-11"
                        aria-label={`上移镜头 ${shot.shot_no}`}
                        disabled={index === 0}
                        onClick={() => move(index, index - 1)}
                      >
                        <ArrowUp aria-hidden="true" />
                      </Button>
                      <Button
                        variant="outline"
                        size="icon"
                        className="size-11"
                        aria-label={`下移镜头 ${shot.shot_no}`}
                        disabled={index === shots.length - 1}
                        onClick={() => move(index, index + 1)}
                      >
                        <ArrowDown aria-hidden="true" />
                      </Button>
                    </div>
                  </td>
                )}
                <td className="min-w-56">
                  {editable ? (
                    <Textarea
                      aria-label={`镜头 ${shot.shot_no} 画面描述`}
                      value={shot.visual_description}
                      maxLength={3000}
                      aria-invalid={!shot.visual_description.trim()}
                      onChange={(event) =>
                        patch(index, { visual_description: event.target.value })
                      }
                    />
                  ) : (
                    shot.visual_description
                  )}
                  {shot.performance && (
                    <div className="mt-2 text-xs text-muted-foreground">
                      <p>
                        {shot.performance.motion_requirement ===
                        "dynamic_required"
                          ? "必须动态"
                          : "允许静态"}{" "}
                        · 接续
                        {shot.performance.continuity_from_shot === null
                          ? "无"
                          : `镜头 ${shot.performance.continuity_from_shot}`}
                      </p>
                      <p>
                        {shot.performance.dialogue
                          .map((line) =>
                            line.presentation === "visible"
                              ? "可见对白／需口型"
                              : line.presentation === "voice_over"
                                ? "旁白／不适用口型"
                                : line.presentation === "obscured"
                                  ? "口部遮挡／不适用口型"
                                  : "画外音／不适用口型",
                          )
                          .join("、")}
                      </p>
                    </div>
                  )}
                </td>
                <td className="min-w-28">
                  {editable ? (
                    <Input
                      type="number"
                      min="0.5"
                      max="30"
                      step="0.001"
                      aria-label={`镜头 ${shot.shot_no} 时长（秒）`}
                      value={shot.duration_ms / 1000}
                      aria-invalid={
                        shot.duration_ms < 500 || shot.duration_ms > 30000
                      }
                      onChange={(event) =>
                        patch(index, {
                          duration_ms: Math.round(
                            Number(event.target.value) * 1000,
                          ),
                        })
                      }
                    />
                  ) : (
                    shot.duration_ms / 1000
                  )}
                </td>
                <td className="min-w-32">
                  {editable ? (
                    <div className="flex flex-col gap-2">
                      <NativeSelect
                        aria-label={`镜头 ${shot.shot_no} 景别`}
                        value={shot.shot_size}
                        onChange={(event) =>
                          patch(index, { shot_size: event.target.value })
                        }
                      >
                        <Options options={sizes} />
                      </NativeSelect>
                      <NativeSelect
                        aria-label={`镜头 ${shot.shot_no} 运镜`}
                        value={shot.camera_movement}
                        onChange={(event) =>
                          patch(index, { camera_movement: event.target.value })
                        }
                      >
                        <Options options={movements} />
                      </NativeSelect>
                    </div>
                  ) : (
                    <>
                      {sizes[shot.shot_size as keyof typeof sizes] ??
                        shot.shot_size}
                      <br />
                      {movements[
                        shot.camera_movement as keyof typeof movements
                      ] ?? shot.camera_movement}
                    </>
                  )}
                </td>
                <td className="min-w-48">
                  {editable ? (
                    <Textarea
                      aria-label={`镜头 ${shot.shot_no} 字幕`}
                      value={shot.subtitle}
                      maxLength={2000}
                      onChange={(event) =>
                        patch(index, { subtitle: event.target.value })
                      }
                    />
                  ) : (
                    shot.subtitle || "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
