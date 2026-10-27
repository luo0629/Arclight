import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui-kit";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";
import type { Source } from "./story-source-types";

type Usage = {
  id: string;
  run_id: string;
  episode_id: string;
  workflow_type: string;
  status: string;
  analysis_id: string | null;
  outline_id: string | null;
  script_id: string | null;
};
export default function SourceHistory({
  episodeId,
  source,
  onOpen,
}: {
  episodeId: string;
  source: Source;
  onOpen: (source: Source) => void;
}) {
  const path = `/episodes/${episodeId}/story-sources/${source.id}`;
  const history = usePagedList<Source>(path + "/history");
  const usages = usePagedList<Usage>(path + "/usages");
  useEffect(() => {
    void history.reload().catch(() => {});
  }, [history.reload]);
  useEffect(() => {
    void usages.reload().catch(() => {});
  }, [usages.reload]);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <section
        aria-label="来源修订历史"
        className="flex min-w-0 flex-col gap-2"
      >
        <h4 className="text-sm font-medium">来源修订历史</h4>
        <div className="flex flex-wrap gap-2">
          {history.rows.map((row) => (
            <Button
              key={row.id}
              variant={source.id === row.id ? "secondary" : "outline"}
              onClick={() => onOpen(row)}
            >
              查看来源 v{row.provenance.version_no}
            </Button>
          ))}
        </div>
        <ListPagination
          {...history}
          label="来源修订"
          count={history.rows.length}
        />
      </section>
      <section
        aria-label="此版本的派生记录"
        className="flex min-w-0 flex-col gap-2"
      >
        <h4 className="text-sm font-medium">此版本的派生记录</h4>
        <p className="text-xs text-muted-foreground">
          只统计精确引用此版本的文本任务；修订不会改写旧任务或自动重新生成。
        </p>
        {usages.loaded && !usages.rows.length && (
          <p className="text-sm">暂无派生任务记录。</p>
        )}
        {usages.rows.map((row) => (
          <div
            key={row.run_id}
            className="flex min-w-0 flex-col gap-1 rounded-lg border p-3 text-xs"
          >
            <span>
              {(
                {
                  story_analysis: "故事提炼",
                  story_outline: "分集大纲",
                  script_storyboard: "编剧与分镜",
                  script_rewrite: "局部改写",
                  storyboard: "独立分镜",
                } as Record<string, string>
              )[row.workflow_type] ?? row.workflow_type}
            </span>
            <StatusBadge status={row.status} />
            <span className="break-all">任务：{row.run_id}</span>
            {row.analysis_id && (
              <span className="break-all">提炼版本：{row.analysis_id}</span>
            )}
            {row.outline_id && (
              <span className="break-all">大纲版本：{row.outline_id}</span>
            )}
            {row.script_id && (
              <span className="break-all">剧本版本：{row.script_id}</span>
            )}
          </div>
        ))}
        <ListPagination
          {...usages}
          label="来源派生"
          count={usages.rows.length}
        />
      </section>
    </div>
  );
}
