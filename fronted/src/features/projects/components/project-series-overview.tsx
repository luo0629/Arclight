"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import EpisodeSeriesEvidence from "@/features/episodes/components/episode-series-evidence";
import { Notice, PagePanel, StatusBadge } from "@/components/ui-kit";
import { LedgerQueryPanel } from "@/features/story/components/story-ledger";
import type { LedgerVersion } from "@/features/story/story-ledger";
import { episodePath, isResourceId, projectPath } from "@/lib/routes";
import type { Episode } from "@/types/api";
import useScopedRead from "../hooks/use-scoped-read";
import { useProject } from "../project-context";
import { useProjectEvents } from "../project-events";
import type { ApprovedSeriesPlan, SeriesWorkspace } from "../series-types";
import SeriesStartForm from "./series-start-form";

const limit = 10;

export default function ProjectSeriesOverview({
  episodes,
}: {
  episodes: Episode[];
}) {
  const { project } = useProject();
  const [expanded, setExpanded] = useState(false);
  return (
    <PagePanel
      title="连续剧制作"
      description="核对批准的剧情范围、时点事实与多集运行。作品完成以正式成片和内容批准为准。"
    >
      <Button
        type="button"
        variant="outline"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "收起连续剧详情" : "查看连续剧详情"}
      </Button>
      {expanded && <SeriesDetails key={project.id} episodes={episodes} />}
    </PagePanel>
  );
}

function SeriesDetails({ episodes }: { episodes: Episode[] }) {
  const { project, owner, writable } = useProject();
  const projectId = project.id;
  const [offset, setOffset] = useState(0);
  const read = useScopedRead<SeriesWorkspace>(
    `/projects/${projectId}/series-overview?limit=${limit}&offset=${offset}`,
  );
  useProjectEvents(read.refresh);
  const value = read.data;
  const data =
    value?.project_id === projectId && value.offset === offset ? value : null;
  if (!data)
    return read.error ? (
      <Notice tone="error">
        {read.error}
        <Button type="button" variant="outline" onClick={read.refresh}>
          重试连续剧详情
        </Button>
      </Notice>
    ) : (
      <p className="text-sm text-muted-foreground">正在读取连续剧记录…</p>
    );

  return (
    <div className="grid min-w-0 gap-5 border-t pt-4" aria-busy={read.loading}>
      {data.plan ? (
        <PlanDetails projectId={projectId} plan={data.plan} />
      ) : (
        <Notice>
          暂无已批准的全剧规划。先在剧集大纲中确认规划，草稿不能用于多集制作。
        </Notice>
      )}
      {data.ledger_version_id && isResourceId(data.ledger_version_id) && (
        <LedgerDetails
          key={data.ledger_version_id}
          projectId={projectId}
          versionId={data.ledger_version_id}
          plan={data.plan}
        />
      )}
      <nav
        aria-label="连续剧作品与待审"
        className="flex flex-wrap gap-x-4 gap-y-2 text-sm"
      >
        <Link className="underline" href={projectPath(projectId, "deliveries")}>
          逐集成片
        </Link>
        <Link className="underline" href={projectPath(projectId, "reviews")}>
          待审与内容批准
        </Link>
        <Link className="underline" href={projectPath(projectId, "assets")}>
          项目素材版本
        </Link>
      </nav>
      <SeriesEpisodePicker projectId={projectId} episodes={episodes} />
      <SeriesStartForm
        key={`${projectId}:${data.plan?.id}:${data.ledger_head_revision}`}
        workspace={data}
        episodes={episodes}
        writable={owner && writable}
        fresh={!read.loading}
        onStarted={() => {
          setOffset(0);
          read.refresh();
        }}
      />
      <section
        aria-label="多集运行记录"
        className="grid min-w-0 gap-2 border-t pt-4"
      >
        <h3 className="font-semibold">多集运行记录</h3>
        {!data.items.length && (
          <p className="text-sm text-muted-foreground">该页暂无多集运行。</p>
        )}
        <ul className="grid gap-2">
          {data.items
            .filter(
              (row) =>
                row.project_id === projectId &&
                isResourceId(row.id) &&
                isResourceId(row.episode_id),
            )
            .map((row) => (
              <li
                key={row.id}
                className="flex flex-wrap items-center justify-between gap-2 border-b py-2 text-sm"
              >
                <Link
                  className="underline"
                  href={`${episodePath(projectId, row.episode_id, "tasks")}&run=${row.id}`}
                >
                  查看多集运行 · 锚点第 {row.episode_no} 集 ·{" "}
                  <time dateTime={row.created_at}>
                    {new Date(row.created_at).toLocaleString("zh-CN")}
                  </time>
                </Link>
                {row.paused ? (
                  <span>已暂停</span>
                ) : (
                  <StatusBadge status={row.status} />
                )}
              </li>
            ))}
        </ul>
        <nav
          aria-label="多集运行分页"
          className="flex flex-wrap items-center justify-between gap-2"
        >
          <Button
            type="button"
            variant="outline"
            disabled={read.loading || offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - limit))}
          >
            上一页运行
          </Button>
          <span className="text-sm text-muted-foreground">
            {data.total} 条运行记录
          </span>
          <Button
            type="button"
            variant="outline"
            disabled={read.loading || offset + limit >= data.total}
            onClick={() => setOffset((value) => value + limit)}
          >
            下一页运行
          </Button>
        </nav>
      </section>
    </div>
  );
}

function PlanDetails({
  projectId,
  plan,
}: {
  projectId: string;
  plan: ApprovedSeriesPlan;
}) {
  const value = plan.content.series_plan;
  const names = new Map(
    plan.characters.map((person) => [person.id, person.name]),
  );
  const titles = new Map(
    value.threads.map((thread) => [thread.id, thread.title]),
  );
  return (
    <section aria-label="批准的全剧规划" className="grid min-w-0 gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold">
          批准的全剧规划 · 第 {plan.version_no} 版
        </h3>
        <Link
          className="text-sm underline"
          href={`${episodePath(projectId, plan.episode_id, "outline")}&outline=${encodeURIComponent(plan.id)}`}
        >
          打开精确规划
        </Link>
      </div>
      <p className="text-sm text-muted-foreground">
        批准范围：{plan.content.target_episode_count}{" "}
        集。下列为作者规划，不代表情节已经发生。
      </p>
      <details open className="rounded-lg border p-3">
        <summary className="cursor-pointer text-sm font-medium">主支线</summary>
        <ul className="divide-y text-sm">
          {value.threads.map((thread) => (
            <li key={thread.id} className="grid gap-1 py-2 break-words">
              <strong>
                {thread.kind === "MAIN" ? "主线" : "支线"} · {thread.title}
              </strong>
              <p>{thread.central_conflict}</p>
              <p className="text-muted-foreground">
                计划收束：{thread.planned_resolution}
              </p>
            </li>
          ))}
        </ul>
      </details>
      <details className="rounded-lg border p-3">
        <summary className="cursor-pointer text-sm font-medium">
          人物发展与关系
        </summary>
        <ul className="divide-y text-sm">
          {value.character_arcs.map((arc) => (
            <li key={arc.character_id} className="py-2 break-words">
              <strong>{names.get(arc.character_id) ?? "角色名称未记录"}</strong>{" "}
              · {arc.starting_goal} → {arc.planned_growth}
            </li>
          ))}
          {value.relationship_arcs.map((arc) => (
            <li key={arc.id} className="py-2 break-words">
              <strong>
                {names.get(arc.source_character_id) ?? "角色名称未记录"} ↔{" "}
                {names.get(arc.target_character_id) ?? "角色名称未记录"}
              </strong>{" "}
              · {arc.initial_relationship} → {arc.planned_development}
            </li>
          ))}
        </ul>
      </details>
      <details className="rounded-lg border p-3">
        <summary className="cursor-pointer text-sm font-medium">
          伏笔与预定回收
        </summary>
        <ul className="divide-y text-sm">
          {value.foreshadowing.map((item) => (
            <li key={item.id} className="grid gap-1 py-2 break-words">
              <strong>
                {titles.get(item.thread_id)} · 第 {item.setup_episode_no} 集 →
                第 {item.expected_payoff_episode_no} 集
              </strong>
              <p>设置：{item.setup}</p>
              <p>预计回收：{item.planned_payoff}</p>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function LedgerDetails({
  projectId,
  versionId,
  plan,
}: {
  projectId: string;
  versionId: string;
  plan: ApprovedSeriesPlan | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className="grid min-w-0 gap-2">
      <Button
        className="justify-self-start"
        type="button"
        variant="outline"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "收起时点事实" : "查询时点事实与关系"}
      </Button>
      {open && (
        <LedgerAtVersion
          projectId={projectId}
          versionId={versionId}
          plan={plan}
        />
      )}
    </section>
  );
}

function LedgerAtVersion({
  projectId,
  versionId,
  plan,
}: {
  projectId: string;
  versionId: string;
  plan: ApprovedSeriesPlan | null;
}) {
  const path = `/projects/${projectId}/story-ledger`;
  const read = useScopedRead<LedgerVersion>(`${path}/versions/${versionId}`);
  const version = read.data;
  if (read.error) return <Notice tone="error">{read.error}</Notice>;
  if (!version || version.id !== versionId || version.project_id !== projectId)
    return (
      <p className="text-sm text-muted-foreground">正在读取批准的账本版本…</p>
    );
  return (
    <>
      <p className="text-sm text-muted-foreground">
        批准账本 · 第 {version.version_no} 版
      </p>
      <LedgerQueryPanel
        key={version.id}
        path={path}
        version={version}
        entities={(plan?.characters ?? []).map((person) => ({
          ...person,
          kind: "CHARACTER",
          archived: false,
        }))}
      />
    </>
  );
}

function SeriesEpisodePicker({
  projectId,
  episodes,
}: {
  projectId: string;
  episodes: Episode[];
}) {
  const [episodeId, setEpisodeId] = useState("");
  const row = episodes.find(
    (episode) => episode.id === episodeId && episode.project_id === projectId,
  );
  return (
    <section aria-label="跨集状态查看" className="grid gap-2 border-t pt-3">
      <h3 className="font-semibold">跨集状态查看</h3>
      <NativeSelect
        aria-label="选择查看制作状态的剧集"
        value={row?.id ?? ""}
        onChange={(event) => setEpisodeId(event.target.value)}
      >
        <option value="">选择本页剧集以查看表演、视频、声音和成片</option>
        {episodes.map((episode) => (
          <option key={episode.id} value={episode.id}>
            第 {episode.episode_no} 集 · {episode.title}
          </option>
        ))}
      </NativeSelect>
      {row && (
        <EpisodeSeriesEvidence
          key={row.id}
          projectId={projectId}
          episodeId={row.id}
        />
      )}
    </section>
  );
}
