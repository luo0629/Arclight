import { StatusBadge, FormLabel } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Field } from "@/components/ui/field";
import { useId, useRef, useState } from "react";
import { api, json, type ContentVersion, type Episode } from "./api";

export type VersionKind = "script" | "storyboard";
export type VersionCreated = (
  version: ContentVersion,
  kind: VersionKind,
) => Promise<void>;
export type Act = (action: () => Promise<unknown>) => Promise<boolean>;
type Script = {
  title: string;
  summary: string;
  characters: string[];
  scenes: {
    scene_no: number;
    title: string;
    action: string;
    dialogue: { speaker: string; text: string }[];
  }[];
};
type Board = {
  shots: {
    shot_no: number;
    scene_no: number;
    duration_ms: number;
    shot_size: string;
    visual_description: string;
    characters: string[];
    camera_movement: string;
    subtitle: string;
  }[];
};

export default function VersionEditor({
  version,
  kind,
  episode,
  writable,
  busy,
  act,
  latestNo,
  active,
  onCreated,
  sourceScript,
}: {
  sourceScript?: ContentVersion;
  version?: ContentVersion;
  onCreated: VersionCreated;
  kind: VersionKind;
  episode: Episode;
  writable: boolean;
  busy: boolean;
  act: Act;
  latestNo: number;
  active: boolean;
}) {
  const editorId = useId();
  const [editing, setEditing] = useState(!version);
  const source = sourceScript?.content_json as Script | undefined;
  function emptyShot(shotNo: number): Board["shots"][number] {
    return {
      shot_no: shotNo,
      scene_no: source?.scenes[0]?.scene_no ?? 1,
      duration_ms: 3000,
      shot_size: "medium",
      visual_description: "",
      characters: [],
      camera_movement: "static",
      subtitle: "",
    };
  }
  const [draft, setDraft] = useState<Record<string, unknown>>(() =>
    version
      ? structuredClone(version.content_json)
      : kind === "storyboard"
        ? { shots: [emptyShot(1)] }
        : {
            title: "",
            summary: "",
            characters: ["主角"],
            scenes: [{ scene_no: 1, title: "", action: "", dialogue: [] }],
          },
  );
  const saving = useRef(false);
  const script = draft as unknown as Script;
  const board = draft as unknown as Board;
  function mutate(change: (value: Script & Board) => void) {
    const copy = structuredClone(draft);
    change(copy as unknown as Script & Board);
    setDraft(copy);
  }
  const invalidBoard =
    kind === "storyboard" &&
    board.shots.some(
      (shot) =>
        !shot.visual_description.trim() ||
        !Number.isFinite(shot.duration_ms) ||
        shot.duration_ms < 500 ||
        shot.duration_ms > 30000,
    );
  async function save() {
    if (
      saving.current ||
      busy ||
      active ||
      !writable ||
      invalidBoard ||
      (kind === "storyboard" && !version && !sourceScript)
    )
      return;
    saving.current = true;
    try {
      await act(async () => {
        const created = await api<ContentVersion>(
          `/episodes/${episode.id}/${kind === "script" ? "scripts" : "storyboards"}`,
          json("POST", {
            expected_version_no: latestNo,
            content: draft,
            ...(kind === "storyboard"
              ? {
                  script_version_id:
                    version?.script_version_id ?? sourceScript?.id,
                }
              : {}),
          }),
        );
        // POST 已成功：先结束草稿并定位精确版本，刷新失败不能退回保存入口。
        setEditing(false);
        await onCreated(created, kind);
      });
    } finally {
      saving.current = false;
    }
  }
  return (
    <>
      <div className="section-heading flex flex-wrap items-center justify-between gap-2">
        <h3>
          {kind === "script" ? "剧本" : "分镜"}{" "}
          {version ? `v${version.version_no}` : "· 人工起稿"}
        </h3>
        {version && <StatusBadge status={version.status} />}
      </div>
      {active && writable && (
        <p className="hint text-xs leading-relaxed text-muted-foreground">
          工作流进行中，先在任务中心完成或取消任务，再另存人工修改版本。
        </p>
      )}
      {writable && version && (
        <Button
          variant="outline"
          size="lg"
          disabled={busy || active}
          onClick={() => {
            setEditing(!editing);
            setDraft(structuredClone(version.content_json));
          }}
        >
          {editing ? "放弃本次编辑" : "编辑并另存新版本"}
        </Button>
      )}
      {kind === "script" ? (
        <div className="document flex min-w-0 flex-col gap-4 break-words">
          {editing ? (
            <>
              <FormLabel>
                剧本标题
                <Input
                  value={script.title}
                  maxLength={160}
                  onChange={(event) =>
                    mutate((value) => {
                      value.title = event.target.value;
                    })
                  }
                />
              </FormLabel>
              <FormLabel>
                梗概
                <Textarea
                  value={script.summary}
                  maxLength={3000}
                  onChange={(event) =>
                    mutate((value) => {
                      value.summary = event.target.value;
                    })
                  }
                />
              </FormLabel>
              <FormLabel>
                角色（用中文逗号分隔）
                <Input
                  value={script.characters.join("，")}
                  onChange={(event) =>
                    mutate((value) => {
                      value.characters = event.target.value
                        .split(/[，,]/)
                        .map((x) => x.trim());
                    })
                  }
                />
              </FormLabel>
            </>
          ) : (
            <>
              <h3>{script.title}</h3>
              <p>{script.summary}</p>
              <p className="muted text-sm text-muted-foreground">
                登场角色：{script.characters.join("、")}
              </p>
            </>
          )}
          {script.scenes.map((scene, index) => (
            <section
              className="scene flex min-w-0 flex-col gap-3 rounded-lg border border-dashed bg-background/70 p-3"
              key={index}
            >
              {editing ? (
                <>
                  <FormLabel>
                    场景 {scene.scene_no}
                    <Input
                      value={scene.title}
                      maxLength={200}
                      onChange={(event) =>
                        mutate((value) => {
                          value.scenes[index].title = event.target.value;
                        })
                      }
                    />
                  </FormLabel>
                  <FormLabel>
                    场景动作
                    <Textarea
                      value={scene.action}
                      maxLength={5000}
                      onChange={(event) =>
                        mutate((value) => {
                          value.scenes[index].action = event.target.value;
                        })
                      }
                    />
                  </FormLabel>
                </>
              ) : (
                <>
                  <h4>
                    {scene.scene_no.toString().padStart(2, "0")} · {scene.title}
                  </h4>
                  <p>{scene.action}</p>
                </>
              )}
              {scene.dialogue.map((line, lineIndex) =>
                editing ? (
                  <div
                    className="dialogue-edit flex flex-col gap-3 rounded-lg bg-muted/50 p-3"
                    key={lineIndex}
                  >
                    <div>
                      <FormLabel
                        htmlFor={`${editorId}-speaker-${index}-${lineIndex}`}
                      >
                        说话角色
                      </FormLabel>
                      <NativeSelect
                        id={`${editorId}-speaker-${index}-${lineIndex}`}
                        value={line.speaker}
                        onChange={(event) =>
                          mutate((value) => {
                            value.scenes[index].dialogue[lineIndex].speaker =
                              event.target.value;
                          })
                        }
                      >
                        {[...new Set([...script.characters, "旁白"])].map(
                          (name) => (
                            <NativeSelectOption key={name}>
                              {name}
                            </NativeSelectOption>
                          ),
                        )}
                      </NativeSelect>
                    </div>
                    <FormLabel>
                      对白
                      <Textarea
                        value={line.text}
                        maxLength={2000}
                        onChange={(event) =>
                          mutate((value) => {
                            value.scenes[index].dialogue[lineIndex].text =
                              event.target.value;
                          })
                        }
                      />
                    </FormLabel>
                    <Button
                      variant="outline"
                      size="lg"
                      onClick={() =>
                        mutate((value) => {
                          value.scenes[index].dialogue.splice(lineIndex, 1);
                        })
                      }
                    >
                      移除对白
                    </Button>
                  </div>
                ) : (
                  <p
                    className="dialogue rounded-lg bg-secondary/40 p-3 text-sm [&_strong]:mr-2"
                    key={lineIndex}
                  >
                    <strong>{line.speaker}</strong>
                    {line.text}
                  </p>
                ),
              )}
              {editing && (
                <div className="button-row flex flex-wrap items-center gap-2">
                  <Button
                    variant="outline"
                    size="lg"
                    onClick={() =>
                      mutate((value) => {
                        value.scenes[index].dialogue.push({
                          speaker: "旁白",
                          text: "",
                        });
                      })
                    }
                  >
                    添加对白
                  </Button>
                  <Button
                    variant="outline"
                    size="lg"
                    disabled={script.scenes.length === 1}
                    onClick={() =>
                      mutate((value) => {
                        value.scenes.splice(index, 1);
                        value.scenes.forEach((row, i) => {
                          row.scene_no = i + 1;
                        });
                      })
                    }
                  >
                    移除此场景
                  </Button>
                </div>
              )}
            </section>
          ))}
          {editing && (
            <Button
              variant="outline"
              size="lg"
              disabled={script.scenes.length >= 50}
              onClick={() =>
                mutate((value) => {
                  value.scenes.push({
                    scene_no: value.scenes.length + 1,
                    title: "",
                    action: "",
                    dialogue: [],
                  });
                })
              }
            >
              添加场景
            </Button>
          )}
        </div>
      ) : (
        <div className="document flex min-w-0 flex-col gap-4 break-words">
          {board.shots.map((shot, index) => (
            <section
              className="scene flex min-w-0 flex-col gap-3 rounded-lg border border-dashed bg-background/70 p-3"
              key={index}
            >
              <h4>镜头 {shot.shot_no.toString().padStart(2, "0")}</h4>
              {editing ? (
                <>
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={`${editorId}-visual-${index}`}>
                      画面描述
                    </FormLabel>
                    <Textarea
                      id={`${editorId}-visual-${index}`}
                      value={shot.visual_description}
                      maxLength={3000}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].visual_description =
                            event.target.value;
                        })
                      }
                    />
                  </Field>
                  <div className="field-pair grid min-w-0 grid-cols-1 items-start gap-4 sm:grid-cols-2">
                    <FormLabel>
                      时长（秒）
                      <Input
                        type="number"
                        min="0.5"
                        max="30"
                        step="0.1"
                        value={shot.duration_ms / 1000}
                        onChange={(event) =>
                          mutate((value) => {
                            value.shots[index].duration_ms = Math.round(
                              Number(event.target.value) * 1000,
                            );
                          })
                        }
                      />
                    </FormLabel>
                    <Field className="editor-field flex min-w-0 flex-col gap-2">
                      <FormLabel htmlFor={`${editorId}-scene-${index}`}>
                        场景编号
                      </FormLabel>
                      {source ? (
                        <NativeSelect
                          id={`${editorId}-scene-${index}`}
                          value={shot.scene_no}
                          onChange={(event) =>
                            mutate((value) => {
                              value.shots[index].scene_no = Number(
                                event.target.value,
                              );
                            })
                          }
                        >
                          {source.scenes.map((scene) => (
                            <NativeSelectOption
                              key={scene.scene_no}
                              value={scene.scene_no}
                            >
                              {scene.scene_no} · {scene.title}
                            </NativeSelectOption>
                          ))}
                        </NativeSelect>
                      ) : (
                        <Input
                          id={`${editorId}-scene-${index}`}
                          type="number"
                          min="1"
                          value={shot.scene_no}
                          onChange={(event) =>
                            mutate((value) => {
                              value.shots[index].scene_no = Number(
                                event.target.value,
                              );
                            })
                          }
                        />
                      )}
                    </Field>
                  </div>
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={`${editorId}-size-${index}`}>
                      景别
                    </FormLabel>
                    <NativeSelect
                      id={`${editorId}-size-${index}`}
                      value={shot.shot_size}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].shot_size = event.target.value;
                        })
                      }
                    >
                      <NativeSelectOption value="wide">远景</NativeSelectOption>
                      <NativeSelectOption value="full">全景</NativeSelectOption>
                      <NativeSelectOption value="medium">
                        中景
                      </NativeSelectOption>
                      <NativeSelectOption value="close_up">
                        特写
                      </NativeSelectOption>
                    </NativeSelect>
                  </Field>
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={`${editorId}-movement-${index}`}>
                      运镜
                    </FormLabel>
                    <NativeSelect
                      id={`${editorId}-movement-${index}`}
                      value={shot.camera_movement}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].camera_movement =
                            event.target.value;
                        })
                      }
                    >
                      <NativeSelectOption value="static">
                        固定
                      </NativeSelectOption>
                      <NativeSelectOption value="push_in">
                        推近
                      </NativeSelectOption>
                      <NativeSelectOption value="pull_out">
                        拉远
                      </NativeSelectOption>
                      <NativeSelectOption value="pan">摇镜</NativeSelectOption>
                    </NativeSelect>
                  </Field>
                  <FormLabel>
                    出场角色（用中文逗号分隔）
                    <Input
                      value={shot.characters.join("，")}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].characters = event.target.value
                            ? event.target.value
                                .split(/[，,]/)
                                .map((name) => name.trim())
                            : [];
                        })
                      }
                    />
                  </FormLabel>
                  {source && (
                    <p className="hint text-xs leading-relaxed text-muted-foreground">
                      可用角色：{source.characters.join("、")}
                      ；留空表示没有角色出镜。
                    </p>
                  )}
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={`${editorId}-subtitle-${index}`}>
                      字幕
                    </FormLabel>
                    <Textarea
                      id={`${editorId}-subtitle-${index}`}
                      value={shot.subtitle}
                      maxLength={2000}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].subtitle = event.target.value;
                        })
                      }
                    />
                  </Field>
                </>
              ) : (
                <>
                  <p>{shot.visual_description}</p>
                  <p className="muted text-sm text-muted-foreground">
                    场景 {shot.scene_no} · {shot.duration_ms / 1000} 秒 ·{" "}
                    {shot.shot_size}
                  </p>
                  {shot.subtitle && <blockquote>{shot.subtitle}</blockquote>}
                </>
              )}
              {editing && (
                <Button
                  variant="outline"
                  size="lg"
                  disabled={board.shots.length === 1}
                  onClick={() =>
                    mutate((value) => {
                      value.shots.splice(index, 1);
                      value.shots.forEach((row, i) => {
                        row.shot_no = i + 1;
                      });
                    })
                  }
                >
                  移除此镜头
                </Button>
              )}
            </section>
          ))}
          {editing && (
            <Button
              variant="outline"
              size="lg"
              disabled={board.shots.length >= 100}
              onClick={() =>
                mutate((value) => {
                  value.shots.push(emptyShot(value.shots.length + 1));
                })
              }
            >
              添加镜头
            </Button>
          )}
        </div>
      )}
      {editing && invalidBoard && (
        <p className="hint text-xs leading-relaxed text-muted-foreground">
          每个镜头都需要画面描述，时长须在 0.5–30 秒之间。
        </p>
      )}
      {editing && (
        <Button
          variant="default"
          size="lg"
          className="wide w-full"
          disabled={busy || active || !writable || invalidBoard}
          onClick={() => void save()}
        >
          {busy
            ? "保存中…"
            : version
              ? "保存为新版本"
              : kind === "script"
                ? "保存人工剧本"
                : "保存人工分镜"}
        </Button>
      )}
    </>
  );
}
