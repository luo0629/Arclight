import { Button } from "@/components/ui/button";
import { FieldGroup, FieldSet, Field } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import { Input } from "@/components/ui/input";
import { useId, useRef, useState, type FormEvent } from "react";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogClose,
} from "@/components/ui/dialog";
import { api, json, message, type Episode } from "./api";

export default function EpisodeEditor({
  episode,
  disabled = false,
  onSaved,
}: {
  episode: Episode;
  disabled?: boolean;
  onSaved: (episode: Episode) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [duration, setDuration] = useState("");
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function changeOpen(next: boolean) {
    if (saving.current || (next && disabled)) return;
    if (next) {
      setTitle(episode.title);
      setDuration(String(episode.target_duration_ms / 1000));
      setError("");
      setNotice("");
    }
    setOpen(next);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current || disabled) return;
    const cleanTitle = title.trim();
    const milliseconds = Number(duration) * 1000;
    if (!cleanTitle || cleanTitle.length > 160) {
      setError("请输入 1–160 个字符的剧集标题，不能只填空格。");
      return;
    }
    if (
      !Number.isFinite(milliseconds) ||
      milliseconds < 2500 ||
      milliseconds > 1800000 ||
      Math.abs(milliseconds - Math.round(milliseconds)) > 0.000001
    ) {
      setError("目标时长应为 2.5–1800 秒，最多保留三位小数。");
      return;
    }
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const updated = await api<Episode>(
        "/episodes/" + episode.id,
        json("PATCH", {
          title: cleanTitle,
          target_duration_ms: Math.round(milliseconds),
        }),
      );
      // 使用服务端返回值同步目录和工作区，避免保存成功却因二次读取失败而误报失败。
      onSaved(updated);
      setOpen(false);
      setNotice("剧集信息已保存");
    } catch (error) {
      setError(
        error instanceof TypeError
          ? "网络连接失败，请检查连接后重试。输入内容已保留。"
          : message(error),
      );
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="episode-editor mt-3 flex flex-wrap items-center gap-2">
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogTrigger asChild>
          <Button variant="outline" size="lg" disabled={disabled}>
            编辑剧集
          </Button>
        </DialogTrigger>

        <DialogContent
          showCloseButton={false}
          className="max-h-[90dvh] overflow-y-auto"
          onInteractOutside={(event) => event.preventDefault()}
          onEscapeKeyDown={(event) => {
            if (saving.current) event.preventDefault();
          }}
        >
          <DialogTitle>编辑剧集信息</DialogTitle>
          <DialogDescription>
            修改第 {episode.episode_no}{" "}
            集的标题与目标时长，不会改写已有剧本或分镜。进行中的任务须先结束或取消。
          </DialogDescription>
          <form
            className="flex min-w-0 flex-col gap-4"
            onSubmit={save}
            aria-busy={busy}
          >
            <FieldGroup className="contents">
              <FieldSet
                className="draft-fields flex min-w-0 flex-col gap-4"
                disabled={busy || disabled}
              >
                <Field className="editor-field flex min-w-0 flex-col gap-2">
                  <FormLabel htmlFor={id + "-title"}>剧集标题</FormLabel>
                  <Input
                    id={id + "-title"}
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                    maxLength={160}
                    required
                  />
                </Field>
                <Field className="editor-field flex min-w-0 flex-col gap-2">
                  <FormLabel htmlFor={id + "-duration"}>
                    目标时长（秒）
                  </FormLabel>
                  <Input
                    id={id + "-duration"}
                    type="number"
                    min="2.5"
                    max="1800"
                    step="0.001"
                    value={duration}
                    onChange={(event) => setDuration(event.target.value)}
                    aria-describedby={id + "-hint"}
                    required
                  />
                  <small
                    id={id + "-hint"}
                    className="muted text-sm text-muted-foreground"
                  >
                    2.5–1800 秒，最多三位小数。
                  </small>
                </Field>
              </FieldSet>
              {error && (
                <Notice tone="error" className="error" role="alert">
                  {error}
                </Notice>
              )}
              <div className="button-row flex flex-wrap items-center gap-2">
                <DialogClose asChild>
                  <Button
                    variant="outline"
                    size="lg"
                    type="button"
                    disabled={busy}
                  >
                    取消
                  </Button>
                </DialogClose>
                <Button
                  variant="default"
                  size="lg"
                  className=""
                  disabled={busy || disabled}
                >
                  {busy ? "保存中…" : "保存剧集"}
                </Button>
              </div>
            </FieldGroup>
          </form>
        </DialogContent>
      </Dialog>
      {notice && (
        <small className="episode-saved text-xs text-success" role="status">
          {notice}
        </small>
      )}
    </div>
  );
}
