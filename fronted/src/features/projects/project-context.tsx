"use client";

import { createContext, useContext } from "react";
import type usePagedList from "@/hooks/use-paged-list";
import type { Episode, Project } from "@/types/api";
import type { EpisodeView } from "@/lib/routes";

type ProjectContextValue = {
  project: Project;
  episode: Episode | null;
  episodes: Episode[];
  episodeList: ReturnType<typeof usePagedList<Episode>>;
  loaded: boolean;
  owner: boolean;
  writable: boolean;
  episodeError: string;
  openEpisode: (
    episode: Episode,
    view?: EpisodeView,
    replace?: boolean,
  ) => void;
  addEpisode: () => void;
  updateEpisode: (episode: Episode) => void;
  reload: () => Promise<void>;
};

export const ProjectContext = createContext<ProjectContextValue | null>(null);

export function useProject() {
  const context = useContext(ProjectContext);
  if (!context) throw new Error("Project layout is required");
  return context;
}
