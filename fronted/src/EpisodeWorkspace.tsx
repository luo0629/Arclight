import { cn } from "@/lib/utils";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Notice, EmptyState, StatusBadge } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  message,
  statusText,
  type ContentVersion,
  type Episode,
  type Graph,
  type GraphNode,
  type Run,
} from "./api";
import { lazy, Suspense } from "react";
import { LoadingState } from "@/components/ui-kit";
const Canvas = lazy(() => import("./Canvas"));
import Inspector from "./Inspector";

import Tasks from "./Tasks";

import usePagedList from "./usePagedList";
import ListPagination from "./ListPagination";
import type { VersionCreated, VersionKind } from "./VersionEditor";

type EpisodeData = { graph: Graph };
export default function EpisodeWorkspace({
  episode,
  owner,
  writable,
}: {
  episode: Episode;
  owner: boolean;
  writable: boolean;
}) {
  const [data, setData] = useState<EpisodeData | null>(null);
  const prefix = `/episodes/${episode.id}`;
  const scripts = usePagedList<ContentVersion>(prefix + "/scripts");
  const boards = usePagedList<ContentVersion>(prefix + "/storyboards");
  const runs = usePagedList<Run>(prefix + "/runs");
  const { reload: loadScripts, ensure: ensureScript } = scripts;
  const { reload: loadBoards, ensure: ensureBoard } = boards;
  const { reload: loadRuns } = runs;
  const [selectedId, setSelectedId] = useState("");
  const [saved, setSaved] = useState<{
    version: ContentVersion;
    kind: VersionKind;
  } | null>(null);
  const [savedRefreshError, setSavedRefreshError] = useState("");
  const [showSavedNotice, setShowSavedNotice] = useState(false);
  const [view, setView] = useState(() =>
    window.matchMedia("(max-width: 900px)").matches ? "list" : "canvas",
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const mounted = useRef(true);
  const sequence = useRef(0);
  const reload = useCallback(async () => {
    const ticket = ++sequence.current;
    const prefix = `/episodes/${episode.id}`;
    const [, , , graph] = await Promise.all([
      loadScripts(),
      loadBoards(),
      loadRuns(),
      api<Graph>(prefix + "/canvas"),
    ]);
    if (mounted.current && ticket === sequence.current) setData({ graph });
  }, [
    episode.id,
    episode.title,
    episode.target_duration_ms,
    loadScripts,
    loadBoards,
    loadRuns,
  ]);
  useEffect(() => {
    mounted.current = true;
    void reload().catch((error) => {
      if (mounted.current) setError(message(error));
    });
    return () => {
      mounted.current = false;
      sequence.current++;
    };
  }, [reload]);
  const active = runs.rows.some((run) =>
    ["QUEUED", "RUNNING", "WAITING_REVIEW"].includes(run.status),
  );
  useEffect(() => {
    if (!active) return;
    let polling = false;
    const timer = window.setInterval(() => {
      if (polling || document.hidden) return;
      polling = true;
      void reload()
        .catch((error) => {
          if (mounted.current) setError(message(error));
        })
        .finally(() => {
          polling = false;
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [active, reload]);
  async function act(action: () => Promise<unknown>) {
    if (busy) return false;
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (error) {
      setError(message(error));
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    setRefreshing(true);
    if (await act(reload)) setSavedRefreshError("");
    setRefreshing(false);
  }
  const onCreated: VersionCreated = async (version, kind) => {
    if (!mounted.current) return;
    setSaved({ version, kind });
    setShowSavedNotice(true);
    setSelectedId(kind + ":" + version.id);
    setSavedRefreshError("");
    // 保留 POST 回执供详情兜底，不向分页 rows 插入记录，避免污染服务端 offset。
    try {
      await reload();
    } catch (error) {
      if (mounted.current) setSavedRefreshError(message(error));
    }
  };
  const scriptRows =
    saved?.kind === "script" &&
    !scripts.rows.some((row) => row.id === saved.version.id)
      ? [saved.version, ...scripts.rows]
      : scripts.rows;
  const boardRows =
    saved?.kind === "storyboard" &&
    !boards.rows.some((row) => row.id === saved.version.id)
      ? [saved.version, ...boards.rows]
      : boards.rows;
  const savedVersion =
    saved &&
    (saved.kind === "script" ? scriptRows : boardRows).find(
      (row) => row.id === saved.version.id,
    );
  const savedNode: GraphNode | null = saved
    ? {
        id: saved.kind + ":" + saved.version.id,
        kind: saved.kind,
        resource_id: saved.version.id,
        title:
          saved.kind === "script"
            ? String(saved.version.content_json.title)
            : "分镜",
        version_no: saved.version.version_no,
        status: saved.version.status,
      }
    : null;
  const selected =
    data?.graph.nodes.find((node) => node.id === selectedId) ??
    (savedNode?.id === selectedId ? savedNode : null) ??
    data?.graph.nodes[0] ??
    null;
  // 画布仍是完整依赖图；点选未加载的历史节点时读取到该精确版本。
  const selectedResource = selected?.resource_id;
  const selectedKind = selected?.kind;
  const versionMissing =
    !!selectedResource &&
    (selectedKind === "script"
      ? !scriptRows.some((row) => row.id === selectedResource)
      : selectedKind === "storyboard" &&
        !boardRows.some((row) => row.id === selectedResource));
  useEffect(() => {
    if (!selectedResource || !versionMissing) return;
    void (
      selectedKind === "script"
        ? ensureScript(selectedResource)
        : ensureBoard(selectedResource)
    ).catch(() => {});
  }, [
    selectedResource,
    selectedKind,
    versionMissing,
    ensureScript,
    ensureBoard,
  ]);
  const loadedIds = new Set([
    ...scriptRows.map((row) => "script:" + row.id),
    ...boardRows.map((row) => "storyboard:" + row.id),
  ]);
  data?.graph.edges.forEach((edge) => {
    if (loadedIds.has(edge.source) && edge.target.startsWith("shot:"))
      loadedIds.add(edge.target);
  });
  const visibleNodes =
    data?.graph.nodes.filter(
      (node) => node.kind === "episode" || loadedIds.has(node.id),
    ) ?? [];
  if (savedNode && !visibleNodes.some((node) => node.id === savedNode.id))
    visibleNodes.splice(1, 0, savedNode);
  const versionPagination = (
    <>
      <ListPagination
        {...scripts}
        label="剧本版本"
        count={scripts.rows.length}
      />
      <ListPagination {...boards} label="分镜版本" count={boards.rows.length} />
    </>
  );
  return (
    <>
      <div className="workspace-toolbar mb-4 flex flex-wrap items-center justify-between gap-3">
        <ToggleGroup
          type="single"
          value={view}
          onValueChange={(value) => {
            if (value) setView(value);
          }}
          variant="outline"
          aria-label="工作区视图"
          className="flex-wrap"
        >
          {[
            ["canvas", "创作画布"],
            ["list", "版本列表"],
            ["tasks", "任务与审核"],
          ].map(([key, label]) => (
            <ToggleGroupItem key={key} value={key}>
              {label}
              {key === "tasks" &&
                runs.rows.some((run) => run.pending_review) && (
                  <span className="size-1.5 rounded-full bg-primary" />
                )}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <Button
          variant="outline"
          size="lg"
          disabled={busy || refreshing}
          onClick={() => void refresh()}
        >
          {refreshing ? "刷新中…" : "刷新内容"}
        </Button>
      </div>
      {error && (
        <Notice
          tone="error"
          className="error workspace-error mb-4"
          role="alert"
        >
          {error}
          <Button variant="outline" size="lg" onClick={() => setError("")}>
            收起提示
          </Button>
        </Notice>
      )}
      {saved && showSavedNotice && (
        <Notice
          tone="success"
          className="success saved-version-notice mb-4"
          role="status"
        >
          <strong>
            人工{saved.kind === "script" ? "剧本" : "分镜"}已保存为新版本 v
            {saved.version.version_no} ·{" "}
            {statusText[savedVersion?.status ?? saved.version.status]}
          </strong>
          <p>
            已自动打开该版本；原版本保持不变，未自动批准。
            {savedVersion?.status === "READY_FOR_REVIEW"
              ? owner
                ? "请在内容与属性中审核。"
                : "请等待项目拥有者审核。"
              : "审核状态已更新。"}
          </p>
          {savedRefreshError && (
            <p>
              新版本已保存，但列表／画布刷新失败：{savedRefreshError}
              。无需再次保存。
              <Button
                variant="outline"
                size="lg"
                disabled={busy || refreshing}
                onClick={() => void refresh()}
              >
                重试刷新已保存版本
              </Button>
            </p>
          )}
          <Button
            variant="outline"
            size="lg"
            onClick={() => setShowSavedNotice(false)}
            disabled={busy || !!savedRefreshError}
          >
            收起保存提示
          </Button>
        </Notice>
      )}
      {!writable && (
        <Notice tone="info" className="read-only mb-4" role="status">
          当前为只读模式：项目／剧集已归档，或你的角色是查看者。
        </Notice>
      )}
      {!data ? (
        <EmptyState className="empty" role="status">
          <h2>正在整理这一集的便签…</h2>
          <p>如果载入失败，请检查 API 服务并点击刷新内容。</p>
        </EmptyState>
      ) : view === "tasks" ? (
        <Tasks
          runs={runs.rows}
          pagination={
            <ListPagination {...runs} label="任务" count={runs.rows.length} />
          }
          owner={owner}
          writable={writable}
          busy={busy}
          act={act}
          reload={reload}
        />
      ) : (
        <div className="editor-layout grid min-w-0 grid-cols-1 items-start gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
          <section
            className="creation-area flex min-w-0 flex-col gap-4"
            aria-label="创作内容"
          >
            {view === "canvas" ? (
              <Suspense
                fallback={<LoadingState>正在打开创作画布…</LoadingState>}
              >
                <Canvas
                  graph={data.graph}
                  selectedId={selected?.id}
                  focusId={
                    savedNode?.id === selected?.id ? savedNode?.id : undefined
                  }
                  episodeId={episode.id}
                  writable={writable}
                  onSelect={(node: GraphNode) => setSelectedId(node.id)}
                />
              </Suspense>
            ) : (
              <div className="version-list flex min-w-0 flex-col gap-3 rounded-xl border bg-card p-4">
                <div className="section-heading flex flex-wrap items-center justify-between gap-2">
                  <h2>故事的每一稿</h2>
                  <span className="muted text-sm text-muted-foreground">
                    选择一项查看与编辑
                  </span>
                </div>
                {visibleNodes.map((node) => (
                  <Button
                    variant="outline"
                    size="lg"
                    key={node.id}
                    className={cn(
                      "version-row h-auto justify-start gap-3 py-4 text-left whitespace-normal [&>span:nth-child(2)]:min-w-0 [&>span:nth-child(2)]:flex-1 [&_strong]:block [&_small]:mt-1 [&_small]:block [&_small]:text-xs [&_small]:text-muted-foreground",
                      selected?.id === node.id && "active ring-2 ring-ring",
                    )}
                    aria-pressed={selected?.id === node.id}
                    onClick={() => setSelectedId(node.id)}
                  >
                    <span
                      className={cn(
                        "kind-dot size-2.5 shrink-0 rounded-full bg-primary",
                        node.kind,
                        node.kind === "script" && "bg-secondary-foreground",
                        node.kind === "shot" && "bg-success",
                      )}
                      aria-hidden="true"
                    />
                    <span>
                      <strong>{node.title}</strong>
                      <small>
                        {node.version_no
                          ? `版本 ${node.version_no}`
                          : node.kind === "episode"
                            ? "故事起点"
                            : "镜头"}
                      </small>
                    </span>
                    <StatusBadge status={node.status} />
                  </Button>
                ))}
                {versionPagination}
              </div>
            )}
            {view === "canvas" && versionPagination}
          </section>
          <Inspector
            node={selected}
            episode={episode}
            scripts={scriptRows}
            boards={boardRows}
            runs={runs.rows}
            owner={owner}
            writable={writable}
            busy={busy}
            act={act}
            reload={reload}
            versionLoading={scripts.loading || boards.loading}
            onCreated={onCreated}
            focusVersionId={saved?.version.id}
            retryVersion={() =>
              selectedResource
                ? selectedKind === "script"
                  ? ensureScript(selectedResource)
                  : ensureBoard(selectedResource)
                : Promise.resolve()
            }
            showTasks={() => setView("tasks")}
          />
        </div>
      )}
    </>
  );
}
