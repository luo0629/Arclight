"use client";

import Link from "next/link";
import { EmptyState } from "@/components/ui-kit";
import { projectPath } from "@/lib/routes";
import { useProject } from "@/features/projects/project-context";
import EpisodeWorkspace from "@/features/episodes/components/episode-workspace";

export default function EpisodePage() {
  const { episode, project, owner, writable, episodeError } = useProject();
  if (!episode)
    return (
      <EmptyState className="empty">
        <h2>{episodeError ? "无法打开此剧集" : "正在载入剧集…"}</h2>
        {episodeError && (
          <>
            <p>剧集不存在、无权访问，或不属于当前项目。</p>
            <Link
              href={projectPath(project.id)}
              className="text-primary underline"
            >
              返回项目
            </Link>
          </>
        )}
      </EmptyState>
    );
  return (
    <EpisodeWorkspace
      key={episode.id}
      episode={episode}
      owner={owner}
      writable={writable && episode.status !== "ARCHIVED"}
    />
  );
}
