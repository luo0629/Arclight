import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import { api, json, message, type ContentVersion, type Episode } from "./api";
import Dialog from "./Dialog";
import { useDraftStore, useSessionDraft } from "./Drafts";

type Scene = {
  scene_id: string;
  scene_no: number;
  title: string;
  action: string;
  dialogue: { line_id: string; speaker: string; text: string }[];
};
type Draft = {
  scope: string;
  first: string;
  last: string;
  field: string;
  start: number;
  end: number;
  instruction: string;
  tone: string;
  dialogueLimit: string;
  narrationLimit: string;
  fingerprint: string;
  key: string;
};
const draftFields = [
  "scope",
  "first",
  "last",
  "field",
  "instruction",
  "tone",
  "dialogueLimit",
  "narrationLimit",
  "fingerprint",
  "key",
];
function validDraft(value: unknown): value is Draft {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    draftFields.every((key) => typeof row[key] === "string") &&
    ["scene", "chapter", "paragraph"].includes(String(row.scope)) &&
    Number.isSafeInteger(row.start) &&
    Number.isSafeInteger(row.end)
  );
}

export default function ScriptRewrite({
  version,
  episode,
  writable,
  busy,
  active,
  reload,
  showTasks,
  resume = false,
}: {
  version: ContentVersion;
  episode: Episode;
  writable: boolean;
  busy: boolean;
  active: boolean;
  reload: () => Promise<void>;
  showTasks: () => void;
  resume?: boolean;
}) {
  const [open, setOpen] = useState(resume);
  const scenes = version.content_json.scenes as Scene[];
  const draftId = JSON.stringify([episode.id, "rewrite", version.id]);
  const { write } = useDraftStore();
  const local = useSessionDraft<Draft>({
    id: draftId,
    episodeId: episode.id,
    label: `剧本 v${version.version_no} 局部改写`,
    initial: {
      scope: "scene",
      first: scenes[0].scene_id,
      last: scenes[0].scene_id,
      field: "action",
      start: 0,
      end: Array.from(scenes[0].action).length,
      instruction: "",
      tone: "",
      dialogueLimit: "",
      narrationLimit: "",
      fingerprint: "",
      key: "",
    },
    validate: validDraft,
  });
  const [submitting, setSubmitting] = useState(false);
  const [accepted, setAccepted] = useState("");
  const [error, setError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const saving = useRef(false);
  const live = useRef(true);
  const id = useId();
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const draft = local.value;
  const scene = scenes.find((row) => row.scene_id === draft.first);
  const text =
    draft.field === "action"
      ? (scene?.action ?? "")
      : (scene?.dialogue.find((line) => line.line_id === draft.field)?.text ??
        "");
  const firstIndex = scenes.findIndex((row) => row.scene_id === draft.first);
  const lastIndex = scenes.findIndex((row) => row.scene_id === draft.last);
  const validSelection =
    !!scene &&
    (draft.scope !== "chapter" || lastIndex >= firstIndex) &&
    (draft.scope !== "paragraph" ||
      (draft.end > draft.start &&
        draft.start >= 0 &&
        draft.end <= Array.from(text).length));
  const disabled = !writable || busy || submitting || active || !!accepted;
  const options = scenes.map((row) => ({
    value: row.scene_id,
    label: `${row.scene_no} · ${row.title}`,
  }));
  function update(patch: Partial<Draft>) {
    local.update({ ...draft, ...patch });
  }
  async function refresh() {
    try {
      await reload();
      if (live.current) setRefreshError("");
    } catch (error) {
      if (live.current) setRefreshError(message(error));
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (disabled || saving.current || !validSelection) return;
    saving.current = true;
    setSubmitting(true);
    setError("");
    const target =
      draft.scope === "chapter"
        ? {
            scope: "chapter",
            scene_ids: scenes
              .slice(firstIndex, lastIndex + 1)
              .map((row) => row.scene_id),
          }
        : draft.scope === "scene"
          ? { scope: "scene", scene_id: draft.first }
          : {
              scope: "paragraph",
              scene_id: draft.first,
              field: draft.field === "action" ? "action" : "dialogue",
              line_id: draft.field === "action" ? null : draft.field,
              start: draft.start,
              end: draft.end,
            };
    const body = {
      base_version_id: version.id,
      target,
      instructions: {
        instruction: draft.instruction,
        tone: draft.tone,
        dialogue_max_chars: draft.dialogueLimit
          ? Number(draft.dialogueLimit)
          : null,
        narration_max_percent: draft.narrationLimit
          ? Number(draft.narrationLimit)
          : null,
      },
    };
    const fingerprint = JSON.stringify(body);
    const key =
      draft.fingerprint === fingerprint && draft.key
        ? draft.key
        : crypto.randomUUID();
    const submitted = { ...draft, fingerprint, key };
    local.update(submitted);
    try {
      const response = await api<{ run_id: string }>(
        `/episodes/${episode.id}/script-rewrite-runs`,
        {
          ...json("POST", body),
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": key,
          },
        },
      );
      if (!live.current) return;
      setAccepted(response.run_id);
      write(draftId, undefined, submitted);
      await refresh();
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      saving.current = false;
      if (live.current) setSubmitting(false);
    }
  }
  return (
    <>
      <Button
        variant="outline"
        size="lg"
        disabled={!writable || busy || active}
        onClick={() => setOpen(true)}
      >
        局部改写此版本
      </Button>
      {open && (
        <Dialog
          labelledBy={id + "title"}
          busy={submitting}
          onDismiss={() => setOpen(false)}
        >
          <h2 id={id + "title"}>局部改写 · 剧本 v{version.version_no}</h2>
          <p className="text-sm text-muted-foreground">
            选择要修改的范围。结果另存为待审核剧本，批准后可从新版本生成分镜。
          </p>
          <p className="text-xs text-muted-foreground break-all">
            来源版本：{version.id}
          </p>
          {local.recovered && <Notice>已恢复此精确版本的本机改写草稿。</Notice>}
          {local.invalid && (
            <Notice tone="error">
              本机改写草稿损坏，已使用此服务器版本重新起稿。
            </Notice>
          )}
          {!writable && <Notice>当前项目或剧集只允许查看。</Notice>}
          {active && !accepted && (
            <Notice>已有未完成任务，请先完成或取消任务。</Notice>
          )}
          {error && <Notice tone="error">{error}</Notice>}
          {accepted ? (
            <div className="flex flex-col gap-3">
              <Notice>改写任务已提交，结果将保存为独立待审核版本。</Notice>
              <p className="text-xs break-all">任务：{accepted}</p>
              {refreshError && (
                <Notice tone="error">
                  任务已提交；工作区刷新失败：{refreshError}
                  <Button variant="outline" onClick={() => void refresh()}>
                    重试刷新工作区
                  </Button>
                </Notice>
              )}
              <Button onClick={showTasks}>前往任务与审核</Button>
            </div>
          ) : (
            <form
              onSubmit={(event) => void submit(event)}
              className="flex flex-col gap-5"
            >
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor={id + "scope"}>改写范围</FieldLabel>
                  <ChoiceSelect
                    id={id + "scope"}
                    value={draft.scope}
                    disabled={disabled}
                    onValueChange={(scope) => update({ scope })}
                    options={[
                      { value: "scene", label: "单个场景" },
                      { value: "chapter", label: "章节（连续场景）" },
                      { value: "paragraph", label: "选定段落" },
                    ]}
                  />
                  <FieldDescription>
                    章节以本剧本中连续的场景表示。
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor={id + "first"}>
                    {draft.scope === "chapter" ? "起始场景" : "改写场景"}
                  </FieldLabel>
                  <ChoiceSelect
                    id={id + "first"}
                    value={draft.first}
                    disabled={disabled}
                    options={options}
                    onValueChange={(first) => {
                      const next = scenes.find(
                        (row) => row.scene_id === first,
                      )!;
                      update({
                        first,
                        last: first,
                        field: "action",
                        start: 0,
                        end: Array.from(next.action).length,
                      });
                    }}
                  />
                </Field>
                {draft.scope === "chapter" && (
                  <Field>
                    <FieldLabel htmlFor={id + "last"}>结束场景</FieldLabel>
                    <ChoiceSelect
                      id={id + "last"}
                      value={draft.last}
                      disabled={disabled}
                      onValueChange={(last) => update({ last })}
                      options={options.map((option, index) => ({
                        ...option,
                        disabled: index < firstIndex,
                      }))}
                    />
                  </Field>
                )}
                {draft.scope === "paragraph" && (
                  <>
                    <Field>
                      <FieldLabel htmlFor={id + "field"}>
                        段落所在字段
                      </FieldLabel>
                      <ChoiceSelect
                        id={id + "field"}
                        value={draft.field}
                        disabled={disabled}
                        options={[
                          { value: "action", label: "动作描述" },
                          ...(scene?.dialogue ?? []).map((line, index) => ({
                            value: line.line_id,
                            label: `台词 ${index + 1} · ${line.speaker}`,
                          })),
                        ]}
                        onValueChange={(field) => {
                          const next =
                            field === "action"
                              ? (scene?.action ?? "")
                              : (scene?.dialogue.find(
                                  (line) => line.line_id === field,
                                )?.text ?? "");
                          update({
                            field,
                            start: 0,
                            end: Array.from(next).length,
                          });
                        }}
                      />
                    </Field>
                    <Field>
                      <FieldLabel htmlFor={id + "text"}>
                        原文（可选中要改写的段落）
                      </FieldLabel>
                      <Textarea
                        id={id + "text"}
                        readOnly
                        disabled={disabled}
                        value={text}
                        onSelect={(event) => {
                          const input = event.currentTarget;
                          if (input.selectionEnd > input.selectionStart)
                            update({
                              start: Array.from(
                                text.slice(0, input.selectionStart),
                              ).length,
                              end: Array.from(text.slice(0, input.selectionEnd))
                                .length,
                            });
                        }}
                      />
                      <FieldDescription>
                        默认改写此字段全文；用鼠标或键盘选中文字可缩小范围。范围按
                        Unicode 字符计数。
                      </FieldDescription>
                    </Field>
                    <p className="text-xs text-muted-foreground">
                      选中第 {draft.start + 1}–{draft.end} 字
                    </p>
                    <pre
                      aria-label="选定改写原文"
                      className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-3 text-sm"
                    >
                      {Array.from(text).slice(draft.start, draft.end).join("")}
                    </pre>
                  </>
                )}
                <Field>
                  <FieldLabel htmlFor={id + "instruction"}>改写指令</FieldLabel>
                  <Textarea
                    id={id + "instruction"}
                    value={draft.instruction}
                    disabled={disabled}
                    maxLength={2000}
                    placeholder="例如：强化冲突，保留人物动机"
                    onChange={(event) =>
                      update({ instruction: event.target.value })
                    }
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor={id + "tone"}>语气要求</FieldLabel>
                  <Input
                    id={id + "tone"}
                    value={draft.tone}
                    disabled={disabled}
                    maxLength={200}
                    placeholder="例如：克制、悬疑"
                    onChange={(event) => update({ tone: event.target.value })}
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor={id + "limit"}>
                    单条对白字数上限（可选）
                  </FieldLabel>
                  <Input
                    id={id + "limit"}
                    type="number"
                    min={1}
                    max={2000}
                    step={1}
                    value={draft.dialogueLimit}
                    disabled={disabled}
                    onChange={(event) =>
                      update({ dialogueLimit: event.target.value })
                    }
                  />
                  <FieldDescription>
                    校验修改后的完整角色对白，包括选区以外的前后缀。
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor={id + "ratio"}>
                    旁白字数占比上限（%，可选）
                  </FieldLabel>
                  <Input
                    id={id + "ratio"}
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={draft.narrationLimit}
                    disabled={disabled}
                    onChange={(event) =>
                      update({ narrationLimit: event.target.value })
                    }
                  />
                  <FieldDescription>
                    范围内旁白字数 ÷ 全部台词字数；动作描述不参与计算。
                  </FieldDescription>
                </Field>
              </FieldGroup>
              {!validSelection && (
                <Notice tone="error">
                  选区已失效，请重新选择本版本的场景和段落。
                </Notice>
              )}
              <p className="text-xs text-muted-foreground">
                新任务使用账号当前文字服务，可能产生模型费用。Mock
                仅演示流程；语气与创作质量需要人工审核。
              </p>
              <Button
                type="submit"
                disabled={
                  disabled ||
                  !validSelection ||
                  !(
                    draft.instruction.trim() ||
                    draft.tone.trim() ||
                    draft.dialogueLimit ||
                    draft.narrationLimit
                  )
                }
              >
                {submitting ? "正在提交改写…" : "提交局部改写"}
              </Button>
            </form>
          )}
          <Button
            variant="outline"
            disabled={submitting}
            onClick={() => setOpen(false)}
          >
            关闭改写
          </Button>
        </Dialog>
      )}
    </>
  );
}
