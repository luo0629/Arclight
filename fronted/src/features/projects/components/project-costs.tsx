"use client";

import { useEffect, useState } from "react";
import { api, message } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  EmptyState,
  LoadingState,
  Notice,
  PagePanel,
} from "@/components/ui-kit";

export type CostTotals = {
  estimated_cost: string | null;
  actual_cost: string | null;
  pending_estimate: string;
  projected_cost: string | null;
  entry_count: number;
  confirmed_count: number;
  estimated_count: number;
  unpriced_count: number;
  cost_is_estimate: boolean;
};
type Dimension = "project" | "episode" | "task" | "model";
type Group = CostTotals & {
  key: string;
  label: string;
  episode_id: string | null;
  task_id: string | null;
  provider: string | null;
  model: string | null;
};
type Report = {
  summary: CostTotals;
  groups: Group[];
  total_groups: number;
  generated_at: string;
};
type Entry = {
  id: string;
  source: "MEDIA" | "TEXT";
  task_id: string;
  task_type: string;
  model: string;
  provider: string;
  attempt_no: number;
  attempt_id: string | null;
  call_id: string | null;
  input_manifest_id: string | null;
  trace_id: string | null;
  actual_cost: string | null;
  estimated_cost: string | null;
  pending_estimate: string;
  cost_is_estimate: boolean;
};
type Entries = { items: Entry[]; total: number };
const dimensions: [Dimension, string][] = [
  ["project", "项目"],
  ["episode", "剧集"],
  ["task", "任务"],
  ["model", "模型"],
];
const money = (value: string | null) =>
  value === null ? "未知" : `${value} USD`;

export function CostSummary({ value }: { value: CostTotals }) {
  if (!value.entry_count)
    return <p className="text-sm text-muted-foreground">尚无费用记录</p>;
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">
            {value.cost_is_estimate ? "已确认费用小计" : "实际成本"}
          </dt>
          <dd className="mt-1 break-words font-semibold tabular-nums">
            {money(value.actual_cost)}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">待核账预留（估算）</dt>
          <dd className="mt-1 break-words font-semibold tabular-nums">
            {money(value.pending_estimate)}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">
            {value.cost_is_estimate ? "预计累计（估算）" : "累计成本"}
          </dt>
          <dd className="mt-1 break-words font-semibold tabular-nums">
            {money(value.projected_cost)}
          </dd>
        </div>
      </dl>
      {value.cost_is_estimate && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          {value.estimated_count} 条待核账，{value.unpriced_count}{" "}
          条缺少可信报价。预留代表预算占用，最终费用以账单为准。
        </p>
      )}
    </div>
  );
}

function Pages({
  offset,
  count,
  loading,
  onChange,
}: {
  offset: number;
  count: number;
  loading: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-xs text-muted-foreground">
        共 {count} 条 · 第 {count ? offset + 1 : 0}–
        {Math.min(offset + 10, count)} 条
      </p>
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={loading || offset === 0}
          onClick={() => onChange(Math.max(0, offset - 10))}
        >
          上一页
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={loading || offset + 10 >= count}
          onClick={() => onChange(offset + 10)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

function CostDetails({
  projectId,
  group,
  dimension,
}: {
  projectId: string;
  group: Group;
  dimension: Dimension;
}) {
  const [data, setData] = useState<Entries | null>(null);
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const filters = new URLSearchParams({ limit: "10", offset: String(offset) });
  if (dimension === "episode" && group.episode_id)
    filters.set("episode_id", group.episode_id);
  if (dimension === "episode" && !group.episode_id)
    filters.set("unassigned_episode", "true");
  if (dimension === "task" && group.task_id)
    filters.set("task_id", group.task_id);
  if (dimension === "model") {
    if (group.provider) filters.set("provider", group.provider);
    if (group.model) filters.set("model", group.model);
  }
  const query = filters.toString();
  useEffect(() => {
    const request = new AbortController();
    setLoading(true);
    setError("");
    setData(null);
    void api<Entries>(`/projects/${projectId}/costs/entries?${query}`, {
      signal: request.signal,
    })
      .then((result) => {
        if (!request.signal.aborted) setData(result);
      })
      .catch((error) => {
        if (!request.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [projectId, query, version]);
  return (
    <section
      aria-label="费用明细"
      className="flex min-w-0 flex-col gap-4 border-t pt-5"
      aria-busy={loading}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">{group.label} · 费用明细</h3>
        <Button
          size="sm"
          variant="outline"
          disabled={loading}
          onClick={() => setVersion((value) => value + 1)}
        >
          刷新明细
        </Button>
      </div>
      {loading && <LoadingState>正在读取费用明细</LoadingState>}
      {error && <Notice tone="error">{error}</Notice>}
      {data && (
        <>
          {!data.items.length && <EmptyState>此范围尚无费用记录。</EmptyState>}
          {data.items.map((entry) => (
            <article
              key={entry.id}
              className="flex min-w-0 flex-col gap-2 border-b pb-4 text-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="min-w-0 break-all font-medium">
                  {entry.provider} / {entry.model}
                </p>
                <Badge
                  variant={entry.cost_is_estimate ? "outline" : "secondary"}
                >
                  {entry.cost_is_estimate ? "估算 · 待核账" : "费用已确认"}
                </Badge>
              </div>
              <p>
                报价：{money(entry.estimated_cost)} · 实际：
                {money(entry.actual_cost)}
              </p>
              <dl className="grid min-w-0 gap-x-3 gap-y-1 text-xs text-muted-foreground sm:grid-cols-[auto_minmax(0,1fr)]">
                <dt>任务</dt>
                <dd className="break-all">{entry.task_id}</dd>
                <dt>{entry.source === "MEDIA" ? "尝试" : "调用"}</dt>
                <dd className="break-all">
                  第 {entry.attempt_no} 次 ·{" "}
                  {entry.attempt_id ?? entry.call_id ?? "尚未提交"}
                </dd>
                {entry.input_manifest_id && (
                  <>
                    <dt>输入清单</dt>
                    <dd className="break-all">{entry.input_manifest_id}</dd>
                  </>
                )}
                {entry.trace_id && (
                  <>
                    <dt>Trace</dt>
                    <dd className="break-all">{entry.trace_id}</dd>
                  </>
                )}
              </dl>
            </article>
          ))}
          <Pages
            offset={offset}
            count={data.total}
            loading={loading}
            onChange={setOffset}
          />
        </>
      )}
    </section>
  );
}

export default function ProjectCosts({ projectId }: { projectId: string }) {
  const [dimension, setDimension] = useState<Dimension>("project");
  const [data, setData] = useState<Report | null>(null);
  const [selected, setSelected] = useState<Group | null>(null);
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const request = new AbortController();
    setLoading(true);
    setData(null);
    setSelected(null);
    setError("");
    void api<Report>(
      `/projects/${projectId}/costs?group_by=${dimension}&limit=10&offset=${offset}`,
      { signal: request.signal },
    )
      .then((result) => {
        if (!request.signal.aborted) setData(result);
      })
      .catch((error) => {
        if (!request.signal.aborted) setError(message(error));
      })
      .finally(() => {
        if (!request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [projectId, dimension, offset, version]);
  return (
    <PagePanel
      title="成本明细"
      description="费用来自已保存的调用与账单记录；报价、预算预留和实际账单分别展示。"
      drawing="film"
    >
      <section
        aria-label="项目成本数据"
        aria-busy={loading}
        className="flex min-w-0 flex-col gap-5"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <ToggleGroup
            type="single"
            variant="outline"
            value={dimension}
            aria-label="成本汇总维度"
            onValueChange={(value) => {
              if (value) {
                setDimension(value as Dimension);
                setOffset(0);
              }
            }}
          >
            {dimensions.map(([value, label]) => (
              <ToggleGroupItem key={value} value={value}>
                按{label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <Button
            variant="outline"
            disabled={loading}
            onClick={() => setVersion((value) => value + 1)}
          >
            刷新成本
          </Button>
        </div>
        {loading && <LoadingState>正在汇总费用</LoadingState>}
        {error && <Notice tone="error">{error}</Notice>}
        {data && (
          <>
            <CostSummary value={data.summary} />
            {!data.summary.entry_count && (
              <EmptyState>
                任务产生调用或预算预留后，将在此显示。缺失账单会保留为待核账。
              </EmptyState>
            )}
            <div className="flex flex-col gap-4">
              {data.groups.map((group) => (
                <div
                  key={group.key}
                  className="flex min-w-0 flex-col gap-3 border-t pt-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="break-words font-medium">{group.label}</h3>
                      {group.task_id && (
                        <p className="mt-1 break-all text-xs text-muted-foreground">
                          {group.task_id}
                        </p>
                      )}
                    </div>
                    <Badge variant="outline">
                      {group.entry_count} 条 ·{" "}
                      {group.cost_is_estimate ? "含估算" : "已确认"}
                    </Badge>
                  </div>
                  <CostSummary value={group} />
                  <p className="text-xs text-muted-foreground">
                    原始报价合计：{money(group.estimated_cost)}
                    （供对比，不是实际扣费）
                  </p>
                  <Button
                    className="self-start"
                    size="sm"
                    variant="outline"
                    onClick={() => setSelected(group)}
                  >
                    查看该组明细
                  </Button>
                </div>
              ))}
            </div>
            <Pages
              offset={offset}
              count={data.total_groups}
              loading={loading}
              onChange={setOffset}
            />
            {selected && (
              <CostDetails
                key={`${dimension}:${selected.key}:${version}`}
                projectId={projectId}
                group={selected}
                dimension={dimension}
              />
            )}
          </>
        )}
      </section>
    </PagePanel>
  );
}
