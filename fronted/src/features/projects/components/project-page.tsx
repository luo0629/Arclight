"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { projectSections, type ProjectSection } from "@/lib/routes";
import { useProject } from "@/features/projects/project-context";
import { useWorkbench } from "@/features/workbench/workbench-context";
import ProjectOverview from "@/features/projects/components/project-overview";
import ProductionTasks from "./production-tasks";
import ProductionReviews from "./production-reviews";
import ProductionDeliveries from "./production-deliveries";
import VisualAssets from "@/features/assets/components/visual-assets";
import Members from "@/features/projects/components/members";
import StoryBible from "@/features/story/components/story-bible";
import Settings from "@/features/projects/components/project-settings";
import ListPagination from "@/components/shared/list-pagination";
import { EmptyState, Notice } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";

export default function ProjectPage() {
  const {
    project,
    episode,
    episodes,
    episodeList,
    owner,
    writable,
    loaded,
    openEpisode,
    addEpisode,
    updateEpisode,
    reload,
  } = useProject();
  const { onDeleted, onProjectSaved, reloadProjects } = useWorkbench();
  const section = useSearchParams().get("section");
  const page = projectSections.includes(section as ProjectSection)
    ? section
    : "canvas";
  const first = episodes[0];
  useEffect(() => {
    if (page === "canvas" && loaded && first)
      openEpisode(first, "canvas", true);
  }, [page, loaded, first, openEpisode]);
  if (page === "settings" && !owner)
    return (
      <Notice>
        {loaded ? "仅项目拥有者可以访问项目设置。" : "正在读取项目权限…"}
      </Notice>
    );
  return page === "overview" ? (
    <ProjectOverview
      key={project.id}
      projectId={project.id}
      onOpen={(row, view) => {
        openEpisode(row, view);
      }}
    />
  ) : page === "tasks" ? (
    <ProductionTasks key={project.id} />
  ) : page === "reviews" ? (
    <ProductionReviews key={project.id} />
  ) : page === "deliveries" ? (
    <ProductionDeliveries key={project.id} />
  ) : page === "assets" ? (
    <VisualAssets
      key={project.id}
      projectId={project.id}
      writable={writable}
      owner={owner}
    />
  ) : page === "members" ? (
    <Members
      key={project.id}
      projectId={project.id}
      owner={owner}
      writable={writable}
    />
  ) : page === "story" ? (
    <StoryBible key={project.id} projectId={project.id} writable={writable} />
  ) : page === "settings" ? (
    <Settings
      onDeleted={onDeleted}
      project={project}
      episodes={
        episode && !episodes.some((row) => row.id === episode.id)
          ? [...episodes, episode]
          : episodes
      }
      pagination={
        <ListPagination
          {...episodeList}
          reload={reload}
          label="剧集"
          count={episodes.length}
        />
      }
      onProjectSaved={onProjectSaved}
      onEpisodeSaved={updateEpisode}
      reload={async () => {
        await reloadProjects();
        await reload();
      }}
    />
  ) : (
    <EmptyState className="empty">
      <h2>
        {loaded && !episodes.length ? "写下第一集的名字吧" : "正在载入剧集…"}
      </h2>
      <p>每一集都有独立的剧本、分镜、画布与任务记录。</p>
      {writable && (
        <Button variant="default" size="lg" className="" onClick={addEpisode}>
          添加第一集
        </Button>
      )}
    </EmptyState>
  );
}
