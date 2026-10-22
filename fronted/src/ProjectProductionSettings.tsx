import { FieldGroup, FieldSet, Field } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useId, useRef, useState, type FormEvent } from "react";
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectGroup,
  SelectItem,
} from "@/components/ui/select";
import { api, json, message, type Project } from "./api";

const presets = [
  {
    value: "portrait",
    label: "竖屏 9:16 · 1080 × 1920",
    width: 1080,
    height: 1920,
  },
  {
    value: "landscape",
    label: "横屏 16:9 · 1920 × 1080",
    width: 1920,
    height: 1080,
  },
  {
    value: "square",
    label: "方形 1:1 · 1080 × 1080",
    width: 1080,
    height: 1080,
  },
];
const numericText = (value: unknown) =>
  typeof value === "number" || typeof value === "string" ? String(value) : "";
function values(project: Project) {
  return {
    description:
      typeof project.style_guide.description === "string"
        ? project.style_guide.description
        : "",
    width: numericText(project.output_profile.width),
    height: numericText(project.output_profile.height),
    fps: numericText(project.output_profile.fps),
  };
}
function presetFor(project: Project) {
  return (
    presets.find(
      (item) =>
        item.width === project.output_profile.width &&
        item.height === project.output_profile.height,
    )?.value ?? "custom"
  );
}

export default function ProjectProductionSettings({
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
  const [preset, setPreset] = useState(() => presetFor(project));
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const locked = disabled || busy || project.archived;
  const legacyDescription =
    Object.hasOwn(project.style_guide, "description") &&
    typeof project.style_guide.description !== "string";
  const width = Number(draft.width),
    height = Number(draft.height);
  const validSize =
    Number.isInteger(width) &&
    Number.isInteger(height) &&
    width >= 1 &&
    width <= 8192 &&
    height >= 1 &&
    height <= 8192;

  function restore() {
    setDraft(values(project));
    setPreset(presetFor(project));
    setError("");
    setNotice("");
  }
  function choose(value: string) {
    setPreset(value);
    setNotice("");
    const selected = presets.find((item) => item.value === value);
    if (selected)
      setDraft((current) => ({
        ...current,
        width: String(selected.width),
        height: String(selected.height),
      }));
  }
  function update(field: keyof typeof draft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setNotice("");
    if (field === "width" || field === "height") setPreset("custom");
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current || locked) return;
    const fps = Number(draft.fps);
    if (
      !validSize ||
      !Number.isFinite(fps) ||
      fps < 1 ||
      fps > 120 ||
      Math.abs(fps * 1000 - Math.round(fps * 1000)) > 0.000001
    ) {
      setError("宽高须为 1–8192 的整数像素；帧率须为 1–120，最多三位小数。");
      return;
    }
    if (!legacyDescription && draft.description.length > 10000) {
      setError("视觉风格说明不能超过 10000 个字符。");
      return;
    }
    saving.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    setNotice("");
    try {
      // PATCH 替换整个对象，因此必须保留表单未管理的键及旧格式内容。
      const styleGuide = { ...project.style_guide };
      if (
        !legacyDescription &&
        draft.description !== values(project).description
      )
        styleGuide.description = draft.description.trim();
      const updated = await api<Project>(
        "/projects/" + project.id,
        json("PATCH", {
          style_guide: styleGuide,
          output_profile: { ...project.output_profile, width, height, fps },
        }),
      );
      onSaved(updated);
      setDraft(values(updated));
      setPreset(presetFor(updated));
      setNotice("风格与画幅已保存");
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
      className="settings-section flex min-w-0 flex-col gap-4 rounded-xl border bg-background/60 p-4 production-settings"
      aria-labelledby={id + "-heading"}
    >
      <h3 id={id + "-heading"}>视觉风格与画幅</h3>
      <p>
        视觉风格会用于之后启动的编剧任务；修改不会重写已有版本。输出规格目前仅保存配置，图片生成与视频渲染尚未接入。
      </p>
      <form
        className="flex min-w-0 flex-col gap-4"
        onSubmit={save}
        aria-busy={busy}
      >
        <FieldGroup className="contents">
          <FieldSet
            className="draft-fields flex min-w-0 flex-col gap-4"
            disabled={locked}
          >
            <Field className="editor-field flex min-w-0 flex-col gap-2">
              <FormLabel htmlFor={id + "-style"}>视觉风格说明</FormLabel>
              <Textarea
                id={id + "-style"}
                value={draft.description}
                onChange={(event) => update("description", event.target.value)}
                maxLength={10000}
                disabled={locked || legacyDescription}
                placeholder="例如：水彩绘本，柔和线条，暖黄色与森林绿；避免写实血腥画面。"
                aria-describedby={id + "-style-hint"}
              />
              <small
                id={id + "-style-hint"}
                className="muted text-sm text-muted-foreground"
              >
                可填写画风、配色、光照和需要避免的元素，最多 10000
                字符；留空不添加额外说明。
              </small>
            </Field>
            {legacyDescription && (
              <p
                className="hint text-xs leading-relaxed text-muted-foreground"
                role="status"
              >
                已有风格说明不是文本格式，此处保持只读，保存画幅时会原样保留。可在下方查看完整配置。
              </p>
            )}
            <div className="production-profile grid min-w-0 items-start gap-6 md:grid-cols-[minmax(0,1fr)_200px]">
              <div className="draft-fields flex min-w-0 flex-col gap-4">
                <Field className="editor-field flex min-w-0 flex-col gap-2">
                  <FormLabel id={id + "-preset-label"} htmlFor={id + "-preset"}>
                    画幅预设
                  </FormLabel>
                  <Select
                    value={preset}
                    onValueChange={choose}
                    disabled={locked}
                  >
                    <SelectTrigger
                      id={id + "-preset"}
                      className="w-full"
                      aria-labelledby={id + "-preset-label"}
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent
                      position="popper"
                      sideOffset={6}
                      collisionPadding={12}
                    >
                      <SelectGroup>
                        {[
                          ...presets,
                          { value: "custom", label: "自定义尺寸" },
                        ].map((item) => (
                          <SelectItem key={item.value} value={item.value}>
                            <>{item.label}</>
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
                <div className="field-pair grid min-w-0 grid-cols-1 items-start gap-4 sm:grid-cols-2">
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={id + "-width"}>宽度（像素）</FormLabel>
                    <Input
                      id={id + "-width"}
                      type="number"
                      min="1"
                      max="8192"
                      step="1"
                      required
                      value={draft.width}
                      onChange={(event) => update("width", event.target.value)}
                    />
                  </Field>
                  <Field className="editor-field flex min-w-0 flex-col gap-2">
                    <FormLabel htmlFor={id + "-height"}>高度（像素）</FormLabel>
                    <Input
                      id={id + "-height"}
                      type="number"
                      min="1"
                      max="8192"
                      step="1"
                      required
                      value={draft.height}
                      onChange={(event) => update("height", event.target.value)}
                    />
                  </Field>
                </div>
                <Field className="editor-field flex min-w-0 flex-col gap-2">
                  <FormLabel htmlFor={id + "-fps"}>帧率（fps）</FormLabel>
                  <Input
                    id={id + "-fps"}
                    type="number"
                    min="1"
                    max="120"
                    step="0.001"
                    required
                    value={draft.fps}
                    onChange={(event) => update("fps", event.target.value)}
                  />
                  <small className="muted text-sm text-muted-foreground">
                    宽高 1–8192 整数像素；帧率 1–120，支持 23.976、29.97
                    等小数。
                  </small>
                </Field>
              </div>
              <figure
                className="profile-preview flex flex-col gap-3 rounded-xl border bg-card p-4 text-center text-xs text-muted-foreground [&_small]:mt-1 [&_small]:block"
                aria-label="画幅比例预览"
              >
                <div
                  className="profile-preview-stage flex h-40 items-center justify-center rounded-lg bg-muted"
                  aria-hidden="true"
                >
                  {validSize ? (
                    <div
                      className="profile-preview-frame rounded-sm border-2 border-primary/40 bg-accent shadow-sm"
                      style={{
                        width: (144 * width) / Math.max(width, height),
                        height: (144 * height) / Math.max(width, height),
                      }}
                    />
                  ) : (
                    <span>请填写有效宽高</span>
                  )}
                </div>
                <figcaption>
                  {validSize
                    ? width + " × " + height + " 像素"
                    : "尚未设置画幅"}
                  <small>仅示意比例，不是成片预览</small>
                </figcaption>
              </figure>
            </div>
          </FieldSet>
          {error && (
            <Notice tone="error" className="error" role="alert">
              {error}
            </Notice>
          )}
          {notice && (
            <Notice tone="success" className="success" role="status">
              {notice}
            </Notice>
          )}
          <div className="button-row flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="lg"
              type="button"
              disabled={locked}
              onClick={restore}
            >
              还原已保存配置
            </Button>
            <Button variant="default" size="lg" className="" disabled={locked}>
              {busy ? "保存中…" : "保存风格与画幅"}
            </Button>
          </div>
        </FieldGroup>
      </form>
      <details className="saved-production-config flex min-w-0 flex-col gap-3 text-sm [&>p]:my-3">
        <summary>查看已保存的完整配置（只读）</summary>
        <p>
          这里显示服务端已保存的值，不是上方未提交的草稿；其他字段会在保存时保留。
        </p>
        <pre>
          {JSON.stringify(
            {
              style_guide: project.style_guide,
              output_profile: project.output_profile,
            },
            null,
            2,
          )}
        </pre>
      </details>
    </section>
  );
}
