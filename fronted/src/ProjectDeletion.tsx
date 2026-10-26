import { useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormLabel, Notice } from "@/components/ui-kit";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogTrigger,
  DialogClose,
} from "@/components/ui/dialog";
import { api, json, message, type Project } from "./api";

export default function ProjectDeletion({
  project,
  disabled,
  onDeleted,
}: {
  project: Project;
  disabled: boolean;
  onDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  function changeOpen(next: boolean) {
    if (saving.current || (next && disabled)) return;
    if (next) {
      setName("");
      setError("");
    }
    setOpen(next);
  }
  async function remove(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving.current || disabled || name.trim() !== project.name) return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      await api(
        "/projects/" + project.id,
        json("DELETE", { confirm_name: name.trim() }),
      );
    } catch (error) {
      setError(
        error instanceof TypeError
          ? "连接中断，删除结果尚未确认。输入已保留，请先刷新项目目录或查看回收站。"
          : message(error),
      );
      saving.current = false;
      setBusy(false);
      return;
    }
    setOpen(false);
    onDeleted();
  }
  return (
    <section className="settings-section studio-settings-section flex min-w-0 flex-col gap-4">
      <h3>删除项目</h3>
      <p className="text-sm text-muted-foreground">
        仅所有者可删除已归档、无活动任务的项目。删除后所有成员失去访问权限；30
        天内可在回收站恢复。恢复后仍为归档状态，不会自动启动生产。
      </p>
      <p className="text-sm text-muted-foreground">
        这是逻辑删除，不是物理擦除：内容、资产引用和审计记录保留。逾期不自动清理，备份及外部服务数据也不在此操作范围内。
      </p>
      {!project.archived && (
        <p className="text-sm">请先归档项目，再申请删除。</p>
      )}
      <Dialog open={open} onOpenChange={changeOpen}>
        <DialogTrigger asChild>
          <Button
            variant="destructive"
            disabled={disabled || !project.archived}
          >
            删除项目
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
          <DialogTitle>确认删除项目</DialogTitle>
          <DialogDescription>
            “{project.name}”将移入回收站，所有成员立即失去访问权限。30
            天内可恢复，内容不会物理清理。
          </DialogDescription>
          <form
            className="flex min-w-0 flex-col gap-4"
            onSubmit={remove}
            aria-busy={busy}
          >
            <FormLabel>
              输入完整项目名称
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={busy}
                autoFocus
                required
                maxLength={160}
                autoComplete="off"
              />
            </FormLabel>
            {error && (
              <Notice tone="error" role="alert">
                {error}
              </Notice>
            )}
            <div className="flex flex-wrap gap-2">
              <DialogClose asChild>
                <Button type="button" variant="outline" disabled={busy}>
                  取消
                </Button>
              </DialogClose>
              <Button
                type="submit"
                variant="destructive"
                disabled={busy || name.trim() !== project.name}
              >
                {busy ? "正在删除…" : "确认移入回收站"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </section>
  );
}
