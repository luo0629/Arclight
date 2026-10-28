"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { episodePath } from "@/lib/routes";
import { statusText } from "@/lib/status";

type Usage = {
  id: string;
  asset_version_id: string;
  asset_version_no: number;
  episode_id: string;
  episode_no: number;
  episode_title: string;
  episode_status: string;
  content_kind: "script" | "storyboard";
  content_id: string;
  content_version_no: number;
  content_status: string;
  reference_path: string;
  scene_id: string | null;
  scene_no: number | null;
  scene_title: string | null;
  shot_no: number | null;
};

export default function AssetUsage({
  projectId,
  path,
}: {
  projectId: string;
  path: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section aria-label="资产实际使用" className="flex min-w-0 flex-col gap-3">
      <Button
        variant="outline"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? "收起实际使用" : "查看实际使用"}
      </Button>
      {open && <UsageList projectId={projectId} path={path} />}
    </section>
  );
}

function UsageList({ projectId, path }: { projectId: string; path: string }) {
  const list = usePagedList<Usage>(path + "/usage");
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  return (
    <>
      <h3 className="font-semibold">实际引用记录</h3>
      <p className="text-sm text-muted-foreground">
        来自已保存的剧本和分镜精确引用，包含历史及待审核版本；与资料中的声明范围分别记录。同一位置的不同引用分别列出，不按名称推断出镜。
      </p>
      {list.loaded && list.rows.length === 0 && (
        <p className="text-sm">尚无已保存的实际引用。</p>
      )}
      {list.rows.map((row) => (
        <article
          key={row.id}
          className="flex min-w-0 flex-col gap-1 border-t py-3 text-sm break-all"
        >
          <p>
            第 {row.episode_no} 集 · {row.episode_title} ·{" "}
            {statusText[row.episode_status] ?? row.episode_status}
          </p>
          <p>
            {row.content_kind === "script" ? "剧本" : "分镜"} v
            {row.content_version_no} ·{" "}
            {statusText[row.content_status] ?? row.content_status}
          </p>
          <p>
            {row.scene_no === null
              ? "剧本全局角色绑定"
              : `场景 ${row.scene_no}${row.scene_title ? " · " + row.scene_title : ""}`}
            {row.shot_no !== null && ` · 镜头 ${row.shot_no}`}
          </p>
          <p>
            引用图片 v{row.asset_version_no} · {row.asset_version_id}
          </p>
          <p className="text-muted-foreground">
            引用位置：{row.reference_path}
            {row.scene_id && ` · 场景 ID：${row.scene_id}`}
          </p>
          <Link
            className="underline underline-offset-4"
            href={
              episodePath(projectId, row.episode_id, "list") +
              "&node=" +
              encodeURIComponent(row.content_kind + ":" + row.content_id)
            }
          >
            打开此内容版本
          </Link>
        </article>
      ))}
      <ListPagination {...list} count={list.rows.length} label="实际引用" />
    </>
  );
}
