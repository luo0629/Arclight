import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, message, statusText, type Episode } from "./api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  EmptyState,
  LoadingState,
  Notice,
  PagePanel,
  StatusBadge,
} from "@/components/ui-kit";

type Overview = {
  project_id: string;
  project_name: string;
  archived: boolean;
  generated_at: string;
  episode_count: number;
  active_episode_count: number;
  archived_episode_count: number;
  stage_counts: Record<string, number>;
  task_counts: Record<string, number>;
  task_total: number;
  pending_reviews: {
    workflows: number;
    manual_scripts: number;
    manual_storyboards: number;
    outline_confirmations: number;
    total: number;
  };
  cost: {
    budget_limit: string;
    actual_cost: null;
    currency: null;
    status: "UNAVAILABLE";
  };
  deliveries: { status: "UNAVAILABLE"; version_count: null };
  episodes: (Episode & { stage: string })[];
  recent_tasks: {
    id: string;
    episode_id: string;
    episode_no: number;
    episode_title: string;
    workflow_type: string;
    status: string;
    created_at: string;
    updated_at: string;
  }[];
};

const stages: Record<string, string> = {
  NOT_STARTED: "尚无文本产物",
  ANALYSIS_READY: "故事已提炼",
  OUTLINE_DRAFT: "大纲待确认",
  OUTLINE_APPROVED: "大纲已确认",
  SCRIPT_DRAFT: "剧本草稿",
  SCRIPT_REVIEW: "剧本待审核",
  SCRIPT_REVISION: "剧本需修改",
  SCRIPT_APPROVED: "剧本已批准",
  STORYBOARD_REVIEW: "分镜待审核",
  STORYBOARD_REVISION: "分镜需修改",
  STORYBOARD_APPROVED: "分镜已批准",
  ARCHIVED: "已归档",
};
const workflows: Record<string, string> = {
  script_storyboard: "编剧与分镜",
  storyboard: "分镜创作",
  story_analysis: "故事提炼",
  story_outline: "分集大纲",
  script_rewrite: "局部改写",
};
function time(value: string) {
  return new Date(
    /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + "Z",
  ).toLocaleString();
}

export default function ProjectOverview({
  projectId,
  onOpen,
}: {
  projectId: string;
  onOpen: (episode: Episode, view: "canvas" | "tasks") => void;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [opening, setOpening] = useState("");
  const refresh = useRef<() => void>(() => {});
  const navigation = useRef<AbortController | null>(null);
  const limit = 20;
  useEffect(() => {
    let live = true;
    let fetching = false;
    let request: AbortController | null = null;
    setData(null);
    setError("");
    setOpening("");
    async function load() {
      if (fetching || !live) return;
      fetching = true;
      request = new AbortController();
      setLoading(true);
      try {
        const result = await api<Overview>(
          `/projects/${projectId}/overview?limit=${limit}&offset=${offset}`,
          { signal: request.signal },
        );
        if (live) {
          setData(result);
          setError("");
        }
      } catch (error) {
        if (live && !request.signal.aborted) setError(message(error));
      } finally {
        fetching = false;
        if (live) setLoading(false);
      }
    }
    refresh.current = () => void load();
    void load();
    const poll = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 15000);
    const visible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      live = false;
      request?.abort();
      navigation.current?.abort();
      window.clearInterval(poll);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [projectId, offset]);

  async function openTask(episodeId: string) {
    navigation.current?.abort();
    const request = new AbortController();
    navigation.current = request;
    setOpening(episodeId);
    try {
      const episode = await api<Episode>(`/episodes/${episodeId}`, {
        signal: request.signal,
      });
      if (!request.signal.aborted) onOpen(episode, "tasks");
    } catch (error) {
      if (!request.signal.aborted) setError(message(error));
    } finally {
      if (!request.signal.aborted) setOpening("");
    }
  }

  return (
    <div
      className="flex min-w-0 flex-col gap-5"
      aria-label="项目总览数据"
      aria-busy={loading}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {data
            ? `更新于 ${time(data.generated_at)} · 可见时每 15 秒刷新`
            : "正在读取项目记录"}
        </p>
        <Button
          variant="outline"
          disabled={loading}
          onClick={() => refresh.current()}
        >
          <RefreshCw data-icon="inline-start" />
          刷新总览
        </Button>
      </div>
      {error && (
        <Notice tone="error">
          {error}
          {data && "当前保留上次成功读取的数据，尚未刷新。"}
          <Button
            variant="outline"
            disabled={loading}
            onClick={() => refresh.current()}
          >
            重试总览
          </Button>
        </Notice>
      )}
      {!data && loading && <LoadingState>正在汇总全项目记录…</LoadingState>}
      {data && (
        <>
          {data.archived && (
            <Notice>
              项目已归档，当前总览仅供查阅；恢复项目后才能继续生产与审核。
            </Notice>
          )}
          <div className="grid min-w-0 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {[
              [
                "剧集",
                String(data.episode_count),
                `生产中 ${data.active_episode_count} · 已归档 ${data.archived_episode_count}`,
              ],
              [
                "待处理审核与确认",
                String(data.pending_reviews.total),
                "按待处理对象计数，不重复计算任务产物",
              ],
              ["文本任务", String(data.task_total), "所有未归档剧集的累计任务"],
              [
                "已批准分镜的剧集",
                String(data.stage_counts.STORYBOARD_APPROVED ?? 0),
                "仅代表文本分镜阶段，不代表成片完成",
              ],
            ].map(([title, value, note]) => (
              <Card className="min-w-0 shadow-none" key={title}>
                <CardHeader>
                  <CardTitle className="text-sm">{title}</CardTitle>
                </CardHeader>
                <CardContent className="flex flex-col gap-2">
                  <p className="text-3xl font-semibold tabular-nums">{value}</p>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    {note}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
          <PagePanel
            title="生产阶段"
            description="全项目未归档剧集；以最新文本版本及精确来源关系判定，不按任务成功率推算成片进度。大纲阶段对应产出大纲的剧集。"
          >
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.stage_counts).map(([stage, count]) => (
                <Badge variant="secondary" key={stage}>
                  {stages[stage] ?? stage} · {count}
                </Badge>
              ))}
            </div>
            {data.active_episode_count === 0 && (
              <p className="text-sm text-muted-foreground">
                暂无生产中的剧集。
              </p>
            )}
            <p className="text-sm leading-relaxed text-muted-foreground">
              画面、配音、时间轴与导出尚未接入；总览仅覆盖已落库的文本生产记录。
            </p>
          </PagePanel>
          <div className="grid min-w-0 gap-5 lg:grid-cols-2">
            <PagePanel
              title="审核与确认"
              description="统计未归档剧集的所有待审版本；归档项目保留历史待处理数。仅拥有者可在对应工作区执行审核。"
            >
              <dl className="grid grid-cols-[1fr_auto] gap-3 text-sm">
                {[
                  ["工作流等待审核", data.pending_reviews.workflows],
                  ["人工剧本待审核", data.pending_reviews.manual_scripts],
                  ["人工分镜待审核", data.pending_reviews.manual_storyboards],
                  [
                    "大纲草稿待确认",
                    data.pending_reviews.outline_confirmations,
                  ],
                ].map(([label, count]) => (
                  <div className="contents" key={label}>
                    <dt>{label}</dt>
                    <dd className="tabular-nums">{count}</dd>
                  </div>
                ))}
              </dl>
              <p className="text-xs leading-relaxed text-muted-foreground">
                待处理数包含历史待审版本；批准仍须满足来源、引用与时长校验。视觉资产审核尚未接入。
              </p>
            </PagePanel>
            <PagePanel
              title="成本与成片"
              description="如实区分已配置预算、实际账单和交付记录。"
            >
              <dl className="flex flex-col gap-3 text-sm">
                <div>
                  <dt className="text-muted-foreground">
                    已配置预算上限（未定义币种）
                  </dt>
                  <dd className="mt-1 text-xl font-semibold tabular-nums">
                    {data.cost.budget_limit}
                  </dd>
                </div>
                <div>
                  <dt className="font-medium">累计实际成本：暂无成本账本</dt>
                  <dd className="mt-1 text-muted-foreground">
                    不展示为 0，也不据此计算剩余预算。
                  </dd>
                </div>
                <div>
                  <dt className="font-medium">成片版本：尚未接入交付记录</dt>
                  <dd className="mt-1 text-muted-foreground">
                    当前分镜与通用资产均不计作成片。
                  </dd>
                </div>
              </dl>
            </PagePanel>
          </div>
          <PagePanel
            title="剧集生产目录"
            description={`共 ${data.episode_count} 集（含归档），当前第 ${data.episode_count ? offset + 1 : 0}–${Math.min(offset + data.episodes.length, data.episode_count)} 集；汇总数字覆盖全部剧集。`}
          >
            {data.episodes.length ? (
              <div className="flex min-w-0 flex-col gap-3">
                {data.episodes.map((episode) => (
                  <div
                    key={episode.id}
                    className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div className="flex min-w-0 flex-1 flex-col gap-2">
                      <h3 className="break-words text-sm font-medium">
                        第 {episode.episode_no} 集 · {episode.title}
                      </h3>
                      <p className="text-xs text-muted-foreground">
                        目标 {episode.target_duration_ms / 1000} 秒
                      </p>
                      <Badge className="w-fit" variant="outline">
                        {stages[episode.stage] ?? episode.stage}
                      </Badge>
                    </div>
                    <Button
                      variant="outline"
                      onClick={() => onOpen(episode, "canvas")}
                      aria-label={`进入第 ${episode.episode_no} 集`}
                    >
                      进入工作区
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState>
                <h3>暂无剧集记录</h3>
                <p>从项目目录创建第一集后，这里将显示真实进度。</p>
              </EmptyState>
            )}
            <nav
              aria-label="总览剧集分页"
              className="flex flex-wrap items-center justify-between gap-3"
            >
              <Button
                variant="outline"
                disabled={loading || offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - limit))}
              >
                上一页剧集
              </Button>
              <span className="text-sm text-muted-foreground">
                每页 {limit} 集
              </span>
              <Button
                variant="outline"
                disabled={loading || offset + limit >= data.episode_count}
                onClick={() => setOffset((value) => value + limit)}
              >
                下一页剧集
              </Button>
            </nav>
          </PagePanel>
          <PagePanel
            title="文本任务进度"
            description="按任务条数统计各状态，不表示镜头或成片完成百分比。最近列表按创建时间排序，最多显示 10 条。"
          >
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.task_counts).map(([status, count]) => (
                <Badge key={status} variant="secondary">
                  {statusText[status] ?? status} · {count}
                </Badge>
              ))}
            </div>
            {data.recent_tasks.length ? (
              <div className="flex min-w-0 flex-col gap-3">
                {data.recent_tasks.map((task) => (
                  <div
                    key={task.id}
                    className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div className="flex min-w-0 flex-1 flex-col gap-2">
                      <p className="break-words text-sm font-medium">
                        第 {task.episode_no} 集 · {task.episode_title} ·{" "}
                        {workflows[task.workflow_type] ?? task.workflow_type}
                      </p>
                      <StatusBadge status={task.status} />
                      <p className="text-xs text-muted-foreground">
                        创建于 {time(task.created_at)} · 更新于{" "}
                        {time(task.updated_at)}
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      disabled={!!opening}
                      onClick={() => void openTask(task.episode_id)}
                    >
                      查看该集任务
                    </Button>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState>
                <h3>暂无文本任务</h3>
                <p>任务启动后将从真实记录汇总状态。</p>
              </EmptyState>
            )}
          </PagePanel>
        </>
      )}
    </div>
  );
}
