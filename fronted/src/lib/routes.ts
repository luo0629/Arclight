export const projectSections = [
  "canvas",
  "overview",
  "members",
  "story",
  "assets",
  "settings",
] as const;
export type ProjectSection = (typeof projectSections)[number];

export const episodeViews = [
  "canvas",
  "library",
  "list",
  "tasks",
  "render",
  "sources",
  "analysis",
  "outline",
  "create",
] as const;
export type EpisodeView = (typeof episodeViews)[number];

export function projectPath(projectId: string, section?: ProjectSection) {
  const path = `/projects/${encodeURIComponent(projectId)}`;
  return section && section !== "canvas" ? `${path}?section=${section}` : path;
}

export function episodePath(
  projectId: string,
  episodeId: string,
  view?: EpisodeView,
) {
  const path = `${projectPath(projectId)}/episodes/${encodeURIComponent(episodeId)}`;
  return view && view !== "canvas" ? `${path}?view=${view}` : path;
}

export function isResourceId(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value,
  );
}
