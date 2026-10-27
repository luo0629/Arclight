import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogTrigger,
} from "@/components/ui/dialog";
import { FormLabel, Notice } from "@/components/ui-kit";
import { ChoiceSelect } from "@/components/choice-select";
import { useDraftStore, useSessionDraft } from "./Drafts";
import { api, json, message } from "./api";
import type { SourceDetail } from "./story-source-types";

type Draft = {
  name: string;
  text: string;
  origin: string;
  rights: string;
  confirmed: string;
  change: string;
  fingerprint: string;
  key: string;
};
const valid = (value: unknown): value is Draft =>
  !!value &&
  typeof value === "object" &&
  [
    "name",
    "text",
    "origin",
    "rights",
    "confirmed",
    "change",
    "fingerprint",
    "key",
  ].every((key) => typeof (value as Record<string, unknown>)[key] === "string");

export default function SourceRevisionEditor({
  episodeId,
  base,
  onSaved,
}: {
  episodeId: string;
  base?: SourceDetail;
  onSaved: (source: SourceDetail) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const saving = useRef(false);
  const live = useRef(true);
  const id = JSON.stringify([episodeId, "source", base?.id ?? "new"]);
  const { write } = useDraftStore();
  const local = useSessionDraft<Draft>({
    id,
    episodeId,
    label: base ? `故事来源 v${base.provenance.version_no} 修订` : "新故事来源",
    initial: {
      name: base?.original_name ?? "故事.txt",
      text: base?.text ?? "",
      origin: base?.source_note ?? "",
      rights:
        base?.provenance.rights_status === "CONFIRMED" ? base.rights_note : "",
      confirmed: "no",
      change: "",
      fingerprint: "",
      key: "",
    },
    validate: valid,
  });
  const draft = local.value;
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  function change(field: keyof Draft, value: string) {
    local.update({ ...draft, [field]: value });
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving.current || draft.confirmed !== "yes") return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      if (new TextEncoder().encode(draft.text).byteLength > 256 * 1024)
        throw new Error("故事文本不能超过 256 KiB");
      const body = {
        original_name: draft.name,
        text: draft.text,
        source_note: draft.origin.trim(),
        rights_note: draft.rights.trim(),
        rights_confirmed: true,
        base_source_id: base?.id,
        change_note: draft.change.trim(),
      };
      const fingerprint = JSON.stringify(body);
      const submitted = {
        ...draft,
        fingerprint,
        key:
          draft.fingerprint === fingerprint && draft.key
            ? draft.key
            : crypto.randomUUID(),
      };
      local.update(submitted);
      const result = await api<SourceDetail>(
        `/episodes/${episodeId}/story-sources/text`,
        json("POST", { ...body, import_key: submitted.key }),
      );
      write(id, undefined, submitted);
      if (!live.current) return;
      local.clear();
      setOpen(false);
      await onSaved(result);
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!saving.current) setOpen(value);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline">
          {base ? "修订此来源" : "保存故事文本"}
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-h-[90dvh] overflow-y-auto"
        onInteractOutside={(event) => {
          if (saving.current) event.preventDefault();
        }}
        onEscapeKeyDown={(event) => {
          if (saving.current) event.preventDefault();
        }}
      >
        <DialogTitle>
          {base
            ? `基于来源 v${base.provenance.version_no} 保存修订`
            : "保存故事文本"}
        </DialogTitle>
        <DialogDescription>
          {base
            ? "另存新版本，原稿、已批准内容和运行中的任务保持原有来源。文本修订使用 UTF-8。"
            : "原始文本与来源、授权一起保存，不启动生成。"}{" "}
          草稿仅保存在当前浏览器标签页。
        </DialogDescription>
        {local.recovered && <Notice>已恢复此来源的未保存草稿。</Notice>}
        {local.invalid && (
          <Notice tone="error">本地草稿损坏，已加载已保存内容。</Notice>
        )}
        <form
          onSubmit={save}
          aria-label="保存来源修订"
          aria-busy={busy}
          className="flex min-w-0 flex-col gap-4"
        >
          <fieldset disabled={busy} className="flex min-w-0 flex-col gap-4">
            <FormLabel>
              来源名称
              <Input
                value={draft.name}
                onChange={(e) => change("name", e.target.value)}
                required
                maxLength={160}
              />
            </FormLabel>
            <FormLabel>
              来源正文
              <Textarea
                aria-label="来源正文"
                value={draft.text}
                onChange={(e) => change("text", e.target.value)}
                className="min-h-40"
                required
                maxLength={262144}
              />
            </FormLabel>
            <FormLabel>
              文本来源说明
              <Input
                value={draft.origin}
                onChange={(e) => change("origin", e.target.value)}
                required
                maxLength={1000}
              />
            </FormLabel>
            <FormLabel>
              文本授权说明
              <Textarea
                aria-label="文本授权说明"
                value={draft.rights}
                onChange={(e) => change("rights", e.target.value)}
                required
                maxLength={1000}
              />
            </FormLabel>
            {base && (
              <FormLabel>
                修订说明
                <Input
                  value={draft.change}
                  onChange={(e) => change("change", e.target.value)}
                  required
                  maxLength={1000}
                />
              </FormLabel>
            )}
            <FormLabel>
              文本使用权确认
              <ChoiceSelect
                aria-label="文本使用权确认"
                value={draft.confirmed}
                onValueChange={(value) => change("confirmed", value)}
                options={[
                  { value: "no", label: "请选择确认" },
                  { value: "yes", label: "我确认有权保存和使用此文本" },
                ]}
              />
            </FormLabel>
          </fieldset>
          {error && <Notice tone="error">{error}</Notice>}
          <Button
            type="submit"
            disabled={
              busy ||
              !draft.text.trim() ||
              !draft.name.trim() ||
              !draft.origin.trim() ||
              !draft.rights.trim() ||
              (base && !draft.change.trim()) ||
              draft.confirmed !== "yes"
            }
          >
            {busy ? "正在保存…" : "另存来源版本"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
