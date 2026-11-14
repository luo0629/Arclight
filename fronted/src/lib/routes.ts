export const projectSections = [
  "canvas",
  "overview",
  "members",
  "story",
  "assets",
  "tasks",
  "reviews",
  "deliveries",
  "settings",
] as const;
export type ProjectSection = (typeof projectSections)[number];

export const episodeViews = [
  "canvas",
  "library",
  "list",
  "tasks",
  "audio",
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

type ProductionTaskDestination = {
  id: string;
  project_id: string;
  episode_id?: string | null;
  family: "WORKFLOW" | "GENERATION" | "RENDER";
};

/** Build only local, known destinations from validated resource identities. */
export function productionTaskPath(row: ProductionTaskDestination) {
  if (!isResourceId(row.id) || !isResourceId(row.project_id)) return null;
  const episodeId = row.episode_id;
  if (episodeId && !isResourceId(episodeId)) return null;
  if (row.family === "GENERATION") {
    const path = episodeId
      ? episodePath(row.project_id, episodeId, "tasks")
      : projectPath(row.project_id, "tasks");
    return `${path}&generation_job=${row.id}`;
  }
  if (!episodeId) return null;
  if (row.family === "WORKFLOW")
    return `${episodePath(row.project_id, episodeId, "tasks")}&run=${row.id}`;
  if (row.family === "RENDER")
    return `${episodePath(row.project_id, episodeId, "render")}&render_job=${row.id}`;
  return null;
}

export function productionDeliveryPath(row: {
  id: string;
  project_id: string;
  episode_id: string;
}) {
  if (
    !isResourceId(row.id) ||
    !isResourceId(row.project_id) ||
    !isResourceId(row.episode_id)
  )
    return null;
  return `${episodePath(row.project_id, row.episode_id, "render")}&delivery=${row.id}`;
}

export function productionReviewPath(row: {
  id: string;
  project_id: string;
  episode_id: string | null;
  source: string;
  kind: string;
  object_id: string;
  version_id: string | null;
  run_id: string | null;
  asset_id?: string | null;
}) {
  if (![row.id, row.project_id, row.object_id].every(isResourceId)) return null;
  if (row.episode_id && !isResourceId(row.episode_id)) return null;
  const version = row.version_id ?? row.object_id;
  if (!isResourceId(version)) return null;
  if (row.source === "HUMAN_REVIEW") {
    if (["REFERENCE", "VISUAL_SPEC"].includes(row.kind))
      return `${projectPath(row.project_id, "assets")}&proposal=${row.object_id}&proposal_version=${version}`;
    if (
      row.kind === "EXCEPTIONAL_SHOT" &&
      row.asset_id &&
      isResourceId(row.asset_id)
    )
      return `${projectPath(row.project_id, "assets")}&asset=${row.asset_id}&asset_review=${version}&review=${row.id}`;
    if (row.episode_id && row.kind === "DELIVERY")
      return productionDeliveryPath({
        id: row.object_id,
        project_id: row.project_id,
        episode_id: row.episode_id,
      });
    if (row.episode_id && row.kind === "EXPORT")
      return `${episodePath(row.project_id, row.episode_id, "render")}&render_manifest=${row.object_id}&review=${row.id}`;
  }
  if (!row.episode_id) return null;
  if (row.source === "OUTLINE")
    return `${episodePath(row.project_id, row.episode_id, "outline")}&outline=${version}`;
  if (row.source === "SCRIPT" || row.kind === "SCRIPT")
    return `${episodePath(row.project_id, row.episode_id, "list")}&node=script:${version}${row.run_id && isResourceId(row.run_id) ? `&run=${row.run_id}` : ""}`;
  if (row.source === "STORYBOARD" || row.kind === "STORYBOARD")
    return `${episodePath(row.project_id, row.episode_id, "list")}&node=storyboard:${version}${row.run_id && isResourceId(row.run_id) ? `&run=${row.run_id}` : ""}`;
  const run = row.run_id ?? (row.source === "WORKFLOW" ? row.id : null);
  return run && isResourceId(run)
    ? `${episodePath(row.project_id, row.episode_id, "tasks")}&run=${run}`
    : null;
}
