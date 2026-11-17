"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FormLabel, Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";
import type { ContentVersion, StoryEntry } from "@/types/api";
import { ledgerKinds, type LedgerKind } from "@/features/story/story-ledger";
import {
  emptyBeat,
  type NarrativeScene,
  type StateChange,
  type StoryStateReceipt,
} from "../script-narrative";

function TextField({
  label,
  value,
  onChange,
  numeric = false,
}: {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  numeric?: boolean;
}) {
  return (
    <FormLabel>
      {label}
      {numeric ? (
        <Input
          type="number"
          step="1"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Textarea
          value={value}
          maxLength={4000}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </FormLabel>
  );
}
function EntityField({
  label,
  value,
  entries,
  onChange,
  optional = false,
}: {
  label: string;
  value: string | null;
  entries: StoryEntry[];
  onChange: (value: string | null) => void;
  optional?: boolean;
}) {
  return (
    <FormLabel>
      {label}
      <select
        className="h-10 w-full min-w-0 rounded-md border bg-background px-2 text-sm"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">{optional ? "不指定" : "选择故事角色"}</option>
        {value && !entries.some((entry) => entry.id === value) && (
          <option value={value}>{value}（冻结引用）</option>
        )}
        {entries.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.name}
          </option>
        ))}
      </select>
    </FormLabel>
  );
}

export function ScriptNarrativeEditor({
  projectId,
  scenes,
  onChange,
}: {
  projectId: string;
  scenes: NarrativeScene[];
  onChange: (index: number, scene: NarrativeScene) => void;
}) {
  const entries = usePagedList<StoryEntry>(
    `/projects/${projectId}/story-entries`,
  );
  const characters = entries.rows.filter(
    (entry) => entry.kind === "CHARACTER" && !entry.archived,
  );
  return (
    <section
      aria-label="连续剧场景交付"
      className="grid min-w-0 gap-4 border-y py-4"
    >
      <h3 className="font-medium">连续剧场景交付</h3>
      <p className="text-sm text-muted-foreground">
        填写人物目的、冲突和情绪变化。状态变更均为提案，只有项目拥有者批准此精确剧本后才提交事实。
      </p>
      <ListPagination
        {...entries}
        label="叙事故事设定"
        count={entries.rows.length}
      />
      {scenes.map((scene, index) => {
        const beat = scene.dramatic_beat ?? emptyBeat(1);
        const changes = scene.state_changes ?? [];
        const patchBeat = (patch: Partial<typeof beat>) =>
          onChange(index, { ...scene, dramatic_beat: { ...beat, ...patch } });
        const patchChange = (i: number, patch: Partial<StateChange>) =>
          onChange(index, {
            ...scene,
            state_changes: changes.map((v, n) =>
              n === i ? { ...v, ...patch } : v,
            ),
          });
        return (
          <details
            key={scene.scene_id ?? index}
            open
            className="grid min-w-0 gap-3 border-t pt-3"
          >
            <summary className="mb-3 font-medium">
              场景 {scene.scene_no} 的叙事与状态提案
            </summary>
            <div className="grid min-w-0 gap-3">
              <TextField
                label="场景故事时点"
                value={beat.story_time}
                numeric
                onChange={(v) => patchBeat({ story_time: Number(v) })}
              />
              <TextField
                label="场景关键冲突"
                value={beat.conflict}
                onChange={(conflict) => patchBeat({ conflict })}
              />
              {beat.character_objectives.map((objective, i) => (
                <div key={i} className="grid gap-3 border-t py-3">
                  <EntityField
                    label="场景人物"
                    value={objective.character_id}
                    entries={characters}
                    onChange={(character_id) =>
                      patchBeat({
                        character_objectives: beat.character_objectives.map(
                          (v, n) =>
                            n === i
                              ? { ...v, character_id: character_id ?? "" }
                              : v,
                        ),
                      })
                    }
                  />
                  {(
                    [
                      ["人物目的", "purpose"],
                      ["起始情绪", "emotion_start"],
                      ["结束情绪", "emotion_end"],
                    ] as const
                  ).map(([label, key]) => (
                    <TextField
                      key={key}
                      label={label}
                      value={objective[key]}
                      onChange={(text) =>
                        patchBeat({
                          character_objectives: beat.character_objectives.map(
                            (v, n) => (n === i ? { ...v, [key]: text } : v),
                          ),
                        })
                      }
                    />
                  ))}
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() =>
                      patchBeat({
                        character_objectives: beat.character_objectives.filter(
                          (_, n) => n !== i,
                        ),
                      })
                    }
                  >
                    移除此人物目的
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  patchBeat({
                    character_objectives: [
                      ...beat.character_objectives,
                      {
                        character_id: "",
                        purpose: "",
                        emotion_start: "",
                        emotion_end: "",
                      },
                    ],
                  })
                }
              >
                添加人物目的
              </Button>
              {beat.dialogue_interactions.map((interaction, i) => (
                <fieldset key={i} className="grid min-w-0 gap-3 border-t py-3">
                  <legend>对白与行动互动 {i + 1}</legend>
                  <TextField
                    label="互动行动与反应"
                    value={interaction.action}
                    onChange={(action) =>
                      patchBeat({
                        dialogue_interactions: beat.dialogue_interactions.map(
                          (v, n) => (n === i ? { ...v, action } : v),
                        ),
                      })
                    }
                  />
                  <p className="text-sm">关联精确台词</p>
                  {scene.dialogue
                    .filter((line) => !!line.line_id)
                    .map((line) => (
                      <label
                        key={line.line_id}
                        className="flex items-start gap-2 text-sm"
                      >
                        <input
                          type="checkbox"
                          checked={interaction.line_ids.includes(line.line_id!)}
                          onChange={(e) =>
                            patchBeat({
                              dialogue_interactions:
                                beat.dialogue_interactions.map((v, n) =>
                                  n === i
                                    ? {
                                        ...v,
                                        line_ids: e.target.checked
                                          ? [...v.line_ids, line.line_id!]
                                          : v.line_ids.filter(
                                              (id) => id !== line.line_id,
                                            ),
                                      }
                                    : v,
                                ),
                            })
                          }
                        />
                        {line.speaker}：{line.text}
                      </label>
                    ))}
                  <p className="text-sm">参与互动的角色</p>
                  {characters.map((character) => (
                    <label key={character.id} className="flex gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={interaction.character_ids.includes(
                          character.id,
                        )}
                        onChange={(e) =>
                          patchBeat({
                            dialogue_interactions:
                              beat.dialogue_interactions.map((v, n) =>
                                n === i
                                  ? {
                                      ...v,
                                      character_ids: e.target.checked
                                        ? [...v.character_ids, character.id]
                                        : v.character_ids.filter(
                                            (id) => id !== character.id,
                                          ),
                                    }
                                  : v,
                              ),
                          })
                        }
                      />
                      {character.name}
                    </label>
                  ))}
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() =>
                      patchBeat({
                        dialogue_interactions:
                          beat.dialogue_interactions.filter((_, n) => n !== i),
                      })
                    }
                  >
                    移除此互动
                  </Button>
                </fieldset>
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  patchBeat({
                    dialogue_interactions: [
                      ...beat.dialogue_interactions,
                      { line_ids: [], character_ids: [], action: "" },
                    ],
                  })
                }
              >
                添加对白互动
              </Button>
              {changes.map((change, i) => (
                <fieldset key={i} className="grid min-w-0 gap-3 border-t py-3">
                  <legend>状态变更提案 {i + 1}</legend>
                  <FormLabel>
                    状态类别
                    <select
                      className="h-10 w-full rounded-md border bg-background px-2"
                      value={change.kind}
                      onChange={(e) =>
                        patchChange(i, { kind: e.target.value as LedgerKind })
                      }
                    >
                      {Object.entries(ledgerKinds).map(([kind, label]) => (
                        <option key={kind} value={kind}>
                          {label}
                        </option>
                      ))}
                    </select>
                  </FormLabel>
                  <EntityField
                    label="状态主体"
                    value={change.subject_id}
                    entries={entries.rows}
                    optional
                    onChange={(subject_id) => patchChange(i, { subject_id })}
                  />
                  <EntityField
                    label="状态关联对象"
                    value={change.target_id}
                    entries={entries.rows}
                    optional
                    onChange={(target_id) => patchChange(i, { target_id })}
                  />
                  <TextField
                    label="状态名称"
                    value={change.state_key}
                    onChange={(state_key) => patchChange(i, { state_key })}
                  />
                  <TextField
                    label="拟确认的状态变化"
                    value={change.value}
                    onChange={(value) => patchChange(i, { value })}
                  />
                  <TextField
                    label="状态生效故事时点"
                    value={change.story_start}
                    numeric
                    onChange={(v) => patchChange(i, { story_start: Number(v) })}
                  />
                  <TextField
                    label="状态结束故事时点（不含，留空持续）"
                    value={change.story_end ?? ""}
                    numeric
                    onChange={(v) =>
                      patchChange(i, { story_end: v === "" ? null : Number(v) })
                    }
                  />
                  <TextField
                    label="前因事件标识（逗号分隔）"
                    value={change.causes.join(", ")}
                    onChange={(v) =>
                      patchChange(i, {
                        causes: v
                          .split(/[,，\n]/)
                          .map((id) => id.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                  <TextField
                    label="替代的既有状态标识（可选）"
                    value={change.supersedes_entry_id ?? ""}
                    onChange={(v) =>
                      patchChange(i, { supersedes_entry_id: v.trim() || null })
                    }
                  />
                  {change.known_by.map((known, k) => (
                    <div key={k} className="grid gap-2 border-t pt-2">
                      <EntityField
                        label="知情角色"
                        value={known.character_id}
                        entries={characters}
                        onChange={(character_id) =>
                          patchChange(i, {
                            known_by: change.known_by.map((v, n) =>
                              n === k
                                ? { ...v, character_id: character_id ?? "" }
                                : v,
                            ),
                          })
                        }
                      />
                      <TextField
                        label="开始知情故事时点"
                        value={known.story_start}
                        numeric
                        onChange={(v) =>
                          patchChange(i, {
                            known_by: change.known_by.map((row, n) =>
                              n === k
                                ? { ...row, story_start: Number(v) }
                                : row,
                            ),
                          })
                        }
                      />
                      <TextField
                        label="停止知情故事时点（不含，可选）"
                        value={known.story_end ?? ""}
                        numeric
                        onChange={(v) =>
                          patchChange(i, {
                            known_by: change.known_by.map((row, n) =>
                              n === k
                                ? {
                                    ...row,
                                    story_end: v === "" ? null : Number(v),
                                  }
                                : row,
                            ),
                          })
                        }
                      />
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() =>
                          patchChange(i, {
                            known_by: change.known_by.filter((_, n) => n !== k),
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
                    onClick={() =>
                      patchChange(i, {
                        known_by: [
                          ...change.known_by,
                          {
                            character_id: "",
                            story_start: change.story_start,
                            story_end: null,
                          },
                        ],
                      })
                    }
                  >
                    添加提案知情角色
                  </Button>
                  <TextField
                    label="状态提案标识"
                    value={change.id}
                    onChange={(id) => patchChange(i, { id: id.trim() })}
                  />
                  <p className="text-xs text-muted-foreground">
                    同一时点修订既有状态时，提案标识与被替代标识相同；跨时点的新变化保留新标识，并填写被替代标识。
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() =>
                      onChange(index, {
                        ...scene,
                        state_changes: changes.filter((_, n) => n !== i),
                      })
                    }
                  >
                    移除此状态提案
                  </Button>
                </fieldset>
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  onChange(index, {
                    ...scene,
                    state_changes: [
                      ...changes,
                      {
                        id: crypto.randomUUID(),
                        kind: "EVENT",
                        subject_id: null,
                        target_id: null,
                        state_key: "",
                        value: "",
                        story_start: beat.story_time,
                        story_end: null,
                        known_by: [],
                        causes: [],
                        supersedes_entry_id: null,
                      },
                    ],
                  })
                }
              >
                添加状态变更提案
              </Button>
            </div>
          </details>
        );
      })}
    </section>
  );
}

export function ScriptNarrativeSummary({
  content,
  approving = false,
}: {
  content: Record<string, unknown>;
  approving?: boolean;
}) {
  if (content.schema_version !== "3.0")
    return (
      <p className="text-xs text-muted-foreground">
        此旧版剧本未记录连续剧情交付。
      </p>
    );
  const context = content.narrative_context as Record<string, unknown>;
  const scenes = content.scenes as NarrativeScene[];
  return (
    <section
      aria-label="连续剧绑定与状态提案"
      className="grid min-w-0 gap-2 border-y py-3 text-sm"
    >
      <h4 className="font-medium">连续剧绑定与状态提案</h4>
      <p className="break-all">
        规划版本：{String(context?.series_plan_version_id ?? "未绑定")}
      </p>
      <p className="break-all">
        账本基准：{String(context?.ledger_version_id ?? "尚无批准账本")} · 修订{" "}
        {String(context?.expected_ledger_head_revision ?? "未记录")}
      </p>
      {scenes.map((scene) => (
        <details key={scene.scene_id ?? scene.scene_no} open={approving}>
          <summary>
            场景 {scene.scene_no} · 故事时点 {scene.dramatic_beat?.story_time} ·{" "}
            {scene.state_changes?.length ?? 0} 项状态提案
          </summary>
          <p>冲突：{scene.dramatic_beat?.conflict}</p>
          {scene.dramatic_beat?.character_objectives.map((objective, i) => (
            <p key={i}>
              人物目的：{objective.purpose}；情绪：{objective.emotion_start} →{" "}
              {objective.emotion_end}
            </p>
          ))}
          {scene.dramatic_beat?.dialogue_interactions.map((interaction, i) => (
            <p key={i}>对白互动：{interaction.action}</p>
          ))}
          {scene.state_changes?.map((change) => (
            <p key={change.id}>
              {ledgerKinds[change.kind]} · {change.state_key}：{change.value}
            </p>
          ))}
        </details>
      ))}
      {approving && (
        <Notice>
          批准此精确剧本时，上述状态提案将与剧本绑定提交。账本基准变化时须重新核对；未批准稿不会成为已确认事实。
        </Notice>
      )}
      <details>
        <summary>冻结写作依据</summary>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
          {JSON.stringify(context, null, 2)}
        </pre>
      </details>
    </section>
  );
}

export function NarrativeApproval({
  episodeId,
  versionId,
  onReady,
}: {
  episodeId: string;
  versionId: string;
  onReady: (id: string) => void;
}) {
  const [data, setData] = useState<ContentVersion | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api<ContentVersion>(`/episodes/${episodeId}/scripts/${versionId}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) {
          if (value.id !== versionId || value.episode_id !== episodeId)
            throw new Error("剧本批准依据不属于当前剧集或版本。");
          setData(value);
          setError("");
          onReady(versionId);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [episodeId, versionId, refresh, onReady]);
  return data?.id === versionId && data.episode_id === episodeId ? (
    <ScriptNarrativeSummary content={data.content_json} approving />
  ) : error ? (
    <Notice tone="error" role="alert">
      {error}
      <Button
        type="button"
        variant="outline"
        onClick={() => setRefresh((v) => v + 1)}
      >
        重读剧本批准依据
      </Button>
    </Notice>
  ) : (
    <p role="status">正在读取剧本批准依据…</p>
  );
}

export function StoryStateReceiptSummary({
  receipt,
}: {
  receipt: StoryStateReceipt;
}) {
  return (
    <section
      role="region"
      aria-label="本次剧本批准回执"
      className="grid min-w-0 gap-2 rounded-lg border p-3 text-sm"
    >
      <h4 className="font-medium">本次剧本批准回执</h4>
      <p role="status">
        剧本与状态提案已一起批准，账本修订 {receipt.revision}。
      </p>
      <p className="break-all">精确剧本：{receipt.script_version_id}</p>
      <p className="break-all">批准账本：{receipt.approved_version_id}</p>
      <details>
        <summary>批准来源与摘要校验</summary>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">
          {JSON.stringify(receipt, null, 2)}
        </pre>
      </details>
      <p className="text-xs text-muted-foreground">
        此回执仅显示本次操作结果；历史依据请查看账本版本、批准记录和精确来源。
      </p>
    </section>
  );
}
