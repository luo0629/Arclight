import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import Dialog from "./Dialog";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";
import { api, json, message, type Project } from "./api";

type DeletedProject = {
  id: string;
  name: string;
  deletion_id: string;
  deleted_at: string;
  restore_until: string;
  can_restore: boolean;
};

export default function ProjectTrash({
  onDismiss,
  onRestored,
}: {
  onDismiss: () => void;
  onRestored: (project: Project) => void;
}) {
  const list = usePagedList<DeletedProject>("/project-trash");
  const { reload } = list;
  const [busy, setBusy] = useState("");
  const saving = useRef(false);
  const [error, setError] = useState("");
  useEffect(() => {
    void reload().catch(() => undefined);
  }, [reload]);
  async function restore(row: DeletedProject) {
    if (saving.current) return;
    saving.current = true;
    setBusy(row.id);
    setError("");
    let restored: Project;
    try {
      restored = await api<Project>(
        "/projects/" + row.id + "/restore",
        json("POST", { deletion_id: row.deletion_id }),
      );
    } catch (error) {
      setError(
        error instanceof TypeError
          ? "连接中断，恢复结果尚未确认。请刷新回收站与项目目录，勿重复提交。"
          : message(error),
      );
      saving.current = false;
      setBusy("");
      return;
    }
    onRestored(restored);
  }
  return (
    <Dialog
      labelledBy="project-trash-title"
      busy={!!busy}
      onDismiss={onDismiss}
    >
      <h2 id="project-trash-title">项目回收站</h2>
      <p className="text-sm text-muted-foreground">
        仅显示你拥有的项目。删除后 30
        天内可恢复，恢复后仍为归档。逾期数据不会自动物理清理，请联系管理员。
      </p>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {list.loaded && !list.rows.length && <p>回收站暂无项目</p>}
      <ul className="flex min-w-0 flex-col gap-3">
        {list.rows.map((row) => (
          <li
            key={row.deletion_id}
            className="flex min-w-0 flex-col gap-2 rounded-lg border p-3"
          >
            <h3 className="break-words font-semibold">{row.name}</h3>
            <p className="text-sm text-muted-foreground">
              删除时间：{new Date(row.deleted_at).toLocaleString()}
            </p>
            <p className="text-sm text-muted-foreground">
              恢复截止：{new Date(row.restore_until).toLocaleString()}
            </p>
            {!row.can_restore && (
              <p className="text-sm">
                恢复窗口已结束；数据尚未物理清理，请联系管理员。
              </p>
            )}
            <Button
              variant="outline"
              disabled={!!busy || !row.can_restore}
              onClick={() => void restore(row)}
            >
              {busy === row.id ? "正在恢复…" : "恢复项目"}
            </Button>
          </li>
        ))}
      </ul>
      <ListPagination
        {...list}
        loading={list.loading || !!busy}
        label="回收站项目"
        count={list.rows.length}
      />
      <Button variant="outline" disabled={!!busy} onClick={onDismiss}>
        关闭回收站
      </Button>
    </Dialog>
  );
}
