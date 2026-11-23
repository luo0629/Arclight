"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice, StatusBadge } from "@/components/ui-kit";
import type { DependencyImpact } from "@/features/dependencies/components/impact-button";
import useScopedRead from "@/features/projects/hooks/use-scoped-read";
import { useProjectEvents } from "@/features/projects/project-events";
import {
  episodePath,
  isResourceId,
  productionDeliveryPath,
  projectPath,
} from "@/lib/routes";
import { issueLabel, phaseLabel } from "../production-stages";

type Evidence = {
  project_id: string;
  episode_id: string;
  phase: string;
  issues: string[];
  script_version_id: string | null;
  storyboard_version_id: string | null;
  outline_version_id: string | null;
  outline_episode_id: string | null;
  ledger_version_id: string | null;
  target_story_time: number | null;
  performance_shots: number;
  dynamic_shots: number;
  reviewed_dynamic_shots: number;
  visible_dialogue_shots: number;
  required_lip_sync_shots: number;
  reviewed_videos: {
    version_id: string;
    shot_no: number;
    temporal_review_id: string;
    lip_sync_reviewed: boolean;
  }[];
  audio_snapshot_id: string | null;
  audio_line_count: number | null;
  bound_audio_total: number;
  bound_audio: {
    version_id: string;
    line_id: string;
    version_no: number;
    status: string;
    stale: boolean;
    voice: {
      version_id: string;
      version_no: number;
      speaker: string;
      profile_version_id: string | null;
    } | null;
  }[];
  delivery_id: string | null;
  delivery_reviewed: boolean;
  shared_versions: {
    id: string;
    asset_id: string;
    name: string;
    version_no: number;
    stale: boolean;
  }[];
};

type Props = { projectId: string; episodeId: string };
export default function EpisodeSeriesEvidence({ projectId, episodeId }: Props) {
  const read = useScopedRead<Evidence>(
    `/episodes/${episodeId}/series-evidence`,
  );
  useProjectEvents(read.refresh);
  const data = read.data;
  if (read.error)
    return (
      <Notice tone="error">
        {read.error}
        <Button size="sm" variant="outline" onClick={read.refresh}>
          重试本集证据
        </Button>
      </Notice>
    );
  if (data && (data.project_id !== projectId || data.episode_id !== episodeId))
    return (
      <Notice tone="error">收到的制作证据不属于当前剧集，请刷新后重试。</Notice>
    );
  if (!data)
    return (
      <p role="status" className="text-sm text-muted-foreground">
        正在读取本集连续剧证据…
      </p>
    );

  const list = episodePath(projectId, episodeId, "list");
  const board = data.storyboard_version_id;
  const script = data.script_version_id;
  const audio = episodePath(projectId, episodeId, "audio");
  const delivery =
    data.delivery_id &&
    productionDeliveryPath({
      id: data.delivery_id,
      project_id: projectId,
      episode_id: episodeId,
    });
  return (
    <section
      className="grid min-w-0 gap-3 rounded-lg border p-3 text-sm"
      aria-label="本集连续剧证据"
      aria-busy={read.loading}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="font-medium">本集连续剧证据</h4>
        <span>{phaseLabel(data.phase)}</span>
      </div>
      <div className="flex flex-wrap gap-2 text-muted-foreground">
        {data.outline_version_id && data.outline_episode_id && (
          <Link
            className="underline"
            href={`${episodePath(projectId, data.outline_episode_id, "outline")}&outline=${encodeURIComponent(data.outline_version_id)}`}
          >
            本集引用的精确规划
          </Link>
        )}
        {data.ledger_version_id && (
          <Link
            className="underline"
            href={`${projectPath(projectId, "story")}&ledger=${encodeURIComponent(data.ledger_version_id)}`}
          >
            本集引用的精确账本
          </Link>
        )}
        {data.target_story_time !== null && (
          <span>故事时点 {data.target_story_time}</span>
        )}
        {!data.outline_version_id && <span>当前剧本未绑定全剧规划。</span>}
      </div>
      {!!data.issues.length && (
        <p className="break-words">
          生产阻塞／问题：{data.issues.map(issueLabel).join("、")}
        </p>
      )}
      <dl className="grid gap-3 sm:grid-cols-2">
        <div>
          <dt className="font-medium">表演和视频</dt>
          <dd>
            {board
              ? `${data.performance_shots} 个表演镜头 · 已选并通过时序审核的动态镜头 ${data.reviewed_dynamic_shots}/${data.dynamic_shots}`
              : "暂无分镜证据"}
          </dd>
          {board && (
            <p className="text-muted-foreground">
              可见对白镜头 {data.visible_dialogue_shots} 个 · 已选并通过口型重审{" "}
              {
                data.reviewed_videos.filter((row) => row.lip_sync_reviewed)
                  .length
              }
              /{data.required_lip_sync_shots} 个必需镜头
            </p>
          )}
        </div>
        <div>
          <dt className="font-medium">声音</dt>
          <dd>
            {data.audio_snapshot_id
              ? `当前剧本 ${data.audio_line_count ?? 0} 句，${data.bound_audio_total} 句已绑定音频。绑定不等于批准或选入成片。`
              : "当前剧本尚无配音来源快照。"}
          </dd>
        </div>
      </dl>
      <nav aria-label="本集精确产物" className="flex flex-wrap gap-x-3 gap-y-2">
        {script && (
          <Link className="underline" href={`${list}&node=script:${script}`}>
            查看剧本／局部返修
          </Link>
        )}
        {board && (
          <Link className="underline" href={`${list}&node=storyboard:${board}`}>
            查看精确分镜
          </Link>
        )}
        <Link
          className="underline"
          href={episodePath(projectId, episodeId, "library")}
        >
          视频候选／时序审查和口型
        </Link>
        <Link
          className="underline"
          href={
            data.audio_snapshot_id
              ? `${audio}&audio_snapshot=${data.audio_snapshot_id}`
              : audio
          }
        >
          逐句声音版本
        </Link>
        {delivery && (
          <Link className="underline" href={delivery}>
            {data.delivery_reviewed
              ? "查看本集正式成片"
              : "查看待验收／历史成片"}
          </Link>
        )}
        <Link className="underline" href={projectPath(projectId, "reviews")}>
          查看项目待审
        </Link>
      </nav>
      {!!data.reviewed_videos.length && (
        <details className="border-t pt-2">
          <summary className="cursor-pointer font-medium">
            已选用并通过时序审核的视频
          </summary>
          <ul className="grid gap-1 pt-2">
            {data.reviewed_videos.map((video) => (
              <li key={video.version_id}>
                <Link
                  className="underline"
                  href={`${list}&node=asset-version:${video.version_id}`}
                >
                  镜头 {video.shot_no} · 查看精确视频／审核
                </Link>
                {video.lip_sync_reviewed && <span> · 已完成口型重审</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {!!data.bound_audio.length && (
        <details className="border-t pt-2">
          <summary className="cursor-pointer font-medium">
            逐句绑定状态与实际使用的音色版本
          </summary>
          <p className="pt-2 text-muted-foreground">
            每句显示最近绑定；当前显示 {data.bound_audio.length} /{" "}
            {data.bound_audio_total} 句，完整历史请打开逐句声音版本。
          </p>
          <ul className="grid gap-2 pt-2">
            {data.bound_audio.map((version, index) => (
              <li key={version.version_id} className="grid gap-1 break-words">
                <div className="flex flex-wrap items-center gap-2">
                  <Link
                    className="underline"
                    href={`${list}&node=asset-version:${version.version_id}`}
                  >
                    配音记录 {index + 1} · 音频 v{version.version_no}
                  </Link>
                  <StatusBadge
                    status={version.stale ? "STALE" : version.status}
                  />
                </div>
                {version.voice && (
                  <span className="text-muted-foreground">
                    {version.voice.speaker} · 音色 v{version.voice.version_no}
                    {version.voice.profile_version_id && (
                      <> · 已关联跨集声音档案</>
                    )}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      {!!data.shared_versions.length && (
        <details className="border-t pt-2">
          <summary className="cursor-pointer font-medium">
            本集引用的共享视觉版本
          </summary>
          <ul className="grid gap-1 pt-2">
            {data.shared_versions.map((version) => (
              <li key={version.id}>
                <Link
                  className="underline break-words"
                  href={`${projectPath(projectId, "assets")}&asset=${version.asset_id}&asset_version=${version.id}`}
                >
                  {version.name} · 第 {version.version_no} 版
                  {version.stale ? "（已失效）" : ""}
                </Link>
              </li>
            ))}
          </ul>
        </details>
      )}
      {script && isResourceId(script) && (
        <ExactImpact key={script} projectId={projectId} versionId={script} />
      )}
    </section>
  );
}

const actions = {
  REVIEW: "重新审核",
  GENERATE: "重新生成",
  RENDER: "重新渲染",
};
function ExactImpact({
  projectId,
  versionId,
}: {
  projectId: string;
  versionId: string;
}) {
  const [open, setOpen] = useState(false);
  const read = useScopedRead<DependencyImpact>(
    open
      ? `/projects/${projectId}/dependency-impact?asset_version_id=${versionId}`
      : null,
  );
  const [visible, setVisible] = useState(10);
  return (
    <section className="grid min-w-0 gap-2">
      <Button
        className="justify-self-start"
        size="sm"
        variant="outline"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "收起跨集影响" : "预览本版剧本的跨集影响"}
      </Button>
      {open && read.loading && <p role="status">正在分析精确消费依赖…</p>}
      {open && read.error && <Notice tone="error">{read.error}</Notice>}
      {open && !read.loading && read.data && (
        <>
          <p>
            重新审核 {read.data.summary.REVIEW} · 重新生成{" "}
            {read.data.summary.GENERATE} · 重新渲染 {read.data.summary.RENDER}
            。此处仅预览，不执行变更。
          </p>
          {read.data.affected.length === 0 && <p>没有已登记的下游影响。</p>}
          <ul className="grid gap-2">
            {read.data.affected.slice(0, visible).map((item) => (
              <li
                key={item.asset_version_id}
                className="border-b pb-2 break-words"
              >
                {item.episode_no === undefined
                  ? "项目共享"
                  : `第 ${item.episode_no} 集`}{" "}
                · {item.asset_name} · 第 {item.version_no} 版 ·{" "}
                {actions[item.action]}
                {item.reason && (
                  <p className="text-muted-foreground">{item.reason}</p>
                )}
              </li>
            ))}
          </ul>
          {read.data.affected.length > visible && (
            <Button
              size="sm"
              variant="outline"
              className="justify-self-start"
              onClick={() => setVisible((value) => value + 10)}
            >
              查看更多受影响版本
            </Button>
          )}
        </>
      )}
    </section>
  );
}
