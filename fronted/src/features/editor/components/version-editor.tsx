"use client";

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
import {
  emptyShotPerformance,
  isShotPerformance,
  emptyPerformanceCharacter,
  performanceProblem,
  reorderPerformanceShots,
  type GenerationRoute,
} from "../shot-performance";
import StoryboardPerformanceControls from "./storyboard-performance-controls";
import { api, json } from "@/lib/api/client";
import NarrativeInputs from "./narrative-inputs";
import {
  ScriptNarrativeEditor,
  ScriptNarrativeSummary,
} from "./script-narrative";
import {
  emptyBeat,
  emptyNarrativeRequest,
  isNarrativeRequest,
  isNarrativeSceneDraft,
  narrativeRequestProblem,
  requestFromContext,
  type DramaticBeat,
  type StateChange,
  type NarrativeRequest,
  type NarrativeCapture,
} from "../script-narrative";
import type { ContentVersion, Episode, ExactAssetReference } from "@/types/api";
import ListPagination from "@/components/shared/list-pagination";
import { useReferenceImages } from "@/features/assets/components/asset-reference-picker";

import Dialog from "@/components/shared/app-dialog";
import ShotDetails from "@/features/editor/components/shot-details";
import { useSessionDraft } from "@/providers/draft-provider";
import ShotProductionButton from "@/features/editor/components/shot-production";
import StoryboardTable, {
  type StoryboardTableShot,
} from "@/features/editor/components/storyboard-table";

export type VersionKind = "script" | "storyboard";
export type VersionCreated = (
  version: ContentVersion,
  kind: VersionKind,
) => Promise<void>;
export type Act = (action: () => Promise<unknown>) => Promise<boolean>;
type Script = {
  schema_version?: string;
  narrative_context?: Record<string, unknown>;
  _narrative_request?: NarrativeRequest;
  _capture_signature?: string;
  title: string;
  summary: string;
  estimated_duration_ms?: number | null;
  characters: string[];
  character_assets?: { character: string; asset: unknown }[];
  scenes: {
    scene_no: number;
    scene_id?: string;
    dramatic_beat?: DramaticBeat;
    state_changes?: StateChange[];
    scene_asset?: unknown;
    location_asset?: unknown;
    props?: ExactAssetReference[];
    title: string;
    action: string;
    dialogue: {
      line_id?: string;
      speaker: string;
      text: string;
      line_type?: "dialogue" | "narration";
      emotion?: string;
    }[];
  }[];
};
type Board = {
  schema_version?: string;
  generation_routes?: GenerationRoute[];
  shots: StoryboardTableShot[];
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
      (row.schema_version !== "3.0" ||
        ((row._narrative_request === undefined ||
          isNarrativeRequest(row._narrative_request)) &&
          Array.isArray(row.scenes) &&
          row.scenes.every(isNarrativeSceneDraft))) &&
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
    (row.generation_routes === undefined ||
      (Array.isArray(row.generation_routes) &&
        row.generation_routes.every(
          (route) => route && text(route.provider) && text(route.model),
        ))) &&
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
        text(shot.subtitle) &&
        (row.schema_version !== "3.0" ||
          (shot.schema_version === "3.0" &&
            isShotPerformance(shot.performance))),
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
  const sourceScenes = new Map(
    source?.scenes.map((scene) => [scene.scene_no, scene]),
  );
  function emptyShot(
    shotNo: number,
    performanceMode = false,
  ): Board["shots"][number] {
    const performance = emptyShotPerformance([]);
    performance.props = (source?.scenes[0]?.props ?? []).map((asset) => ({
      asset: asset as { asset_id: string; asset_version_id: string },
      holder_start: "",
      holder_end: "",
      position_start: "",
      position_end: "",
    }));
    return {
      shot_no: shotNo,
      scene_no: source?.scenes[0]?.scene_no ?? 1,
      scene_id: source?.scenes[0]?.scene_id ?? null,
      scene_asset: source?.scenes[0]?.scene_asset ?? null,
      location_asset: source?.scenes[0]?.location_asset ?? null,
      props: source?.scenes[0]?.props ?? [],
      duration_ms:
        shotNo === 1 ? Math.min(episode.target_duration_ms, 30000) : 3000,
      shot_size: "medium",
      visual_description: "",
      characters: [],
      camera_movement: "static",
      subtitle: "",
      ...(performanceMode ? { schema_version: "3.0", performance } : {}),
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
            estimated_duration_ms: episode.target_duration_ms,
            characters: ["主角"],
            scenes: [
              {
                scene_id: crypto.randomUUID(),
                scene_no: 1,
                title: "",
                action: "",
                dialogue: [],
              },
            ],
          },
  );
  const local = useSessionDraft({
    id: JSON.stringify([
      episode.id,
      kind,
      version?.id ?? null,
      version ? null : (sourceScript?.id ?? null),
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
  const [tableView, setTableView] = useState(false);
  function discard() {
    local.clear();
    local.update(structuredClone(initial));
    setEditing(!version);
    setDiscarding(false);
  }
  const saving = useRef(false);
  const script = draft as unknown as Script;
  const board = draft as unknown as Board;
  const showPropReferences =
    kind === "storyboard" &&
    editing &&
    !tableView &&
    board.shots.some((shot) => shot.performance?.props.length);
  const propReferences = useReferenceImages(
    episode.project_id,
    showPropReferences,
  );
  function mutate(change: (value: Script & Board) => void) {
    const copy = structuredClone(draft);
    change(copy as unknown as Script & Board);
    local.update(copy);
  }
  const performanceIssue =
    kind === "storyboard" && board.schema_version === "3.0"
      ? board.shots
          .map((shot) => performanceProblem(shot.performance))
          .find(Boolean)
      : null;
  const invalidBoard =
    kind === "storyboard" &&
    (!!performanceIssue ||
      board.shots.some(
        (shot) =>
          !shot.visual_description.trim() ||
          !Number.isFinite(shot.duration_ms) ||
          shot.duration_ms < 500 ||
          shot.duration_ms > 30000,
      ));
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
        let content = draft;
        if (kind === "script" && script.schema_version === "3.0") {
          const request =
            script._narrative_request ??
            requestFromContext(script.narrative_context ?? {});
          if (!request || narrativeRequestProblem(request))
            throw new Error(
              request
                ? narrativeRequestProblem(request)
                : "请先选择连续剧写作依据。",
            );
          let context = script.narrative_context;
          const signature = JSON.stringify(request);
          if (!context || script._capture_signature !== signature) {
            const captured = await api<NarrativeCapture>(
              `/episodes/${episode.id}/writing-context`,
              json("POST", request),
            );
            context = captured.context;
            local.update({
              ...draft,
              narrative_context: context,
              _narrative_request: request,
              _capture_signature: signature,
            });
          }
          const clean: Record<string, unknown> = {
            ...draft,
            narrative_context: context,
          };
          delete clean._narrative_request;
          delete clean._capture_signature;
          content = clean;
        }
        const created = await api<ContentVersion>(
          `/episodes/${episode.id}/${kind === "script" ? "scripts" : "storyboards"}`,
          json("POST", {
            expected_version_no: latestNo,
            ...(version ? { base_version_id: version.id } : {}),
            content,
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
      {kind === "storyboard" && (
        <legend className="mb-4">
          <div
            className="flex flex-wrap gap-2"
            role="group"
            aria-label="分镜显示方式"
          >
            <Button
              variant="outline"
              aria-pressed={!tableView}
              onClick={() => setTableView(false)}
            >
              镜头详情
            </Button>
            <Button
              variant="outline"
              aria-pressed={tableView}
              onClick={() => setTableView(true)}
            >
              分镜表格
            </Button>
            {version && !editing && (
              <ShotProductionButton
                projectId={episode.project_id}
                episodeId={episode.id}
                storyboardId={version.id}
              />
            )}
          </div>
        </legend>
      )}
      {kind === "storyboard" && (
        <p className="text-sm text-muted-foreground" aria-live="polite">
          镜头总时长：
          {board.shots.reduce((total, shot) => total + shot.duration_ms, 0) /
            1000}{" "}
          秒 · 目标 {episode.target_duration_ms / 1000}{" "}
          秒，保存及批准由服务端校验。
        </p>
      )}
      {kind === "storyboard" && (
        <StoryboardPerformanceControls
          schemaVersion={board.schema_version}
          routes={board.generation_routes ?? []}
          editing={editing}
          sourceLoaded={!!source}
          onRoutes={(generation_routes) =>
            mutate((value) => {
              value.generation_routes = generation_routes;
            })
          }
          onEnable={() =>
            mutate((value) => {
              value.schema_version = "3.0";
              value.generation_routes = [];
              const previous = new Map<number, number>();
              value.shots = value.shots.map((shot) => {
                const scene = source?.scenes.find(
                  (row) => row.scene_no === shot.scene_no,
                );
                const performance = emptyShotPerformance(
                  shot.characters,
                  shot.dialogue_line_ids,
                  scene?.dialogue,
                );
                performance.continuity_from_shot =
                  previous.get(shot.scene_no) ?? null;
                previous.set(shot.scene_no, shot.shot_no);
                performance.props = (shot.props ?? []).map((asset) => ({
                  asset: asset as {
                    asset_id: string;
                    asset_version_id: string;
                  },
                  holder_start: "",
                  holder_end: "",
                  position_start: "",
                  position_end: "",
                }));
                return { ...shot, schema_version: "3.0", performance };
              });
            })
          }
        />
      )}
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
              <Field>
                <FormLabel htmlFor={`${editorId}-estimated-duration`}>
                  剧本估算时长（秒）
                </FormLabel>
                <Input
                  id={`${editorId}-estimated-duration`}
                  type="number"
                  min="0.001"
                  step="0.001"
                  value={
                    script.estimated_duration_ms == null
                      ? ""
                      : script.estimated_duration_ms / 1000
                  }
                  onChange={(event) =>
                    mutate((value) => {
                      value.estimated_duration_ms =
                        event.target.value === ""
                          ? null
                          : Math.round(Number(event.target.value) * 1000);
                    })
                  }
                />
                <small className="text-muted-foreground">
                  目标 {episode.target_duration_ms / 1000}{" "}
                  秒，允许范围由服务端配置；估算值不是实测配音时长。旧稿缺失时需填写后另存。
                </small>
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
              <p className="text-sm text-muted-foreground">
                剧本估算时长：
                {script.estimated_duration_ms == null
                  ? "旧稿未填写"
                  : `${script.estimated_duration_ms / 1000} 秒`}{" "}
                · 目标 {episode.target_duration_ms / 1000} 秒
              </p>
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
                            value.scenes[index].dialogue[lineIndex].line_type =
                              event.target.value === "旁白"
                                ? "narration"
                                : "dialogue";
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
                    <FormLabel>
                      台词情绪
                      <Input
                        value={line.emotion ?? ""}
                        maxLength={300}
                        onChange={(event) =>
                          mutate((value) => {
                            value.scenes[index].dialogue[lineIndex].emotion =
                              event.target.value;
                          })
                        }
                      />
                    </FormLabel>
                    <small className="text-xs text-muted-foreground">
                      第 {lineIndex + 1} 句 ·{" "}
                      {line.line_type === "narration" ? "旁白" : "对白"}
                    </small>
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
                          line_id: crypto.randomUUID(),
                          speaker: "旁白",
                          line_type: "narration",
                          emotion: "",
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
                    scene_id: crypto.randomUUID(),
                    scene_no: value.scenes.length + 1,
                    title: "",
                    action: "",
                    dialogue: [],
                    ...(script.schema_version === "3.0"
                      ? {
                          dramatic_beat: emptyBeat(value.scenes.length + 1),
                          state_changes: [],
                        }
                      : {}),
                  });
                })
              }
            >
              添加场景
            </Button>
          )}
        </div>
      ) : tableView ? (
        <StoryboardTable
          key={editing ? "editing" : "reading"}
          shots={board.shots}
          editable={editing && writable && !busy && !active}
          onChange={(shots) =>
            mutate((value) => {
              value.shots = shots;
            })
          }
        />
      ) : (
        <div className="document flex min-w-0 flex-col gap-4 break-words">
          {showPropReferences &&
            (propReferences.loading ||
              propReferences.error ||
              propReferences.hasMore) && (
              <ListPagination
                label="接续道具参考"
                count={propReferences.rows.length}
                {...propReferences}
              />
            )}
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
                              const scene = source?.scenes.find(
                                (row) =>
                                  row.scene_no === Number(event.target.value),
                              );
                              if (value.shots[index].performance) {
                                const current = value.shots[index].performance!;
                                current.dialogue = [];
                                current.props = (scene?.props ?? []).map(
                                  (asset) => ({
                                    asset: asset as {
                                      asset_id: string;
                                      asset_version_id: string;
                                    },
                                    holder_start: "",
                                    holder_end: "",
                                    position_start: "",
                                    position_end: "",
                                  }),
                                );
                              }
                              Object.assign(value.shots[index], {
                                scene_id: scene?.scene_id ?? null,
                                dialogue_line_ids: [],
                                scene_asset: scene?.scene_asset ?? null,
                                location_asset: scene?.location_asset ?? null,
                                props: scene?.props ?? [],
                              });
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
                          value={shot.scene_no}
                          disabled
                          title="精确来源剧本尚未加载，暂不能更改场景"
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
                      disabled={!!shot.performance && !source}
                      onChange={(event) =>
                        mutate((value) => {
                          value.shots[index].characters = event.target.value
                            ? event.target.value
                                .split(/[，,]/)
                                .map((name) => name.trim())
                            : [];
                          const current = value.shots[index];
                          if (current.performance) {
                            const performance = current.performance;
                            performance.characters = current.characters.map(
                              (character) =>
                                performance.characters.find(
                                  (row) => row.character === character,
                                ) ?? emptyPerformanceCharacter(character),
                            );
                            performance.interactions =
                              performance.interactions.filter(
                                (row) =>
                                  current.characters.includes(row.actor) &&
                                  current.characters.includes(row.target),
                              );
                          }
                          const details = current.character_details ?? [];
                          current.character_details = current.characters.map(
                            (character) => {
                              const existing = details.find(
                                (row) => row.character === character,
                              );
                              const binding = source?.character_assets?.find(
                                (row) => row.character === character,
                              );
                              return (
                                existing ?? {
                                  character,
                                  ...(binding ? { asset: binding.asset } : {}),
                                }
                              );
                            },
                          );
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
              <ShotDetails
                shot={shot}
                editing={editing}
                referenceImages={propReferences.rows.filter((row) =>
                  sourceScenes
                    .get(shot.scene_no)
                    ?.props?.some(
                      (prop) =>
                        prop.asset_id === row.asset_id &&
                        prop.asset_version_id === row.id,
                    ),
                )}
                canAddProp={!!sourceScenes.get(shot.scene_no)?.props?.length}
                lines={sourceScenes.get(shot.scene_no)?.dialogue}
                update={(patch) =>
                  mutate((value) => {
                    Object.assign(value.shots[index], patch);
                  })
                }
              />
              {editing && (
                <Button
                  variant="outline"
                  size="lg"
                  disabled={
                    board.shots.length === 1 ||
                    board.shots.some(
                      (row) =>
                        row.performance?.continuity_from_shot === shot.shot_no,
                    )
                  }
                  onClick={() =>
                    mutate((value) => {
                      value.shots.splice(index, 1);
                      value.shots = reorderPerformanceShots(value.shots);
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
                  const shot = emptyShot(
                    value.shots.length + 1,
                    value.schema_version === "3.0",
                  );
                  if (shot.performance) {
                    shot.performance.continuity_from_shot =
                      [...value.shots]
                        .reverse()
                        .find((row) => row.scene_no === shot.scene_no)
                        ?.shot_no ?? null;
                  }
                  value.shots.push(shot);
                })
              }
            >
              添加镜头
            </Button>
          )}
        </div>
      )}
      {kind === "script" && editing && script.schema_version !== "3.0" && (
        <Button
          type="button"
          variant="outline"
          onClick={() =>
            mutate((value) => {
              value.schema_version = "3.0";
              value.narrative_context = {};
              value._narrative_request = emptyNarrativeRequest();
              value.scenes.forEach((scene, index) => {
                scene.dramatic_beat = emptyBeat(index + 1);
                scene.state_changes = [];
              });
            })
          }
        >
          升级为连续剧剧本
        </Button>
      )}
      {kind === "script" && script.schema_version === "3.0" && editing && (
        <>
          <NarrativeInputs
            episode={episode}
            required
            value={
              script._narrative_request ??
              requestFromContext(script.narrative_context ?? {}) ??
              emptyNarrativeRequest()
            }
            disabled={busy || active || !writable}
            onChange={(request) =>
              mutate((value) => {
                value._narrative_request = request ?? emptyNarrativeRequest();
                value.narrative_context = {};
                delete value._capture_signature;
              })
            }
          />
          <ScriptNarrativeEditor
            projectId={episode.project_id}
            scenes={script.scenes}
            onChange={(index, scene) =>
              mutate((value) => {
                value.scenes[index] = { ...value.scenes[index], ...scene };
              })
            }
          />
        </>
      )}
      {kind === "script" && !editing && (
        <ScriptNarrativeSummary content={draft} />
      )}
      {editing && invalidBoard && (
        <p className="hint text-xs leading-relaxed text-muted-foreground">
          {performanceIssue ||
            "每个镜头都需要画面描述，时长须在 0.5–30 秒之间。"}
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
