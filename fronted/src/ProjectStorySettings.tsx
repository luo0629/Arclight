import { useId, useRef, useState, type FormEvent } from "react";
import { Field, FieldGroup, FieldSet } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { api, json, message, type Project } from "./api";

function values(project: Project) {
  const brief = project.story_settings;
  return {
    description: brief.description,
    genre: brief.genre,
    target_audience: brief.target_audience,
    content_restrictions: brief.content_restrictions,
    duration: String(brief.default_episode_duration_ms / 1000),
    count: String(brief.target_episode_count),
    episode_plan: brief.episode_plan,
  };
}
const textFields = [
  { key: "description", label: "项目简介", max: 4000, multiline: true },
  { key: "genre", label: "题材", max: 100, multiline: false },
  { key: "target_audience", label: "目标受众", max: 500, multiline: false },
  {
    key: "content_restrictions",
    label: "内容限制",
    max: 4000,
    multiline: true,
  },
  { key: "episode_plan", label: "分集规划输入", max: 4000, multiline: true },
] as const;

export default function ProjectStorySettings({
  project,
  disabled,
  onBusyChange,
  onSaved,
}: {
  project: Project;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onSaved: (project: Project) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(() => values(project));
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const locked = disabled || busy || project.archived;
  function update(field: keyof typeof draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setNotice("");
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || saving.current) return;
    const milliseconds = Number(draft.duration) * 1000;
    const duration = Math.round(milliseconds);
    const count = Number(draft.count);
    if (
      !draft.duration.trim() ||
      !Number.isFinite(milliseconds) ||
      Math.abs(milliseconds - duration) > 0.000001 ||
      duration < 2500 ||
      duration > 1800000 ||
      !Number.isInteger(count) ||
      count < 1 ||
      count > 1000
    ) {
      setError(
        "默认单集时长须为 2.5–1800 秒，最多三位小数；目标集数须为 1–1000 的整数。",
      );
      return;
    }
    if (
      textFields.some(
        (field) => Array.from(draft[field.key].trim()).length > field.max,
      )
    ) {
      setError("业务设定文本超过长度限制，请缩短后保存。");
      return;
    }
    saving.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    setNotice("");
    try {
      const updated = await api<Project>(
        "/projects/" + project.id,
        json("PATCH", {
          story_settings: {
            schema_version: 1,
            description: draft.description.trim(),
            genre: draft.genre.trim(),
            target_audience: draft.target_audience.trim(),
            content_restrictions: draft.content_restrictions.trim(),
            episode_plan: draft.episode_plan.trim(),
            default_episode_duration_ms: duration,
            target_episode_count: count,
          },
        }),
      );
      onSaved(updated);
      setDraft(values(updated));
      setNotice("项目业务设定已保存");
    } catch (error) {
      setError(
        error instanceof TypeError
          ? "网络连接失败，请检查连接后重试。输入内容已保留。"
          : message(error),
      );
    } finally {
      saving.current = false;
      setBusy(false);
      onBusyChange(false);
    }
  }
  return (
    <section
      className="settings-section studio-settings-section flex min-w-0 flex-col gap-5"
      aria-labelledby={id + "-heading"}
    >
      <h3 id={id + "-heading"}>项目业务设定</h3>
      <p className="text-sm text-muted-foreground">
        设定会随新编剧任务保存为输入快照；不会改写已有版本。默认时长仅用于新建剧集，目标集数和规划不会自动创建剧集或大纲。内容限制作为创作输入，不代替自动内容审核。
      </p>
      <form onSubmit={save}>
        <FieldSet disabled={locked}>
          <FieldGroup className="studio-settings-fields">
            {textFields.map((field) => (
              <Field
                key={field.key}
                data-disabled={locked}
                className={field.multiline ? "studio-field-wide" : undefined}
              >
                <FormLabel htmlFor={id + "-" + field.key}>
                  {field.label}
                </FormLabel>
                {field.multiline ? (
                  <Textarea
                    id={id + "-" + field.key}
                    value={draft[field.key]}
                    rows={3}
                    maxLength={field.max}
                    onChange={(event) => update(field.key, event.target.value)}
                  />
                ) : (
                  <Input
                    id={id + "-" + field.key}
                    value={draft[field.key]}
                    maxLength={field.max}
                    onChange={(event) => update(field.key, event.target.value)}
                  />
                )}
              </Field>
            ))}
            <Field>
              <FormLabel htmlFor={id + "-duration"}>
                默认单集时长（秒）
              </FormLabel>
              <Input
                id={id + "-duration"}
                type="number"
                min="2.5"
                max="1800"
                step="0.001"
                required
                value={draft.duration}
                onChange={(event) => update("duration", event.target.value)}
              />
            </Field>
            <Field>
              <FormLabel htmlFor={id + "-count"}>目标集数</FormLabel>
              <Input
                id={id + "-count"}
                type="number"
                min="1"
                max="1000"
                step="1"
                required
                value={draft.count}
                onChange={(event) => update("count", event.target.value)}
              />
            </Field>
            {error && (
              <Notice tone="error" role="alert">
                {error}
              </Notice>
            )}
            {notice && (
              <Notice tone="success" role="status">
                {notice}
              </Notice>
            )}
            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={locked}>
                {busy ? "保存中…" : "保存业务设定"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={locked}
                onClick={() => {
                  setDraft(values(project));
                  setError("");
                  setNotice("");
                }}
              >
                恢复已保存的业务设定
              </Button>
            </div>
          </FieldGroup>
        </FieldSet>
      </form>
    </section>
  );
}
