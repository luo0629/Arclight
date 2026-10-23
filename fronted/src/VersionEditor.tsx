import { Notice, StatusBadge, FormLabel } from "@/components/ui-kit";
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

import Dialog from "./Dialog";
import { useSessionDraft } from "./Drafts";

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

function validContent(value: unknown, kind: VersionKind): boolean {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  const strings = (v: unknown): v is string[] =>
    Array.isArray(v) && v.every((x) => typeof x === "string");
  const text = (v: unknown) => typeof v === "string";
  const number = (v: unknown) => typeof v === "number" && Number.isFinite(v);
  if (kind === "script")
    return (
      text(row.title) &&
      text(row.summary) &&
      strings(row.characters) &&
      Array.isArray(row.scenes) &&
      row.scenes.length > 0 &&
      row.scenes.every(
        (scene) =>
          scene &&
          number(scene.scene_no) &&
          text(scene.title) &&
          text(scene.action) &&
          Array.isArray(scene.dialogue) &&
          scene.dialogue.every(
            (line: Record<string, unknown>) =>
              line && text(line.speaker) && text(line.text),
          ),
      )
    );
  return (
    Array.isArray(row.shots) &&
    row.shots.length > 0 &&
    row.shots.every(
      (shot) =>
        shot &&
        number(shot.shot_no) &&
        number(shot.scene_no) &&
        number(shot.duration_ms) &&
        text(shot.shot_size) &&
        text(shot.visual_description) &&
        strings(shot.characters) &&
        text(shot.camera_movement) &&
        text(shot.subtitle),
    )
  );
}

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
  const [initial] = useState<Record<string, unknown>>(() =>
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
  const local = useSessionDraft({
    id: JSON.stringify([
      episode.id,
      kind,
      version?.id ?? null,
      sourceScript?.id ?? null,
    ]),
    episodeId: episode.id,
    label: version
      ? `${kind === "script" ? "剧本" : "分镜"} v${version.version_no} 的修改`
      : kind === "script"
        ? "人工剧本起稿"
        : `人工分镜起稿（来源剧本 v${sourceScript?.version_no}）`,
    initial,
    validate: (value): value is Record<string, unknown> =>
      validContent(value, kind),
  });
  const draft = local.value;
  const [editing, setEditing] = useState(!version || local.recovered);
  const [discarding, setDiscarding] = useState(false);
  function discard() {
    local.clear();
    local.update(structuredClone(initial));
    setEditing(!version);
    setDiscarding(false);
  }
  const saving = useRef(false);
  const script = draft as unknown as Script;
  const board = draft as unknown as Board;
  function mutate(change: (value: Script & Board) => void) {
    const copy = structuredClone(draft);
    change(copy as unknown as Script & Board);
    local.update(copy);
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
            ...(version ? { base_version_id: version.id } : {}),
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
        local.clear();
        setEditing(false);
        await onCreated(created, kind);
      });
    } finally {
      saving.current = false;
    }
  }
  return (
    <fieldset
      disabled={busy || !writable || active}
      className="flex min-w-0 flex-col gap-4"
    >
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
            if (editing && local.dirty) setDiscarding(true);
            else setEditing(!editing);
          }}
        >
          {editing ? "放弃本次编辑" : "编辑并另存新版本"}
        </Button>
      )}
      {local.invalid && (
        <Notice tone="error" role="alert">
          本地草稿格式损坏，未自动恢复。当前显示原稿；可放弃草稿后重新编辑。
        </Notice>
      )}
      {local.dirty && (
        <Notice tone="info" role="status" className="draft-notice">
          {local.recovered ? "已恢复未保存草稿。" : "未保存草稿已暂存。"}
          仅保留在当前浏览器标签页，切换后重新打开相同版本或起稿入口即可恢复；不代表已保存到服务器，关闭标签页会清除。
          {!version && (
            <Button
              variant="outline"
              onClick={() => setDiscarding(true)}
              disabled={busy}
            >
              放弃草稿
            </Button>
          )}
        </Notice>
      )}
      {discarding && (
        <Dialog
          labelledBy={`${editorId}-discard`}
          busy={busy}
          onDismiss={() => setDiscarding(false)}
        >
          <h2 id={`${editorId}-discard`}>放弃未保存草稿？</h2>
          <p>此操作会清除本标签页的这份草稿，服务器上的历史版本不受影响。</p>
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="outline" onClick={() => setDiscarding(false)}>
              继续编辑
            </Button>
            <Button variant="destructive" onClick={discard}>
              确认放弃草稿
            </Button>
          </div>
        </Dialog>
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
              <Field className="flex min-w-0 flex-col gap-2">
                <FormLabel htmlFor={`${editorId}-summary`}>梗概</FormLabel>
                <Textarea
                  id={`${editorId}-summary`}
                  value={script.summary}
                  maxLength={3000}
                  onChange={(event) =>
                    mutate((value) => {
                      value.summary = event.target.value;
                    })
                  }
                />
              </Field>
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
                  <Field className="flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={`${editorId}-action-${index}`}>
                      场景动作
                    </FormLabel>
                    <Textarea
                      id={`${editorId}-action-${index}`}
                      value={scene.action}
                      maxLength={5000}
                      onChange={(event) =>
                        mutate((value) => {
                          value.scenes[index].action = event.target.value;
                        })
                      }
                    />
                  </Field>
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
                    <Field className="flex min-w-0 flex-col gap-2">
                      <FormLabel
                        htmlFor={`${editorId}-dialogue-${index}-${lineIndex}`}
                      >
                        对白
                      </FormLabel>
                      <Textarea
                        id={`${editorId}-dialogue-${index}-${lineIndex}`}
                        value={line.text}
                        maxLength={2000}
                        onChange={(event) =>
                          mutate((value) => {
                            value.scenes[index].dialogue[lineIndex].text =
                              event.target.value;
                          })
                        }
                      />
                    </Field>
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
    </fieldset>
  );
}
