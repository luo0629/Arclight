"use client";

import {
  PanelLeftClose,
  PanelLeftOpen,
  LayoutDashboard,
  Users,
  Settings2,
  Leaf,
  BookOpen,
  ChartNoAxesCombined,
} from "lucide-react";
import { cn } from "@/lib/utils";

import { Notice, FormLabel } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

import { FieldGroup } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetTrigger,
  SheetClose,
} from "@/components/ui/sheet";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import {
  episodePath,
  projectPath,
  projectSections,
  type EpisodeView,
  type ProjectSection,
} from "@/lib/routes";
import { ProjectContext } from "@/features/projects/project-context";
import { api, json, message } from "@/lib/api/client";
import type { Episode, Project } from "@/types/api";

import EpisodeEditor from "@/features/episodes/components/episode-editor";
import EpisodeCopy from "@/features/episodes/components/episode-copy";
import Dialog from "@/components/shared/app-dialog";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";

export default function ProjectWorkspace({
  project,
  projectPicker,
  directoryOpen,
  setDirectoryOpen,
  onDirectoryOpenAutoFocus,
  children,
}: {
  children: ReactNode;
  project: Project;
  projectPicker: React.ReactNode;
  directoryOpen: boolean;
  setDirectoryOpen: (open: boolean) => void;
  onDirectoryOpenAutoFocus: (event: Event) => void;
}) {
  const episodeList = usePagedList<Episode>(`/projects/${project.id}/episodes`);
  const { rows: episodes, reload: loadEpisodes } = episodeList;
  const [pinnedEpisode, setPinnedEpisode] = useState<Episode | null>(null);
  const router = useRouter();
  const params = useParams<{ episodeId?: string }>();
  const episodeId = params.episodeId ?? "";
  const currentEpisodeId = useRef(episodeId);
  currentEpisodeId.current = episodeId;
  const [episodeError, setEpisodeError] = useState("");
  const lastEpisode = useRef<Episode | null>(null);
  const [role, setRole] = useState("viewer");
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const section = useSearchParams().get("section");
  const page = episodeId
    ? "canvas"
    : projectSections.includes(section as ProjectSection)
      ? section
      : "canvas";
  const sectionIntent = useRef<ProjectSection | null>(null);
  function setPage(next: ProjectSection) {
    sectionIntent.current = next;
    if (next === "canvas") {
      const row = episode ?? lastEpisode.current ?? episodes[0];
      if (row) {
        openEpisode(row);
        return;
      }
    }
    setDirectoryOpen(false);
    router.push(projectPath(project.id, next), { scroll: false });
  }
  const reload = useCallback(async () => {
    const [rows, access] = await Promise.all([
      loadEpisodes(),
      api<{ role: string }>(`/projects/${project.id}/access`),
    ]);
    setRole(access.role);
    const id = currentEpisodeId.current;
    if (id) {
      try {
        const selected =
          rows.find((row) => row.id === id) ??
          (await api<Episode>(`/episodes/${id}`));
        if (selected.project_id !== project.id)
          throw new Error("剧集不属于当前项目。");
        if (currentEpisodeId.current === id) {
          setPinnedEpisode(selected);
          lastEpisode.current = selected;
          setEpisodeError("");
        }
      } catch (error) {
        if (currentEpisodeId.current === id) {
          setPinnedEpisode(null);
          setEpisodeError(message(error));
        }
      }
    }
    setLoaded(true);
    setError("");
  }, [project.id, loadEpisodes]);
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        await reload();
      } catch (error) {
        if (live) setError(message(error));
      }
    })();
    return () => {
      live = false;
    };
  }, [reload, episodeId]);
  const owner = role === "owner";
  const writable = role !== "viewer" && !project.archived;
  const episode =
    episodes.find((row) => row.id === episodeId) ??
    (pinnedEpisode?.id === episodeId ? pinnedEpisode : null) ??
    (!episodeId ? lastEpisode.current : null);
  const openEpisode = useCallback(
    (row: Episode, view: EpisodeView = "canvas", replace = false) => {
      // 初始首集跳转不能覆盖用户在数据载入时明确选择的项目页面。
      if (
        replace &&
        sectionIntent.current &&
        sectionIntent.current !== "canvas"
      )
        return;
      if (!replace) sectionIntent.current = null;
      setPinnedEpisode(row);
      lastEpisode.current = row;
      if (!replace) setDirectoryOpen(false);
      router[replace ? "replace" : "push"](
        episodePath(project.id, row.id, view),
        { scroll: false },
      );
    },
    [project.id, router, setDirectoryOpen],
  );
  function updateEpisode(updated: Episode) {
    episodeList.update(updated);
    setPinnedEpisode(updated);
    if (lastEpisode.current?.id === updated.id) lastEpisode.current = updated;
  }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const row = await api<Episode>(
        `/projects/${project.id}/episodes`,
        json("POST", {
          episode_no: Number(data.get("number")),
          title: data.get("title"),
          target_duration_ms: Math.round(Number(data.get("duration")) * 1000),
        }),
      );
      await reload();
      setPinnedEpisode(row);
      setAdding(false);
      setDirectoryOpen(false);
      openEpisode(row);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <ProjectContext.Provider
      value={{
        project,
        episode,
        episodes,
        episodeList,
        loaded,
        owner,
        writable,
        episodeError,
        openEpisode,
        addEpisode: () => setAdding(true),
        updateEpisode,
        reload,
      }}
    >
      <Sheet open={directoryOpen} onOpenChange={setDirectoryOpen}>
        <div className="app-shell project-shell">
          <nav
            aria-label="项目功能"
            className="flex flex-wrap items-center gap-2 border-b bg-card px-4 py-3 lg:px-6"
          >
            <SheetTrigger asChild>
              <Button variant="ghost">
                {directoryOpen ? <PanelLeftClose /> : <PanelLeftOpen />}
                {directoryOpen ? "收起目录" : "展开目录"}
              </Button>
            </SheetTrigger>
            <Separator
              orientation="vertical"
              className="mx-1 hidden h-5 sm:block"
            />
            <Button
              variant={page === "canvas" ? "secondary" : "ghost"}
              aria-pressed={page === "canvas"}
              onClick={() => setPage("canvas")}
            >
              <LayoutDashboard />
              故事创作
            </Button>
            <Button
              variant={page === "overview" ? "secondary" : "ghost"}
              aria-pressed={page === "overview"}
              onClick={() => setPage("overview")}
            >
              <ChartNoAxesCombined data-icon="inline-start" />
              项目总览
            </Button>
            <Button
              variant={page === "members" ? "secondary" : "ghost"}
              aria-pressed={page === "members"}
              onClick={() => setPage("members")}
            >
              <Users />
              创作伙伴
            </Button>
            <Button
              variant={page === "story" ? "secondary" : "ghost"}
              aria-pressed={page === "story"}
              onClick={() => setPage("story")}
            >
              <BookOpen />
              故事设定
            </Button>
            <Button
              variant={page === "assets" ? "secondary" : "ghost"}
              aria-pressed={page === "assets"}
              onClick={() => setPage("assets")}
            >
              <Leaf data-icon="inline-start" />
              视觉资产
            </Button>
            {owner && (
              <Button
                variant={page === "settings" ? "secondary" : "ghost"}
                aria-pressed={page === "settings"}
                onClick={() => setPage("settings")}
              >
                <Settings2 />
                项目设置与归档
              </Button>
            )}
          </nav>
          <div
            className={cn("project-body", directoryOpen && "directory-is-open")}
            aria-busy={
              !loaded ||
              episodeList.loading ||
              (!episodeId && page === "canvas" && episodes.length > 0)
            }
          >
            <SheetContent
              onOpenAutoFocus={onDirectoryOpenAutoFocus}
              id="project-directory"
              side="left"
              showCloseButton={false}
              aria-describedby={undefined}
              className="studio-directory"
            >
              <div className="directory-heading">
                <SheetTitle>项目与剧集</SheetTitle>
                <SheetClose asChild>
                  <Button variant="ghost" size="icon" aria-label="关闭项目目录">
                    <PanelLeftClose />
                  </Button>
                </SheetClose>
              </div>
              {projectPicker}
              <Separator />
              <div className="section-heading flex flex-wrap items-center justify-between gap-2">
                <h2>剧集目录</h2>
                <span
                  className="count rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  title="已加载剧集数，不代表总数"
                >
                  {episodes.length}
                  {episodeList.hasMore ? "+" : ""}
                </span>
              </div>
              <ScrollArea className="directory-episodes">
                <nav
                  aria-label="剧集列表"
                  className="episode-nav flex flex-col gap-1"
                >
                  {(episode && !episodes.some((row) => row.id === episode.id)
                    ? [...episodes, episode]
                    : episodes
                  ).map((row) => (
                    <Button
                      variant={row.id === episodeId ? "secondary" : "ghost"}
                      size="lg"
                      aria-pressed={row.id === episodeId}
                      className={cn(
                        "h-auto justify-start gap-3 px-2 py-3 text-left whitespace-normal [&_small]:mt-1 [&_small]:block [&_small]:text-xs [&_small]:font-normal [&_small]:text-muted-foreground",
                        row.id === episodeId && "active",
                      )}
                      key={row.id}
                      onClick={() => {
                        setPinnedEpisode(row);
                        openEpisode(row);
                      }}
                    >
                      <span className="episode-number flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-xs text-secondary-foreground">
                        {String(row.episode_no).padStart(2, "0")}
                      </span>
                      <span>
                        {row.title}
                        <small>
                          {row.status === "ARCHIVED"
                            ? "已归档"
                            : `${row.target_duration_ms / 1000} 秒目标时长`}
                        </small>
                      </span>
                    </Button>
                  ))}
                </nav>
              </ScrollArea>
              <ListPagination
                {...episodeList}
                reload={reload}
                label="剧集"
                count={episodes.length}
              />
              {writable && (
                <Button
                  variant="outline"
                  size="lg"
                  className="dashed-button w-full border-dashed"
                  onClick={() => setAdding(true)}
                >
                  ＋ 添加一集
                </Button>
              )}
              <div className="sidebar-bottom mt-auto flex flex-col gap-2 pt-6">
                <p className="hint text-xs leading-relaxed text-muted-foreground">
                  {owner
                    ? "你是项目拥有者"
                    : role === "editor"
                      ? "你可以编辑与创建任务"
                      : "你当前拥有只读权限"}
                </p>
                <p className="version-note mt-4 text-[10px] leading-relaxed text-muted-foreground">
                  每一份灵感，都值得好好收藏。
                </p>
              </div>
            </SheetContent>
            <main
              className={`workspace project-main ${page === "canvas" ? "is-creation" : "is-management"}`}
            >
              <div className="workspace-top flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="breadcrumb mb-1 text-xs text-muted-foreground">
                    我的故事 / {project.name}
                    {project.archived ? " / 已归档" : ""}
                  </p>
                  <h1>
                    {page === "overview"
                      ? "项目总览"
                      : page === "members"
                        ? "创作伙伴"
                        : page === "story"
                          ? "故事设定"
                          : page === "settings"
                            ? "项目设置"
                            : episode
                              ? `第 ${episode.episode_no} 集 · ${episode.title}`
                              : "你的故事，准备开场"}
                  </h1>
                  {page === "canvas" && episode && role !== "viewer" && (
                    <div className="flex flex-wrap items-center gap-2">
                      <EpisodeEditor
                        key={episode.id}
                        episode={episode}
                        disabled={!writable || episode.status === "ARCHIVED"}
                        onSaved={updateEpisode}
                      />
                      <EpisodeCopy
                        key={"copy-" + episode.id}
                        episode={episode}
                        suggestedNumber={
                          Math.max(
                            episode.episode_no,
                            ...episodes.map((row) => row.episode_no),
                          ) + 1
                        }
                        disabled={!writable || episode.status === "ARCHIVED"}
                        onCopied={(copied) => {
                          setPinnedEpisode(copied);
                          openEpisode(copied);
                          // A successful copy stays successful even if the directory refresh fails.
                          void loadEpisodes().catch((error) =>
                            setError(message(error)),
                          );
                        }}
                      />
                    </div>
                  )}
                </div>
                <span className="hidden items-center gap-2 rounded-full bg-accent px-3 py-1.5 text-xs text-accent-foreground md:flex">
                  <Leaf className="size-3.5" />
                  让灵感慢慢生长
                </span>
              </div>
              {error && (
                <Notice
                  tone="error"
                  className="error workspace-error mb-4"
                  role="alert"
                >
                  {error}
                  <Button
                    variant="outline"
                    size="lg"
                    onClick={() =>
                      void reload().catch((error) => setError(message(error)))
                    }
                  >
                    重试
                  </Button>
                </Notice>
              )}
              {children}
            </main>
          </div>
          {adding && (
            <Dialog
              labelledBy="episode-title"
              busy={busy}
              onDismiss={() => setAdding(false)}
            >
              <h2 id="episode-title">开始新的一集</h2>
              <form className="flex min-w-0 flex-col gap-4" onSubmit={create}>
                <FieldGroup className="contents">
                  <FormLabel>
                    剧集标题
                    <Input name="title" required maxLength={160} autoFocus />
                  </FormLabel>
                  <div className="field-pair grid min-w-0 grid-cols-1 items-start gap-4 sm:grid-cols-2">
                    <FormLabel>
                      集数
                      {episodeList.hasMore && (
                        <small className="hint text-xs leading-relaxed text-muted-foreground">
                          目录尚未全部加载，请核对集数，避免重复。
                        </small>
                      )}
                      <Input
                        name="number"
                        type="number"
                        min="1"
                        defaultValue={
                          Math.max(
                            0,
                            ...episodes.map((row) => row.episode_no),
                          ) + 1
                        }
                        required
                      />
                    </FormLabel>
                    <FormLabel>
                      目标时长（秒）
                      <Input
                        name="duration"
                        type="number"
                        min="2.5"
                        max="1800"
                        step="0.001"
                        defaultValue={
                          project.story_settings.default_episode_duration_ms /
                          1000
                        }
                        required
                      />
                    </FormLabel>
                  </div>
                  {error && (
                    <Notice tone="error" className="error" role="alert">
                      {error}
                    </Notice>
                  )}
                  <div className="button-row flex flex-wrap items-center gap-2">
                    <Button
                      variant="outline"
                      size="lg"
                      type="button"
                      disabled={busy}
                      onClick={() => setAdding(false)}
                    >
                      取消
                    </Button>
                    <Button
                      variant="default"
                      size="lg"
                      className=""
                      disabled={busy}
                    >
                      {busy ? "创建中…" : "创建剧集"}
                    </Button>
                  </div>
                </FieldGroup>
              </form>
            </Dialog>
          )}
        </div>
      </Sheet>
    </ProjectContext.Provider>
  );
}
