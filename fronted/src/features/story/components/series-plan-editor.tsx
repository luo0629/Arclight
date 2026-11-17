"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";
import type { StoryEntry } from "@/types/api";
import type { CharacterReference, Milestone, SeriesPlan } from "../series-plan";

function PlanText({
  id,
  label,
  value,
  onChange,
  short = false,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  short?: boolean;
}) {
  return (
    <Field>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {short ? (
        <Input
          id={id}
          value={value}
          maxLength={200}
          required
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Textarea
          id={id}
          value={value}
          maxLength={4000}
          rows={2}
          required
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </Field>
  );
}

function EpisodeNumber({
  id,
  label,
  value,
  count,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  count: number;
  onChange: (value: number) => void;
}) {
  return (
    <Field>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input
        id={id}
        type="number"
        min={1}
        max={count}
        step={1}
        required
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </Field>
  );
}

function Milestones({
  id,
  label,
  value,
  count,
  onChange,
}: {
  id: string;
  label: string;
  value: Milestone[];
  count: number;
  onChange: (value: Milestone[]) => void;
}) {
  return (
    <div className="grid gap-3">
      <p className="text-sm font-medium">分集变化</p>
      {value.map((milestone, index) => (
        <div key={index} className="grid min-w-0 gap-3 border-l pl-3">
          <EpisodeNumber
            id={`${id}-${index}-episode`}
            label={`${label}变化${index + 1}集数`}
            value={milestone.episode_no}
            count={count}
            onChange={(episode_no) =>
              onChange(
                value.map((row, i) =>
                  i === index ? { ...row, episode_no } : row,
                ),
              )
            }
          />
          <PlanText
            id={`${id}-${index}-change`}
            label={`${label}变化${index + 1}说明`}
            value={milestone.change}
            onChange={(change) =>
              onChange(
                value.map((row, i) => (i === index ? { ...row, change } : row)),
              )
            }
          />
          <Button
            type="button"
            variant="ghost"
            className="justify-self-start"
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            移除{label}变化{index + 1}
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        className="justify-self-start"
        disabled={value.length >= count}
        onClick={() =>
          onChange([
            ...value,
            {
              episode_no:
                Array.from({ length: count }, (_, i) => i + 1).find(
                  (n) => !value.some((m) => m.episode_no === n),
                ) ?? 1,
              change: "",
            },
          ])
        }
      >
        添加{label}分集变化
      </Button>
    </div>
  );
}

type Props = {
  plan: SeriesPlan;
  episodeCount: number;
  projectId: string;
  disabled: boolean;
  references: CharacterReference[];
  onChange: (plan: SeriesPlan) => void;
};

export default function SeriesPlanEditor({
  plan,
  episodeCount,
  projectId,
  disabled,
  references,
  onChange,
}: Props) {
  const [characters, setCharacters] = useState<StoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setLoading(true);
    setError("");
    setCharacters([]);
    void (async () => {
      const rows: StoryEntry[] = [];
      let offset = 0;
      while (true) {
        const page = await api<StoryEntry[]>(
          `/projects/${projectId}/story-entries?limit=100&offset=${offset}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        rows.push(...page);
        offset += page.length;
        if (page.length < 100) break;
      }
      if (live)
        setCharacters(
          rows.filter(
            (row) =>
              row.project_id === projectId &&
              row.kind === "CHARACTER" &&
              !row.archived,
          ),
        );
    })()
      .catch((e: unknown) => {
        if (live && !controller.signal.aborted) setError(message(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, refresh]);

  const frozenCharacters = new Map(references.map((row) => [row.id, row]));
  const names = new Map(
    plan.character_arcs.map((arc) => {
      const frozen = frozenCharacters.get(arc.character_id);
      const current = characters.find((row) => row.id === arc.character_id);
      return [
        arc.character_id,
        frozen && (disabled || frozen.revision === arc.character_revision)
          ? frozen.name
          : (current?.name ?? frozen?.name ?? "历史人物"),
      ];
    }),
  );
  const characterOptions = characters.map((row) => ({
    value: row.id,
    label: row.name,
  }));
  const plannedCharacters = plan.character_arcs
    .filter((arc) => arc.character_id)
    .map((arc) => ({
      value: arc.character_id,
      label: names.get(arc.character_id) ?? "历史人物",
    }));
  const threadOptions = plan.threads.map((row, index) => ({
    value: row.id,
    label: row.title || `线索${index + 1}（待填写）`,
  }));
  const arcOptions = (id: string) => {
    if (!id) return characterOptions;
    if (disabled) return [{ value: id, label: names.get(id) ?? "历史人物" }];
    return characterOptions.some((row) => row.value === id)
      ? characterOptions.map((row) =>
          row.value === id
            ? { ...row, label: names.get(id) ?? row.label }
            : row,
        )
      : [
          ...characterOptions,
          {
            value: id,
            label: (names.get(id) ?? "历史人物") + "（当前不可选）",
            disabled: true,
          },
        ];
  };

  function change<K extends keyof SeriesPlan>(key: K, value: SeriesPlan[K]) {
    onChange({ ...plan, [key]: value });
  }
  return (
    <section className="grid min-w-0 gap-5" aria-label="全剧规划">
      <div className="grid gap-2">
        <h4 className="text-lg font-semibold">全剧／全季规划</h4>
        <p className="text-sm text-muted-foreground">
          以下内容是预定规划，尚非已发生事实。伏笔的预计回收也不表示已经完成。
        </p>
        <p className="text-sm text-muted-foreground">
          与当前分集大纲一起另存、确认；局部修改保留旧版和其他未变内容。
        </p>
      </div>
      {loading && <p role="status">正在读取项目人物…</p>}
      {error && (
        <Notice tone="error" role="alert">
          人物读取失败：{error}
        </Notice>
      )}
      {!loading && !characters.length && !error && (
        <Notice>请先在故事设定中建立角色，再选择人物成长与关系的对象。</Notice>
      )}
      <Button
        type="button"
        variant="outline"
        className="justify-self-start"
        disabled={loading || disabled}
        onClick={() => setRefresh((v) => v + 1)}
      >
        刷新可选人物
      </Button>
      <fieldset disabled={disabled} className="grid min-w-0 gap-5">
        <details open className="studio-disclosure">
          <summary>主线与支线</summary>
          <div className="grid gap-5 pt-4">
            {plan.threads.map((thread, index) => (
              <div key={thread.id} className="grid min-w-0 gap-3 border-b pb-5">
                <h5 className="font-medium">线索{index + 1}</h5>
                <Field>
                  <FieldLabel htmlFor={`series-thread-${index}-kind`}>
                    线索{index + 1}类型
                  </FieldLabel>
                  <ChoiceSelect
                    id={`series-thread-${index}-kind`}
                    aria-label={`线索${index + 1}类型`}
                    value={thread.kind}
                    disabled={disabled}
                    options={[
                      { value: "MAIN", label: "主线" },
                      { value: "SUBPLOT", label: "支线" },
                    ]}
                    onValueChange={(kind) =>
                      change(
                        "threads",
                        plan.threads.map((row, i) =>
                          i === index
                            ? { ...row, kind: kind as "MAIN" | "SUBPLOT" }
                            : row,
                        ),
                      )
                    }
                  />
                </Field>
                {(
                  [
                    ["title", "标题", true],
                    ["central_conflict", "关键冲突", false],
                    ["planned_resolution", "预定收束", false],
                  ] as const
                ).map(([key, label, short]) => (
                  <PlanText
                    key={key}
                    id={`series-thread-${index}-${key}`}
                    label={`线索${index + 1}${label}`}
                    value={thread[key]}
                    short={short}
                    onChange={(value) =>
                      change(
                        "threads",
                        plan.threads.map((row, i) =>
                          i === index ? { ...row, [key]: value } : row,
                        ),
                      )
                    }
                  />
                ))}
                <Button
                  type="button"
                  variant="ghost"
                  className="justify-self-start"
                  disabled={plan.threads.length <= 1}
                  onClick={() =>
                    change(
                      "threads",
                      plan.threads.filter((_, i) => i !== index),
                    )
                  }
                >
                  移除线索{index + 1}
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              className="justify-self-start"
              disabled={plan.threads.length >= 100}
              onClick={() =>
                change("threads", [
                  ...plan.threads,
                  {
                    id: crypto.randomUUID(),
                    kind: "SUBPLOT",
                    title: "",
                    central_conflict: "",
                    planned_resolution: "",
                  },
                ])
              }
            >
              添加支线
            </Button>
          </div>
        </details>
        <details open className="studio-disclosure">
          <summary>人物成长</summary>
          <div className="grid gap-5 pt-4">
            {plan.character_arcs.map((arc, index) => {
              const current = characters.find((c) => c.id === arc.character_id);
              return (
                <div key={index} className="grid min-w-0 gap-3 border-b pb-5">
                  <h5 className="font-medium">人物{index + 1}</h5>
                  <Field>
                    <FieldLabel htmlFor={`series-character-${index}`}>
                      人物{index + 1}角色
                    </FieldLabel>
                    <ChoiceSelect
                      id={`series-character-${index}`}
                      aria-label={`人物${index + 1}角色`}
                      value={arc.character_id}
                      disabled={disabled || loading}
                      placeholder="选择故事设定中的人物"
                      options={arcOptions(arc.character_id)}
                      onValueChange={(character_id) =>
                        change(
                          "character_arcs",
                          plan.character_arcs.map((row, i) =>
                            i === index
                              ? {
                                  ...row,
                                  character_id,
                                  character_revision:
                                    characters.find(
                                      (c) => c.id === character_id,
                                    )?.revision ?? 0,
                                }
                              : row,
                          ),
                        )
                      }
                    />
                  </Field>
                  {arc.character_id && (
                    <p className="text-xs text-muted-foreground">
                      规划引用 {names.get(arc.character_id) ?? "历史人物"}{" "}
                      的设定修订{" "}
                      {disabled
                        ? (frozenCharacters.get(arc.character_id)?.revision ??
                          arc.character_revision)
                        : arc.character_revision}
                    </p>
                  )}
                  {!disabled &&
                    current &&
                    current.revision !== arc.character_revision && (
                      <Notice>
                        此人物设定已变化；采用当前设定后重新检查成长规划。
                        <Button
                          type="button"
                          variant="outline"
                          disabled={disabled}
                          onClick={() =>
                            change(
                              "character_arcs",
                              plan.character_arcs.map((row, i) =>
                                i === index
                                  ? {
                                      ...row,
                                      character_revision: current.revision,
                                    }
                                  : row,
                              ),
                            )
                          }
                        >
                          人物{index + 1}采用当前设定
                        </Button>
                      </Notice>
                    )}
                  {(
                    [
                      ["starting_goal", "初始目标"],
                      ["internal_conflict", "内在冲突"],
                      ["planned_growth", "预定成长"],
                    ] as const
                  ).map(([key, label]) => (
                    <PlanText
                      key={key}
                      id={`series-character-${index}-${key}`}
                      label={`人物${index + 1}${label}`}
                      value={arc[key]}
                      onChange={(value) =>
                        change(
                          "character_arcs",
                          plan.character_arcs.map((row, i) =>
                            i === index ? { ...row, [key]: value } : row,
                          ),
                        )
                      }
                    />
                  ))}
                  <Milestones
                    id={`series-character-${index}-milestone`}
                    label={`人物${index + 1}`}
                    value={arc.milestones}
                    count={episodeCount}
                    onChange={(milestones) =>
                      change(
                        "character_arcs",
                        plan.character_arcs.map((row, i) =>
                          i === index ? { ...row, milestones } : row,
                        ),
                      )
                    }
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    className="justify-self-start"
                    onClick={() =>
                      change(
                        "character_arcs",
                        plan.character_arcs.filter((_, i) => i !== index),
                      )
                    }
                  >
                    移除人物{index + 1}成长规划
                  </Button>
                </div>
              );
            })}
            <Button
              type="button"
              variant="outline"
              className="justify-self-start"
              disabled={
                plan.character_arcs.length >= 200 ||
                loading ||
                !characters.length
              }
              onClick={() =>
                change("character_arcs", [
                  ...plan.character_arcs,
                  {
                    character_id: "",
                    character_revision: 0,
                    starting_goal: "",
                    internal_conflict: "",
                    planned_growth: "",
                    milestones: [],
                  },
                ])
              }
            >
              添加人物成长
            </Button>
          </div>
        </details>
        <details className="studio-disclosure">
          <summary>关系发展</summary>
          <div className="grid gap-5 pt-4">
            <p className="text-sm text-muted-foreground">
              选择已列入成长规划的人物；记录预定关系变化，不会改写故事设定中的已确认关系。
            </p>
            {plan.relationship_arcs.map((arc, index) => (
              <div key={arc.id} className="grid min-w-0 gap-3 border-b pb-5">
                <h5 className="font-medium">关系{index + 1}</h5>
                {(
                  [
                    ["source_character_id", "一方"],
                    ["target_character_id", "另一方"],
                  ] as const
                ).map(([key, label]) => (
                  <Field key={key}>
                    <FieldLabel htmlFor={`series-relationship-${index}-${key}`}>
                      关系{index + 1}
                      {label}
                    </FieldLabel>
                    <ChoiceSelect
                      id={`series-relationship-${index}-${key}`}
                      aria-label={`关系${index + 1}${label}`}
                      value={arc[key]}
                      disabled={disabled}
                      placeholder="选择人物"
                      options={plannedCharacters}
                      onValueChange={(value) =>
                        change(
                          "relationship_arcs",
                          plan.relationship_arcs.map((row, i) =>
                            i === index ? { ...row, [key]: value } : row,
                          ),
                        )
                      }
                    />
                  </Field>
                ))}
                {(
                  [
                    ["initial_relationship", "初始关系"],
                    ["planned_development", "预定发展"],
                  ] as const
                ).map(([key, label]) => (
                  <PlanText
                    key={key}
                    id={`series-relationship-${index}-${key}`}
                    label={`关系${index + 1}${label}`}
                    value={arc[key]}
                    onChange={(value) =>
                      change(
                        "relationship_arcs",
                        plan.relationship_arcs.map((row, i) =>
                          i === index ? { ...row, [key]: value } : row,
                        ),
                      )
                    }
                  />
                ))}
                <Milestones
                  id={`series-relationship-${index}-milestone`}
                  label={`关系${index + 1}`}
                  value={arc.milestones}
                  count={episodeCount}
                  onChange={(milestones) =>
                    change(
                      "relationship_arcs",
                      plan.relationship_arcs.map((row, i) =>
                        i === index ? { ...row, milestones } : row,
                      ),
                    )
                  }
                />
                <Button
                  type="button"
                  variant="ghost"
                  className="justify-self-start"
                  onClick={() =>
                    change(
                      "relationship_arcs",
                      plan.relationship_arcs.filter((_, i) => i !== index),
                    )
                  }
                >
                  移除关系{index + 1}
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              className="justify-self-start"
              disabled={
                plan.relationship_arcs.length >= 200 ||
                plannedCharacters.length < 2
              }
              onClick={() =>
                change("relationship_arcs", [
                  ...plan.relationship_arcs,
                  {
                    id: crypto.randomUUID(),
                    source_character_id: "",
                    target_character_id: "",
                    initial_relationship: "",
                    planned_development: "",
                    milestones: [],
                  },
                ])
              }
            >
              添加关系发展
            </Button>
          </div>
        </details>
        <details open className="studio-disclosure">
          <summary>伏笔与预定回收</summary>
          <div className="grid gap-5 pt-4">
            {plan.foreshadowing.map((item, index) => (
              <div key={item.id} className="grid min-w-0 gap-3 border-b pb-5">
                <h5 className="font-medium">伏笔{index + 1}</h5>
                <Field>
                  <FieldLabel htmlFor={`series-foreshadow-${index}-thread`}>
                    伏笔{index + 1}所属线索
                  </FieldLabel>
                  <ChoiceSelect
                    id={`series-foreshadow-${index}-thread`}
                    aria-label={`伏笔${index + 1}所属线索`}
                    value={item.thread_id}
                    disabled={disabled}
                    placeholder="选择线索"
                    options={threadOptions}
                    onValueChange={(thread_id) =>
                      change(
                        "foreshadowing",
                        plan.foreshadowing.map((row, i) =>
                          i === index ? { ...row, thread_id } : row,
                        ),
                      )
                    }
                  />
                </Field>
                <div className="grid gap-3 sm:grid-cols-2">
                  <EpisodeNumber
                    id={`series-foreshadow-${index}-setup`}
                    label={`伏笔${index + 1}设置集`}
                    value={item.setup_episode_no}
                    count={episodeCount}
                    onChange={(setup_episode_no) =>
                      change(
                        "foreshadowing",
                        plan.foreshadowing.map((row, i) =>
                          i === index ? { ...row, setup_episode_no } : row,
                        ),
                      )
                    }
                  />
                  <EpisodeNumber
                    id={`series-foreshadow-${index}-payoff`}
                    label={`伏笔${index + 1}预计回收集`}
                    value={item.expected_payoff_episode_no}
                    count={episodeCount}
                    onChange={(expected_payoff_episode_no) =>
                      change(
                        "foreshadowing",
                        plan.foreshadowing.map((row, i) =>
                          i === index
                            ? { ...row, expected_payoff_episode_no }
                            : row,
                        ),
                      )
                    }
                  />
                </div>
                <PlanText
                  id={`series-foreshadow-${index}-hint`}
                  label={`伏笔${index + 1}设置说明`}
                  value={item.setup}
                  onChange={(setup) =>
                    change(
                      "foreshadowing",
                      plan.foreshadowing.map((row, i) =>
                        i === index ? { ...row, setup } : row,
                      ),
                    )
                  }
                />
                <PlanText
                  id={`series-foreshadow-${index}-resolution`}
                  label={`伏笔${index + 1}预定回收说明`}
                  value={item.planned_payoff}
                  onChange={(planned_payoff) =>
                    change(
                      "foreshadowing",
                      plan.foreshadowing.map((row, i) =>
                        i === index ? { ...row, planned_payoff } : row,
                      ),
                    )
                  }
                />
                <Button
                  type="button"
                  variant="ghost"
                  className="justify-self-start"
                  disabled={plan.foreshadowing.length <= 1}
                  onClick={() =>
                    change(
                      "foreshadowing",
                      plan.foreshadowing.filter((_, i) => i !== index),
                    )
                  }
                >
                  移除伏笔{index + 1}
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              className="justify-self-start"
              disabled={plan.foreshadowing.length >= 500}
              onClick={() =>
                change("foreshadowing", [
                  ...plan.foreshadowing,
                  {
                    id: crypto.randomUUID(),
                    thread_id: plan.threads[0]?.id ?? "",
                    setup_episode_no: 1,
                    setup: "",
                    expected_payoff_episode_no: episodeCount,
                    planned_payoff: "",
                  },
                ])
              }
            >
              添加伏笔
            </Button>
          </div>
        </details>
        <details open className="studio-disclosure">
          <summary>分集因果与结尾承接</summary>
          <div className="grid gap-5 pt-4">
            {plan.episode_progressions.map((episode, index) => (
              <div
                key={episode.episode_no}
                className="grid min-w-0 gap-3 border-b pb-5"
                aria-label={`第${episode.episode_no}集叙事推进`}
              >
                <h5 className="font-medium">
                  第{episode.episode_no}集叙事推进
                </h5>
                {episode.thread_beats.map((beat, beatIndex) => (
                  <div
                    key={beatIndex}
                    className="grid min-w-0 gap-3 border-l pl-3"
                  >
                    <Field>
                      <FieldLabel
                        htmlFor={`series-episode-${index}-beat-${beatIndex}-thread`}
                      >
                        第{episode.episode_no}集推进{beatIndex + 1}线索
                      </FieldLabel>
                      <ChoiceSelect
                        id={`series-episode-${index}-beat-${beatIndex}-thread`}
                        aria-label={`第${episode.episode_no}集推进${beatIndex + 1}线索`}
                        value={beat.thread_id}
                        disabled={disabled}
                        options={threadOptions}
                        placeholder="选择线索"
                        onValueChange={(thread_id) =>
                          change(
                            "episode_progressions",
                            plan.episode_progressions.map((row, i) =>
                              i === index
                                ? {
                                    ...row,
                                    thread_beats: row.thread_beats.map(
                                      (b, j) =>
                                        j === beatIndex
                                          ? { ...b, thread_id }
                                          : b,
                                    ),
                                  }
                                : row,
                            ),
                          )
                        }
                      />
                    </Field>
                    <PlanText
                      id={`series-episode-${index}-beat-${beatIndex}-progression`}
                      label={`第${episode.episode_no}集推进${beatIndex + 1}说明`}
                      value={beat.progression}
                      onChange={(progression) =>
                        change(
                          "episode_progressions",
                          plan.episode_progressions.map((row, i) =>
                            i === index
                              ? {
                                  ...row,
                                  thread_beats: row.thread_beats.map((b, j) =>
                                    j === beatIndex ? { ...b, progression } : b,
                                  ),
                                }
                              : row,
                          ),
                        )
                      }
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      className="justify-self-start"
                      disabled={episode.thread_beats.length <= 1}
                      onClick={() =>
                        change(
                          "episode_progressions",
                          plan.episode_progressions.map((row, i) =>
                            i === index
                              ? {
                                  ...row,
                                  thread_beats: row.thread_beats.filter(
                                    (_, j) => j !== beatIndex,
                                  ),
                                }
                              : row,
                          ),
                        )
                      }
                    >
                      移除第{episode.episode_no}集推进{beatIndex + 1}
                    </Button>
                  </div>
                ))}
                <Button
                  type="button"
                  variant="outline"
                  className="justify-self-start"
                  disabled={episode.thread_beats.length >= plan.threads.length}
                  onClick={() =>
                    change(
                      "episode_progressions",
                      plan.episode_progressions.map((row, i) =>
                        i === index
                          ? {
                              ...row,
                              thread_beats: [
                                ...row.thread_beats,
                                {
                                  thread_id:
                                    plan.threads.find(
                                      (thread) =>
                                        !row.thread_beats.some(
                                          (beat) =>
                                            beat.thread_id === thread.id,
                                        ),
                                    )?.id ?? "",
                                  progression: "",
                                },
                              ],
                            }
                          : row,
                      ),
                    )
                  }
                >
                  添加第{episode.episode_no}集线索推进
                </Button>
                <fieldset className="grid gap-3">
                  <legend className="text-sm font-medium">
                    第{episode.episode_no}集因果来源
                  </legend>
                  <p className="text-xs text-muted-foreground">
                    添加带来本集后果的其他集；这是叙事关联，不会创建制作依赖。
                  </p>
                  {episode.caused_by_episode_nos.map((n, sourceIndex) => (
                    <div
                      key={sourceIndex}
                      className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end"
                    >
                      <EpisodeNumber
                        id={`series-episode-${index}-cause-${sourceIndex}`}
                        label={`第${episode.episode_no}集因果来源${sourceIndex + 1}集号`}
                        value={n}
                        count={episodeCount}
                        onChange={(value) =>
                          change(
                            "episode_progressions",
                            plan.episode_progressions.map((row, i) =>
                              i === index
                                ? {
                                    ...row,
                                    caused_by_episode_nos:
                                      row.caused_by_episode_nos.map((v, j) =>
                                        j === sourceIndex ? value : v,
                                      ),
                                  }
                                : row,
                            ),
                          )
                        }
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() =>
                          change(
                            "episode_progressions",
                            plan.episode_progressions.map((row, i) =>
                              i === index
                                ? {
                                    ...row,
                                    caused_by_episode_nos:
                                      row.caused_by_episode_nos.filter(
                                        (_, j) => j !== sourceIndex,
                                      ),
                                  }
                                : row,
                            ),
                          )
                        }
                      >
                        移除第{episode.episode_no}集因果来源{sourceIndex + 1}
                      </Button>
                    </div>
                  ))}
                  <Button
                    type="button"
                    variant="outline"
                    className="justify-self-start"
                    disabled={
                      episode.caused_by_episode_nos.length >= episodeCount - 1
                    }
                    onClick={() =>
                      change(
                        "episode_progressions",
                        plan.episode_progressions.map((row, i) =>
                          i === index
                            ? {
                                ...row,
                                caused_by_episode_nos: [
                                  ...row.caused_by_episode_nos,
                                  Array.from(
                                    { length: episodeCount },
                                    (_, n) => n + 1,
                                  ).find(
                                    (n) =>
                                      n !== row.episode_no &&
                                      !row.caused_by_episode_nos.includes(n),
                                  ) ?? 1,
                                ],
                              }
                            : row,
                        ),
                      )
                    }
                  >
                    添加第{episode.episode_no}集因果来源
                  </Button>
                </fieldset>
                <PlanText
                  id={`series-episode-${index}-cause`}
                  label={`第${episode.episode_no}集因果说明`}
                  value={episode.causal_link}
                  onChange={(causal_link) =>
                    change(
                      "episode_progressions",
                      plan.episode_progressions.map((row, i) =>
                        i === index ? { ...row, causal_link } : row,
                      ),
                    )
                  }
                />
                <PlanText
                  id={`series-episode-${index}-handoff`}
                  label={`第${episode.episode_no}集结尾承接`}
                  value={episode.ending_handoff}
                  onChange={(ending_handoff) =>
                    change(
                      "episode_progressions",
                      plan.episode_progressions.map((row, i) =>
                        i === index ? { ...row, ending_handoff } : row,
                      ),
                    )
                  }
                />
              </div>
            ))}
          </div>
        </details>
      </fieldset>
    </section>
  );
}
