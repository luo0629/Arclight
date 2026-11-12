import { expect, type Page, type Route } from "@playwright/test";
import type {
  ProductionDelivery,
  ProductionReview,
  ProductionTask,
} from "../src/features/projects/production-types";
import { mediaFixture } from "./media-tasks-helpers";
import { closeDirectory } from "./workspace-helpers";

export const productionSecret = "UI07_PRIVATE_PROMPT_CREDENTIAL_STACK";
export const productionNow = "2026-11-11T01:00:00Z";
export type ProductionResource = "tasks" | "pending-reviews" | "deliveries";
type Read = (route: Route, url: URL) => Promise<boolean>;

// Authentication and the primary project/episodes use the real API. Production
// summaries are deterministic browser contracts; they do not prove DB ordering,
// paid generation, rendered bytes or backend access checks.
export async function productionFixture(page: Page) {
  const media = await mediaFixture(page);
  const otherProject = {
    ...media.project,
    id: crypto.randomUUID(),
    name: "另一项目的生产记录",
  };
  const createTask = (
    overrides: Partial<ProductionTask> = {},
  ): ProductionTask => ({
    id: crypto.randomUUID(),
    family: "GENERATION",
    project_id: media.project.id,
    episode_id: media.episodes[0].id,
    episode_no: 1,
    episode_title: media.episodes[0].title,
    task_type: "IMAGE",
    workflow_type: null,
    status: "FAILED",
    error_code: "PROVIDER_UNAVAILABLE",
    created_at: productionNow,
    updated_at: productionNow,
    detail_url: "/ignored-untrusted-destination",
    ...overrides,
  });
  const createReview = (
    overrides: Partial<ProductionReview> = {},
  ): ProductionReview => ({
    id: crypto.randomUUID(),
    source: "SCRIPT",
    project_id: media.project.id,
    episode_id: media.episodes[0].id,
    episode_no: 1,
    episode_title: media.episodes[0].title,
    kind: "SCRIPT",
    object_id: crypto.randomUUID(),
    version_id: crypto.randomUUID(),
    run_id: null,
    asset_id: null,
    status: "WAITING_REVIEW",
    blocking_code: null,
    created_at: productionNow,
    detail_url: "/ignored-untrusted-destination",
    ...overrides,
  });
  const createDelivery = (
    overrides: Partial<ProductionDelivery> = {},
  ): ProductionDelivery => ({
    id: crypto.randomUUID(),
    project_id: media.project.id,
    episode_id: media.episodes[0].id,
    episode_no: 1,
    episode_title: media.episodes[0].title,
    job_id: crypto.randomUUID(),
    asset_id: crypto.randomUUID(),
    asset_version_id: crypto.randomUUID(),
    version_no: 3,
    file_id: crypto.randomUUID(),
    sha256: "a".repeat(64),
    byte_size: 1024,
    duration_ms: 30000,
    profile: "PREVIEW",
    manifest_id: crypto.randomUUID(),
    manifest_sha256: "b".repeat(64),
    timeline_id: crypto.randomUUID(),
    dependency_manifest_id: crypto.randomUUID(),
    inspection_id: crypto.randomUUID(),
    report_id: null,
    report_file_id: null,
    storage_backend: "LOCAL",
    stale: false,
    is_delivery: false,
    acceptance: {
      phase: "PREVIEW",
      is_delivery: false,
      warnings_confirmed: false,
      review_id: null,
      reviewed_by_user_id: null,
      blocking_issues: [],
    },
    created_at: productionNow,
    detail_url: "/ignored-untrusted-destination",
    ...overrides,
  });
  const tasks = [createTask()];
  const reviews: ProductionReview[] = [];
  const deliveries: ProductionDelivery[] = [];
  const otherTasks = [
    createTask({ project_id: otherProject.id, episode_id: null }),
  ];
  const reads: URL[] = [];
  const control: {
    status?: number;
    read?: Read;
    resourceStatus: Partial<Record<ProductionResource, number>>;
  } = { resourceStatus: {} };

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (route.request().method() !== "GET") return route.fallback();
    if (path === "/api/v1/projects")
      return route.fulfill({ json: [media.project, otherProject] });
    if (path === `/api/v1/projects/${otherProject.id}`)
      return route.fulfill({ json: otherProject });
    if (path === `/api/v1/projects/${otherProject.id}/access`)
      return route.fulfill({ json: { role: "owner" } });
    if (path === `/api/v1/projects/${otherProject.id}/episodes`)
      return route.fulfill({ json: [] });
    const match = path.match(
      /^\/api\/v1\/projects\/([^/]+)\/(tasks|pending-reviews|deliveries)$/,
    );
    if (!match) return route.fallback();
    reads.push(url);
    if (control.read && (await control.read(route, url))) return;
    const resource = match[2] as ProductionResource;
    const status = control.resourceStatus[resource] ?? control.status;
    if (status)
      return route.fulfill({
        status,
        json: {
          error: {
            code: status === 403 ? "FORBIDDEN" : "INTERNAL_ERROR",
            message: productionSecret,
            details: { stack: productionSecret },
            request_id: "ui07-browser-contract",
          },
        },
      });
    let items: (ProductionTask | ProductionReview | ProductionDelivery)[] =
      resource === "tasks"
        ? match[1] === otherProject.id
          ? otherTasks
          : tasks
        : resource === "pending-reviews"
          ? reviews
          : deliveries;
    if (resource === "tasks") {
      items = (items as ProductionTask[]).filter((row) => {
        for (const key of [
          "episode_id",
          "task_type",
          "status",
          "error_code",
        ] as const) {
          const value = url.searchParams.get(key);
          if (value && row[key] !== value) return false;
        }
        const from = url.searchParams.get("created_from");
        const to = url.searchParams.get("created_to");
        return (
          (!from || new Date(row.created_at) >= new Date(from)) &&
          (!to || new Date(row.created_at) < new Date(to))
        );
      });
    }
    const limit = Number(url.searchParams.get("limit"));
    const offset = Number(url.searchParams.get("offset"));
    return route.fulfill({
      json: {
        items: items.slice(offset, offset + limit),
        total: items.length,
        limit,
        offset,
      },
    });
  });
  const href = (section: "tasks" | "reviews" | "deliveries" = "tasks") =>
    `/projects/${media.project.id}?section=${section}`;
  const open = async (
    section: "tasks" | "reviews" | "deliveries" = "tasks",
  ) => {
    await page.goto(href(section));
    await closeDirectory(page);
    const label =
      section === "tasks"
        ? "项目生产任务"
        : section === "reviews"
          ? "项目待审列表"
          : "项目成片列表";
    const panel = page.getByLabel(label, { exact: true });
    await expect(panel).toBeVisible();
    return panel;
  };
  return {
    ...media,
    otherProject,
    otherTasks,
    tasks,
    reviews,
    deliveries,
    productionControl: control,
    productionReads: reads,
    createTask,
    createReview,
    createDelivery,
    productionHref: href,
    openProduction: open,
  };
}

export async function chooseProductionFilter(
  page: Page,
  label: string,
  option: string,
) {
  await page.getByRole("combobox", { name: label, exact: true }).click();
  await page.getByRole("option", { name: option, exact: true }).click();
}
