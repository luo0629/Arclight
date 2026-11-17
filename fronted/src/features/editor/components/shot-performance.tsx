"use client";

import { useId } from "react";
import { Field, FieldGroup } from "@/components/ui/field";
import { FormLabel } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import type {
  ShotPerformance,
  PerformanceSourceLine,
} from "../shot-performance";

const characterFields = {
  objective: "目标",
  emotion_start: "起始情绪",
  emotion_end: "结束情绪",
  action_start: "起始动作",
  action_end: "结束动作",
  position_start: "起始位置",
  position_end: "结束位置",
  eyeline_start: "起始视线",
  eyeline_end: "结束视线",
} as const;
const motionLabels = {
  dynamic_required: "必须动态",
  static_allowed: "允许静态",
};
const presentationLabels = {
  visible: "可见对白",
  voice_over: "旁白",
  off_screen: "画外音",
};

function TextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <Field>
      <FormLabel htmlFor={id}>{label}</FormLabel>
      <Textarea
        id={id}
        value={value}
        maxLength={2000}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );
}

export default function ShotPerformanceDetails({
  performance,
  editing,
  lines,
  update,
}: {
  performance: ShotPerformance;
  editing: boolean;
  lines?: PerformanceSourceLine[];
  update: (performance: ShotPerformance) => void;
}) {
  const id = useId();
  function patch(change: Partial<ShotPerformance>) {
    update({ ...performance, ...change });
  }
  return (
    <section
      className="flex min-w-0 flex-col gap-4 break-words [overflow-wrap:anywhere]"
      aria-label="镜头表演计划"
    >
      <h5 className="font-medium">表演与接续计划</h5>
      <p className="text-sm font-medium">
        {motionLabels[performance.motion_requirement]} ·{" "}
        {performance.continuity_mode === "continuous"
          ? "连续接续"
          : "省略／跳切"}
      </p>
      {editing ? (
        <FieldGroup style={{ containerType: "normal" }}>
          <TextField
            label="场景目的"
            value={performance.scene_purpose}
            onChange={(value) => patch({ scene_purpose: value })}
          />
          <TextField
            label="镜头叙事目的"
            value={performance.narrative_purpose}
            onChange={(value) => patch({ narrative_purpose: value })}
          />
          <TextField
            label="情绪与节奏"
            value={performance.emotion_rhythm}
            onChange={(value) => patch({ emotion_rhythm: value })}
          />
          <Field>
            <FormLabel htmlFor={`${id}-motion`}>动态要求</FormLabel>
            <NativeSelect
              id={`${id}-motion`}
              value={performance.motion_requirement}
              onChange={(event) =>
                patch({
                  motion_requirement: event.target
                    .value as ShotPerformance["motion_requirement"],
                })
              }
            >
              <NativeSelectOption value="dynamic_required">
                必须动态
              </NativeSelectOption>
              <NativeSelectOption value="static_allowed">
                允许静态
              </NativeSelectOption>
            </NativeSelect>
            <small>
              必须动态的动作不能用静图运镜替代；能力不足须修订或交由人工决定。
            </small>
          </Field>
          {performance.characters.map((character, index) => (
            <FieldGroup
              key={character.character}
              className="rounded-lg border p-3"
            >
              <h6>{character.character} · 表演起止</h6>
              {(
                Object.entries(characterFields) as [
                  keyof typeof characterFields,
                  string,
                ][]
              ).map(([field, label]) => (
                <TextField
                  key={field}
                  label={`${character.character} · ${label}`}
                  value={character[field]}
                  onChange={(value) =>
                    patch({
                      characters: performance.characters.map((row, i) =>
                        i === index ? { ...row, [field]: value } : row,
                      ),
                    })
                  }
                />
              ))}
            </FieldGroup>
          ))}
          <TextField
            label="起始空间状态"
            value={performance.space_start}
            onChange={(value) => patch({ space_start: value })}
          />
          <TextField
            label="结束空间状态"
            value={performance.space_end}
            onChange={(value) => patch({ space_end: value })}
          />
          <Field>
            <FormLabel htmlFor={`${id}-from`}>接续来源镜头</FormLabel>
            <Input
              id={`${id}-from`}
              type="number"
              min={1}
              value={performance.continuity_from_shot ?? ""}
              onChange={(event) =>
                patch({
                  continuity_from_shot: event.target.value
                    ? Number(event.target.value)
                    : null,
                })
              }
            />
            <small>留空表示没有前镜接续；填写精确镜头编号。</small>
          </Field>
          <Field>
            <FormLabel htmlFor={`${id}-continuity`}>接续方式</FormLabel>
            <NativeSelect
              id={`${id}-continuity`}
              value={performance.continuity_mode}
              onChange={(event) =>
                patch({
                  continuity_mode: event.target
                    .value as ShotPerformance["continuity_mode"],
                })
              }
            >
              <NativeSelectOption value="continuous">
                连续接续
              </NativeSelectOption>
              <NativeSelectOption value="ellipsis">
                省略／跳切
              </NativeSelectOption>
            </NativeSelect>
          </Field>
          <TextField
            label="接续说明"
            value={performance.continuity_notes}
            onChange={(value) => patch({ continuity_notes: value })}
          />
          {performance.interactions.map((interaction, index) => (
            <FieldGroup key={index} className="rounded-lg border p-3">
              <h6>互动 {index + 1}</h6>
              {(["actor", "target"] as const).map((field) => (
                <Field key={field}>
                  <FormLabel htmlFor={`${id}-interaction-${index}-${field}`}>
                    {`互动 ${index + 1} · ${field === "actor" ? "行动者" : "反应者"}`}
                  </FormLabel>
                  <NativeSelect
                    id={`${id}-interaction-${index}-${field}`}
                    value={interaction[field]}
                    onChange={(event) =>
                      patch({
                        interactions: performance.interactions.map((row, i) =>
                          i === index
                            ? { ...row, [field]: event.target.value }
                            : row,
                        ),
                      })
                    }
                  >
                    <NativeSelectOption value="">选择人物</NativeSelectOption>
                    {performance.characters.map((character) => (
                      <NativeSelectOption
                        key={character.character}
                        value={character.character}
                      >
                        {character.character}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </Field>
              ))}
              {(["action", "reaction"] as const).map((field) => (
                <TextField
                  key={field}
                  label={`互动 ${index + 1} · ${field === "action" ? "行动" : "反应"}`}
                  value={interaction[field]}
                  onChange={(value) =>
                    patch({
                      interactions: performance.interactions.map((row, i) =>
                        i === index ? { ...row, [field]: value } : row,
                      ),
                    })
                  }
                />
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  patch({
                    interactions: performance.interactions.filter(
                      (_, i) => i !== index,
                    ),
                  })
                }
              >
                移除互动 {index + 1}
              </Button>
            </FieldGroup>
          ))}
          <Button
            type="button"
            variant="outline"
            disabled={
              performance.characters.length < 2 ||
              performance.interactions.length >= 30
            }
            onClick={() =>
              patch({
                interactions: [
                  ...performance.interactions,
                  {
                    actor: performance.characters[0].character,
                    target: performance.characters[1].character,
                    action: "",
                    reaction: "",
                  },
                ],
              })
            }
          >
            添加人物互动
          </Button>
          {performance.props.map((prop, index) => (
            <FieldGroup key={index} className="rounded-lg border p-3">
              <h6>接续道具 {index + 1}</h6>
              {(["asset_id", "asset_version_id"] as const).map((field) => (
                <Field key={field}>
                  <FormLabel htmlFor={`${id}-prop-${index}-${field}`}>
                    {`道具 ${index + 1} · ${field === "asset_id" ? "资产 ID" : "精确版本 ID"}`}
                  </FormLabel>
                  <Input
                    id={`${id}-prop-${index}-${field}`}
                    value={prop.asset[field]}
                    disabled={!lines}
                    onChange={(event) =>
                      patch({
                        props: performance.props.map((row, i) =>
                          i === index
                            ? {
                                ...row,
                                asset: {
                                  ...row.asset,
                                  [field]: event.target.value,
                                },
                              }
                            : row,
                        ),
                      })
                    }
                  />
                </Field>
              ))}
              {(
                [
                  "holder_start",
                  "holder_end",
                  "position_start",
                  "position_end",
                ] as const
              ).map((field) => (
                <TextField
                  key={field}
                  label={`道具 ${index + 1} · ${{ holder_start: "起始持有人", holder_end: "结束持有人", position_start: "起始位置", position_end: "结束位置" }[field]}`}
                  value={prop[field]}
                  onChange={(value) =>
                    patch({
                      props: performance.props.map((row, i) =>
                        i === index ? { ...row, [field]: value } : row,
                      ),
                    })
                  }
                />
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  patch({
                    props: performance.props.filter((_, i) => i !== index),
                  })
                }
              >
                移除接续道具 {index + 1}
              </Button>
            </FieldGroup>
          ))}
          <Button
            type="button"
            variant="outline"
            disabled={!lines || performance.props.length >= 50}
            onClick={() =>
              patch({
                props: [
                  ...performance.props,
                  {
                    asset: { asset_id: "", asset_version_id: "" },
                    holder_start: "",
                    holder_end: "",
                    position_start: "",
                    position_end: "",
                  },
                ],
              })
            }
          >
            添加接续道具
          </Button>
          {performance.dialogue.map((line, index) => {
            const source = lines?.find((row) => row.line_id === line.line_id);
            return (
              <FieldGroup key={line.line_id} className="rounded-lg border p-3">
                <h6>
                  台词 {index + 1} · {line.speaker}
                </h6>
                <p>{line.text}</p>
                <small className="break-all">精确台词：{line.line_id}</small>
                <Field>
                  <FormLabel htmlFor={`${id}-dialogue-${index}`}>
                    {`台词 ${index + 1} · 呈现方式`}
                  </FormLabel>
                  <NativeSelect
                    id={`${id}-dialogue-${index}`}
                    value={line.presentation}
                    disabled={!source}
                    onChange={(event) => {
                      const presentation = event.target
                        .value as typeof line.presentation;
                      patch({
                        dialogue: performance.dialogue.map((row, i) =>
                          i === index
                            ? {
                                ...row,
                                presentation,
                                lipsync:
                                  presentation === "visible"
                                    ? "required"
                                    : "not_applicable",
                                exemption_reason:
                                  presentation === "visible"
                                    ? ""
                                    : row.exemption_reason,
                              }
                            : row,
                        ),
                      });
                    }}
                  >
                    {source?.line_type === "narration" ? (
                      <NativeSelectOption value="voice_over">
                        旁白
                      </NativeSelectOption>
                    ) : (
                      <>
                        <NativeSelectOption value="visible">
                          可见对白
                        </NativeSelectOption>
                        <NativeSelectOption value="off_screen">
                          画外音
                        </NativeSelectOption>
                      </>
                    )}
                  </NativeSelect>
                </Field>
                <p>
                  {line.lipsync === "required"
                    ? "口型：必须同步；请明确选择必须动态以承载说话动作。"
                    : "口型：不适用"}
                </p>
                {(
                  [
                    "emotion",
                    "pacing",
                    ...(line.lipsync === "not_applicable"
                      ? (["exemption_reason"] as const)
                      : []),
                  ] as const
                ).map((field) => (
                  <TextField
                    key={field}
                    label={`台词 ${index + 1} · ${{ emotion: "情绪", pacing: "节奏", exemption_reason: "口型豁免理由" }[field]}`}
                    value={line[field]}
                    onChange={(value) =>
                      patch({
                        dialogue: performance.dialogue.map((row, i) =>
                          i === index ? { ...row, [field]: value } : row,
                        ),
                      })
                    }
                  />
                ))}
                {!source && (
                  <small>
                    精确来源剧本尚未加载，台词文本和呈现方式保持固定。
                  </small>
                )}
              </FieldGroup>
            );
          })}
        </FieldGroup>
      ) : (
        <div className="flex min-w-0 flex-col gap-3 text-sm">
          <p>场景目的：{performance.scene_purpose}</p>
          <p>叙事目的：{performance.narrative_purpose}</p>
          <p>情绪与节奏：{performance.emotion_rhythm}</p>
          {performance.characters.map((character) => (
            <div key={character.character}>
              <strong>
                {character.character} · {character.objective}
              </strong>
              <p>
                情绪：{character.emotion_start} → {character.emotion_end}
              </p>
              <p>
                动作：{character.action_start} → {character.action_end}
              </p>
              <p>
                位置：{character.position_start} → {character.position_end}
              </p>
              <p>
                视线：{character.eyeline_start} → {character.eyeline_end}
              </p>
            </div>
          ))}
          {performance.interactions.map((interaction, i) => (
            <p key={i}>
              互动：{interaction.actor} → {interaction.target} ·{" "}
              {interaction.action}；反应：{interaction.reaction}
            </p>
          ))}
          <p>
            空间：{performance.space_start} → {performance.space_end}
          </p>
          <p>
            接续来源：
            {performance.continuity_from_shot === null
              ? "无"
              : `镜头 ${performance.continuity_from_shot}`}{" "}
            · {performance.continuity_notes}
          </p>
          {performance.props.map((prop, i) => (
            <p key={i}>
              道具 {prop.asset.asset_id} · 精确版本{" "}
              {prop.asset.asset_version_id}：{prop.holder_start}／
              {prop.position_start} → {prop.holder_end}／{prop.position_end}
            </p>
          ))}
          {performance.dialogue.map((line) => (
            <div key={line.line_id}>
              <p>
                {presentationLabels[line.presentation]} ·{" "}
                {line.lipsync === "required"
                  ? "口型：必须同步"
                  : "口型：不适用"}
              </p>
              <p>
                {line.speaker}：{line.text}
              </p>
              <p>
                情绪：{line.emotion} · 节奏：{line.pacing}
              </p>
              {line.exemption_reason && (
                <p>豁免理由：{line.exemption_reason}</p>
              )}
              <small className="break-all">台词 ID：{line.line_id}</small>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
