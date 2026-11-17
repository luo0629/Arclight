"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { NativeSelect } from "@/components/ui/native-select";
import { FormLabel, Notice } from "@/components/ui-kit";
import type { StoryEntry } from "@/types/api";
import {
  ledgerKinds,
  ledgerStatuses,
  sceneLabel,
  type LedgerContent,
  type LedgerEntry,
  type LedgerKind,
  type LedgerSource,
  type LedgerStatus,
} from "../story-ledger";

export default function StoryLedgerEditor({
  content,
  sources,
  entities,
  disabled,
  onChange,
}: {
  content: LedgerContent;
  sources: LedgerSource[];
  entities: StoryEntry[];
  disabled: boolean;
  onChange: (content: LedgerContent) => void;
}) {
  const [sourceId, setSourceId] = useState("");
  const [sceneId, setSceneId] = useState("");
  const [storyTime, setStoryTime] = useState(0);
  const [segment, setSegment] = useState(1);
  const [error, setError] = useState("");
  const source = sources.find((row) => row.script_version_id === sourceId);
  const characters = entities.filter(
    (row) => row.kind === "CHARACTER" && !row.archived,
  );
  const active = entities.filter((row) => !row.archived);
  function subjects(kind: LedgerKind) {
    return active.filter(
      (row) =>
        kind === "EVENT" ||
        (kind === "PROP_OWNERSHIP"
          ? row.kind === "PROP"
          : row.kind === "CHARACTER"),
    );
  }
  function targets(kind: LedgerKind) {
    return active.filter(
      (row) =>
        !["RELATIONSHIP", "PROP_OWNERSHIP"].includes(kind) ||
        row.kind === "CHARACTER",
    );
  }
  function updateEntry(index: number, patch: Partial<LedgerEntry>) {
    onChange({
      ...content,
      entries: content.entries.map((row, i) =>
        i === index ? { ...row, ...patch } : row,
      ),
    });
  }
  function entityName(id: string) {
    return entities.find((row) => row.id === id)?.name ?? id;
  }
  return (
    <fieldset disabled={disabled} className="grid min-w-0 gap-5">
      <section className="grid min-w-0 gap-3" aria-label="故事时序映射">
        <h4 className="font-semibold">故事时序与播放位置</h4>
        <p className="max-w-prose text-sm text-muted-foreground">
          故事时点表示事件实际发生的先后；集、场与片段表示观众看到的位置。倒叙、插叙请分别添加映射。同一场有多个时点时使用不同片段号。
        </p>
        {content.scene_times.map((scene) => (
          <div
            key={scene.id}
            className="grid min-w-0 gap-2 border-b pb-3 sm:grid-cols-[1fr_auto]"
          >
            <p className="break-words text-sm">{sceneLabel(scene)}</p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                if (
                  content.entries.some(
                    (entry) =>
                      entry.disclosure_start === scene.id ||
                      entry.disclosure_end === scene.id ||
                      (entry.source_script_version_id ===
                        scene.script_version_id &&
                        entry.source_scene_id === scene.scene_id),
                  )
                ) {
                  setError(
                    "此映射仍被条目引用，请先修改条目的披露位置和来源。",
                  );
                  return;
                }
                setError("");
                onChange({
                  ...content,
                  scene_times: content.scene_times.filter(
                    (row) => row.id !== scene.id,
                  ),
                });
              }}
            >
              移除时序映射
            </Button>
          </div>
        ))}
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <FormLabel>
            映射来源剧本
            <NativeSelect
              className="w-full"
              aria-label="映射来源剧本"
              value={sourceId}
              onChange={(event) => {
                setSourceId(event.target.value);
                setSceneId("");
              }}
            >
              <option value="">选择已加载的批准版本</option>
              {sources.map((row) => (
                <option
                  key={row.script_version_id}
                  value={row.script_version_id}
                >
                  第 {row.episode_no} 集 · 剧本版本 {row.version_no}
                </option>
              ))}
            </NativeSelect>
          </FormLabel>
          <FormLabel>
            映射来源场景
            <NativeSelect
              className="w-full"
              aria-label="映射来源场景"
              value={sceneId}
              onChange={(event) => setSceneId(event.target.value)}
            >
              <option value="">选择场景（含无对白场景）</option>
              {source?.scenes.map((scene) => (
                <option key={scene.scene_id} value={scene.scene_id}>
                  第 {scene.scene_no} 场 · {scene.title}
                </option>
              ))}
            </NativeSelect>
          </FormLabel>
          <FormLabel>
            故事时点
            <Input
              aria-label="故事时点"
              type="number"
              step={1}
              value={storyTime}
              onChange={(event) => setStoryTime(Number(event.target.value))}
            />
          </FormLabel>
          <FormLabel>
            片段号
            <Input
              aria-label="片段号"
              type="number"
              min={1}
              step={1}
              value={segment}
              onChange={(event) => setSegment(Number(event.target.value))}
            />
          </FormLabel>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <Button
          type="button"
          variant="outline"
          className="justify-self-start"
          disabled={!source || !sceneId}
          onClick={() => {
            const scene = source?.scenes.find(
              (row) => row.scene_id === sceneId,
            );
            if (
              !source ||
              !scene ||
              !Number.isInteger(storyTime) ||
              !Number.isInteger(segment) ||
              segment < 1
            ) {
              setError("故事时点须为整数，片段号须为大于零的整数。");
              return;
            }
            if (
              content.scene_times.some(
                (row) =>
                  row.episode_no === source.episode_no &&
                  row.scene_no === scene.scene_no &&
                  row.segment_no === segment,
              )
            ) {
              setError("该集、场、片段已有映射，请使用其他片段号。");
              return;
            }
            setError("");
            onChange({
              ...content,
              scene_times: [
                ...content.scene_times,
                {
                  id: crypto.randomUUID(),
                  episode_id: source.episode_id,
                  episode_no: source.episode_no,
                  script_version_id: source.script_version_id,
                  scene_id: scene.scene_id,
                  scene_no: scene.scene_no,
                  segment_no: segment,
                  story_time: storyTime,
                },
              ],
            });
          }}
        >
          添加时序映射
        </Button>
      </section>

      <section className="grid min-w-0 gap-4" aria-label="剧情账本条目编辑">
        <h4 className="font-semibold">事件与状态条目</h4>
        {!content.entries.length && (
          <p className="text-sm text-muted-foreground">
            先添加时序映射，再记录事件、关系、知情、伤势、服装或道具归属。
          </p>
        )}
        {content.entries.map((entry, index) => (
          <fieldset
            key={entry.id}
            className="grid min-w-0 gap-3 border-t pt-4"
            aria-label={`账本条目 ${index + 1}`}
          >
            <legend className="text-sm font-semibold">
              {index + 1}. {ledgerKinds[entry.kind]}
            </legend>
            <div className="grid min-w-0 gap-3 sm:grid-cols-2">
              <FormLabel>
                条目类型
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 类型`}
                  value={entry.kind}
                  onChange={(event) =>
                    updateEntry(index, {
                      kind: event.target.value as LedgerKind,
                      subject_id: null,
                      target_id: null,
                    })
                  }
                >
                  {Object.entries(ledgerKinds).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                记录性质
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 性质`}
                  value={entry.status}
                  onChange={(event) =>
                    updateEntry(index, {
                      status: event.target.value as LedgerStatus,
                    })
                  }
                >
                  {Object.entries(ledgerStatuses).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                主体设定
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 主体`}
                  value={entry.subject_id ?? ""}
                  required={entry.kind !== "EVENT"}
                  onChange={(event) =>
                    updateEntry(index, {
                      subject_id: event.target.value || null,
                    })
                  }
                >
                  <option value="">选择活动设定</option>
                  {entry.subject_id &&
                    !active.some((row) => row.id === entry.subject_id) && (
                      <option value={entry.subject_id}>
                        {entityName(entry.subject_id)}（历史引用）
                      </option>
                    )}
                  {subjects(entry.kind).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                关联目标（可选）
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 目标`}
                  value={entry.target_id ?? ""}
                  onChange={(event) =>
                    updateEntry(index, {
                      target_id: event.target.value || null,
                    })
                  }
                >
                  <option value="">无关联目标</option>
                  {entry.target_id &&
                    !active.some((row) => row.id === entry.target_id) && (
                      <option value={entry.target_id}>
                        {entityName(entry.target_id)}（历史引用）
                      </option>
                    )}
                  {targets(entry.kind).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                状态名称
                <Input
                  aria-label={`条目 ${index + 1} 状态名称`}
                  value={entry.state_key}
                  required
                  maxLength={100}
                  placeholder="例如：左臂伤势、信件归属、门锁损坏"
                  onChange={(event) =>
                    updateEntry(index, { state_key: event.target.value })
                  }
                />
              </FormLabel>
              <FormLabel>
                状态或事件内容
                <Textarea
                  aria-label={`条目 ${index + 1} 内容`}
                  value={entry.value}
                  required
                  maxLength={4000}
                  placeholder="描述具体发生了什么，或此时处于什么状态"
                  onChange={(event) =>
                    updateEntry(index, { value: event.target.value })
                  }
                />
              </FormLabel>
              <FormLabel>
                实际生效时点
                <Input
                  aria-label={`条目 ${index + 1} 生效时点`}
                  type="number"
                  step={1}
                  value={entry.story_start}
                  onChange={(event) =>
                    updateEntry(index, {
                      story_start: Number(event.target.value),
                    })
                  }
                />
              </FormLabel>
              <FormLabel>
                实际结束时点（不含，留空持续）
                <Input
                  aria-label={`条目 ${index + 1} 结束时点`}
                  type="number"
                  step={1}
                  value={entry.story_end ?? ""}
                  onChange={(event) =>
                    updateEntry(index, {
                      story_end:
                        event.target.value === ""
                          ? null
                          : Number(event.target.value),
                    })
                  }
                />
              </FormLabel>
              <FormLabel>
                首次披露位置
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 披露起点`}
                  value={entry.disclosure_start}
                  required
                  onChange={(event) =>
                    updateEntry(index, { disclosure_start: event.target.value })
                  }
                >
                  <option value="">选择映射</option>
                  {content.scene_times.map((scene) => (
                    <option key={scene.id} value={scene.id}>
                      {sceneLabel(scene)}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
              <FormLabel>
                披露结束位置（不含，可选）
                <NativeSelect
                  className="w-full"
                  aria-label={`条目 ${index + 1} 披露终点`}
                  value={entry.disclosure_end ?? ""}
                  onChange={(event) =>
                    updateEntry(index, {
                      disclosure_end: event.target.value || null,
                    })
                  }
                >
                  <option value="">持续有效</option>
                  {content.scene_times.map((scene) => (
                    <option key={scene.id} value={scene.id}>
                      {sceneLabel(scene)}
                    </option>
                  ))}
                </NativeSelect>
              </FormLabel>
            </div>
            <FormLabel>
              精确来源（待核实与确认条目必填）
              <NativeSelect
                className="w-full"
                aria-label={`条目 ${index + 1} 精确来源`}
                value={
                  content.scene_times.find(
                    (scene) =>
                      scene.script_version_id ===
                        entry.source_script_version_id &&
                      scene.scene_id === entry.source_scene_id,
                  )?.id ?? ""
                }
                required={entry.status !== "AUTHOR_PLANNED"}
                onChange={(event) => {
                  const scene = content.scene_times.find(
                    (row) => row.id === event.target.value,
                  );
                  updateEntry(index, {
                    source_script_version_id: scene?.script_version_id ?? null,
                    source_scene_id: scene?.scene_id ?? null,
                  });
                }}
              >
                <option value="">作者计划可暂不关联来源</option>
                {content.scene_times
                  .filter(
                    (scene, i, rows) =>
                      rows.findIndex(
                        (row) =>
                          row.script_version_id === scene.script_version_id &&
                          row.scene_id === scene.scene_id,
                      ) === i,
                  )
                  .map((scene) => (
                    <option key={scene.id} value={scene.id}>
                      {sceneLabel(scene)}
                    </option>
                  ))}
              </NativeSelect>
            </FormLabel>
            <details className="studio-disclosure">
              <summary>角色知情与事件因果</summary>
              <div className="grid min-w-0 gap-3 py-3">
                {entry.known_by.map((knowledge, knowledgeIndex) => (
                  <div
                    key={knowledgeIndex}
                    className="grid min-w-0 gap-2 sm:grid-cols-2"
                  >
                    <FormLabel>
                      知情角色
                      <NativeSelect
                        className="w-full"
                        aria-label={`条目 ${index + 1} 知情角色 ${knowledgeIndex + 1}`}
                        value={knowledge.character_id}
                        required
                        onChange={(event) =>
                          updateEntry(index, {
                            known_by: entry.known_by.map((row, i) =>
                              i === knowledgeIndex
                                ? { ...row, character_id: event.target.value }
                                : row,
                            ),
                          })
                        }
                      >
                        <option value="">选择角色</option>
                        {!characters.some(
                          (row) => row.id === knowledge.character_id,
                        ) &&
                          knowledge.character_id && (
                            <option value={knowledge.character_id}>
                              {entityName(knowledge.character_id)}（历史引用）
                            </option>
                          )}
                        {characters.map((row) => (
                          <option key={row.id} value={row.id}>
                            {row.name}
                          </option>
                        ))}
                      </NativeSelect>
                    </FormLabel>
                    <FormLabel>
                      开始知情时点
                      <Input
                        type="number"
                        step={1}
                        aria-label={`条目 ${index + 1} 知情起点 ${knowledgeIndex + 1}`}
                        value={knowledge.story_start}
                        onChange={(event) =>
                          updateEntry(index, {
                            known_by: entry.known_by.map((row, i) =>
                              i === knowledgeIndex
                                ? {
                                    ...row,
                                    story_start: Number(event.target.value),
                                  }
                                : row,
                            ),
                          })
                        }
                      />
                    </FormLabel>
                    <FormLabel>
                      停止知情时点（不含，可选）
                      <Input
                        type="number"
                        step={1}
                        aria-label={`条目 ${index + 1} 知情终点 ${knowledgeIndex + 1}`}
                        value={knowledge.story_end ?? ""}
                        onChange={(event) =>
                          updateEntry(index, {
                            known_by: entry.known_by.map((row, i) =>
                              i === knowledgeIndex
                                ? {
                                    ...row,
                                    story_end:
                                      event.target.value === ""
                                        ? null
                                        : Number(event.target.value),
                                  }
                                : row,
                            ),
                          })
                        }
                      />
                    </FormLabel>
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() =>
                        updateEntry(index, {
                          known_by: entry.known_by.filter(
                            (_, i) => i !== knowledgeIndex,
                          ),
                        })
                      }
                    >
                      移除此知情范围
                    </Button>
                  </div>
                ))}
                <Button
                  type="button"
                  variant="outline"
                  className="justify-self-start"
                  onClick={() =>
                    updateEntry(index, {
                      known_by: [
                        ...entry.known_by,
                        {
                          character_id: "",
                          story_start: entry.story_start,
                          story_end: null,
                        },
                      ],
                    })
                  }
                >
                  添加知情角色
                </Button>
                <p className="text-sm font-medium">前因事件</p>
                {content.entries
                  .filter((row) => row.id !== entry.id && row.kind === "EVENT")
                  .map((cause) => (
                    <label
                      key={cause.id}
                      className="flex items-start gap-2 text-sm"
                    >
                      <input
                        type="checkbox"
                        checked={entry.causes.includes(cause.id)}
                        onChange={(event) =>
                          updateEntry(index, {
                            causes: event.target.checked
                              ? [...entry.causes, cause.id]
                              : entry.causes.filter((id) => id !== cause.id),
                          })
                        }
                      />
                      <span className="min-w-0 break-words">
                        {cause.state_key || "未命名事件"}：{cause.value}
                      </span>
                    </label>
                  ))}
                {!content.entries.some(
                  (row) => row.id !== entry.id && row.kind === "EVENT",
                ) && (
                  <p className="text-sm text-muted-foreground">
                    添加另一剧情事件后可关联前因。
                  </p>
                )}
              </div>
            </details>
            <Button
              type="button"
              variant="ghost"
              className="justify-self-start"
              onClick={() => {
                if (
                  content.entries.some((row) => row.causes.includes(entry.id))
                ) {
                  setError("其他条目仍引用此事件为前因，请先解除因果关联。");
                  return;
                }
                onChange({
                  ...content,
                  entries: content.entries.filter((row) => row.id !== entry.id),
                });
              }}
            >
              移除条目 {index + 1}
            </Button>
          </fieldset>
        ))}
        <Button
          type="button"
          variant="outline"
          className="justify-self-start"
          disabled={!content.scene_times.length}
          onClick={() => {
            const scene = content.scene_times[0];
            onChange({
              ...content,
              entries: [
                ...content.entries,
                {
                  id: crypto.randomUUID(),
                  kind: "EVENT",
                  status: "AUTHOR_PLANNED",
                  subject_id: null,
                  target_id: null,
                  state_key: "",
                  value: "",
                  story_start: scene.story_time,
                  story_end: null,
                  disclosure_start: scene.id,
                  disclosure_end: null,
                  source_script_version_id: null,
                  source_scene_id: null,
                  known_by: [],
                  causes: [],
                },
              ],
            });
          }}
        >
          添加账本条目
        </Button>
      </section>
    </fieldset>
  );
}
