import { useId, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldSet } from "@/components/ui/field";
import { FormLabel, Notice } from "@/components/ui-kit";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { api, json, message, type Episode } from "./api";

export default function EpisodeCopy({
  episode,
  suggestedNumber,
  disabled,
  onCopied,
}: {
  episode: Episode;
  suggestedNumber: number;
  disabled: boolean;
  onCopied: (episode: Episode) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [number, setNumber] = useState("");
  const [reuseAssets, setReuseAssets] = useState("yes");
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState("");

  function changeOpen(next: boolean) {
    if (saving.current || (next && disabled)) return;
    if (next) {
      setTitle(episode.title.slice(0, 155) + "（副本）");
      setNumber(String(suggestedNumber));
      setReuseAssets("yes");
      setError("");
    }
    setOpen(next);
  }

  async function copy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current || disabled) return;
    const episodeNo = Number(number);
    if (
      !title.trim() ||
      title.trim().length > 160 ||
      !Number.isInteger(episodeNo) ||
      episodeNo < 1 ||
      episodeNo > 2147483647
    ) {
      setError("请输入 1–160 个字符的标题和有效的正整数集数。");
      return;
    }
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await api<Episode>(
        "/episodes/" + episode.id + "/copy",
        json("POST", {
          title: title.trim(),
          episode_no: episodeNo,
          reuse_assets: reuseAssets === "yes",
        }),
      );
      setOpen(false);
      onCopied(result);
    } catch (error) {
      setError(
        error instanceof TypeError
          ? "网络连接失败，输入已保留。请先刷新目录确认是否已复制，再重试。"
          : message(error),
      );
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="lg" disabled={disabled}>
          复制为新集草稿
        </Button>
      </DialogTrigger>
      <DialogContent
        showCloseButton={false}
        className="studio-action-dialog max-h-[90dvh] overflow-y-auto"
        onInteractOutside={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => {
          if (saving.current) event.preventDefault();
        }}
      >
        <DialogTitle>复制剧集为草稿</DialogTitle>
        <DialogDescription>
          复制当前可用剧本及其分镜，保留原目标时长。新文本须重新审核；不复制任务、失效或驳回版本、镜头生成结果、原审核关系及画布布局。活动任务须先结束或取消。
        </DialogDescription>
        <form
          className="flex min-w-0 flex-col gap-4"
          onSubmit={copy}
          aria-busy={busy}
        >
          <FieldGroup className="contents">
            <FieldSet
              className="flex min-w-0 flex-col gap-4"
              disabled={busy || disabled}
            >
              <Field>
                <FormLabel htmlFor={id + "-number"}>新剧集集数</FormLabel>
                <Input
                  id={id + "-number"}
                  type="number"
                  min="1"
                  max="2147483647"
                  step="1"
                  value={number}
                  onChange={(event) => setNumber(event.target.value)}
                  required
                />
              </Field>
              <Field>
                <FormLabel htmlFor={id + "-title"}>新剧集标题</FormLabel>
                <Input
                  id={id + "-title"}
                  maxLength={160}
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  required
                />
              </Field>
              <Field>
                <FormLabel htmlFor={id + "-assets"}>资产复用</FormLabel>
                <Select
                  value={reuseAssets}
                  onValueChange={setReuseAssets}
                  disabled={busy || disabled}
                >
                  <SelectTrigger id={id + "-assets"}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="yes">复用有效的精确资产版本</SelectItem>
                    <SelectItem value="no">仅复制文本，移除资产引用</SelectItem>
                  </SelectContent>
                </Select>
                <small className="text-muted-foreground">
                  只复用本项目已批准或锁定且未失效的角色、场景、地点、物品和服装版本；不复制资产文件，不替换为最新版本。
                </small>
              </Field>
            </FieldSet>
            {error && (
              <Notice tone="error" role="alert">
                {error}
              </Notice>
            )}
            <div className="flex flex-wrap gap-2">
              <DialogClose asChild>
                <Button
                  type="button"
                  variant="outline"
                  size="lg"
                  disabled={busy}
                >
                  取消
                </Button>
              </DialogClose>
              <Button size="lg" disabled={busy || disabled}>
                {busy ? "复制中…" : "确认复制"}
              </Button>
            </div>
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  );
}
