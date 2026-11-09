import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, type Page, type Route } from "@playwright/test";
import type { AssetVersion, Graph } from "../src/types/api";
import type {
  ComparisonReview,
  ContinuityComparison,
  HumanReview,
} from "../src/features/continuity/types";

const now = "2026-11-09T06:00:00Z";
const hash = (data: Buffer | string) =>
  createHash("sha256").update(data).digest("hex");
const id = () => crypto.randomUUID();

// Real authentication/project navigation; controlled read receipts and writes only
// prove browser presentation and request contracts. Backend tests exercise the services.
export async function continuityFixture(page: Page, human = false) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `qa08-${id()}@example.com`,
      password: "continuity-comparison-test",
      display_name: "逐图审核",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "逐图连续性 · 固定绘图验收" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "核对红色箱子的状态" } },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const [originalPng, revisedPng, referencePng] = await Promise.all(
    ["prop.png", "match.png", "prop-reference.png"].map((name) =>
      readFile(resolve("../backend/tests/fixtures/continuity_rules", name)),
    ),
  );
  const assetIds = [id(), id()];
  const versionIds = [id(), id()];
  const candidateIds = [id(), id()];
  const referenceAsset = id(),
    referenceVersion = id(),
    referenceId = id();
  const shotId = id(),
    boardId = id(),
    revisionId = id(),
    runId = id();
  const imagePath = (index: number) =>
    `/projects/${project.id}/assets/${assetIds[index]}/versions/${versionIds[index]}/image`;
  const versions: AssetVersion[] = versionIds.map((versionId, index) => ({
    id: versionId,
    asset_id: assetIds[index],
    version_no: 1,
    checksum: hash(index ? revisedPng : originalPng),
    created_by: session.user.id,
    created_at: now,
    status: "DRAFT",
    stale: false,
    revision: 3,
    archived_from_status: null,
    media: {
      media_type: "image/png",
      width: 240,
      height: 160,
      byte_size: (index ? revisedPng : originalPng).length,
      original_name: "fixed-drawing.png",
      source_note: "项目固定测试绘图",
      rights_note: "测试自有",
    },
    asset_snapshot: null,
    restored_from_version_id: null,
    restoration_note: null,
    provenance: null,
  }));
  const models = versions.map((version, index): ContinuityComparison => {
    const reviewId = id(),
      oldReviewId = id(),
      evidenceId = id(),
      candidateRef = id();
    const referencePath = `/projects/${project.id}/assets/${assetIds[index]}/versions/${version.id}/continuity/reviews/${reviewId}/references/${referenceId}/image`;
    const target = {
      candidate_id: candidateIds[index],
      asset_version_id: version.id,
      shot_id: shotId,
      storyboard_id: boardId,
    };
    const references = [
      {
        reference: {
          reference_id: candidateRef,
          role: "CANDIDATE",
          resource_id: candidateIds[index],
          content_sha256: version.checksum,
          asset_id: null,
          asset_type: null,
        },
        name: "CANDIDATE",
        image_path: imagePath(index),
        status: "DRAFT",
        unavailable_code: null,
      },
      {
        reference: {
          reference_id: referenceId,
          role: "REFERENCE",
          resource_id: referenceVersion,
          content_sha256: hash(referencePng),
          asset_id: referenceAsset,
          asset_type: "PROP",
        },
        name: "红色箱子 · 固定参考",
        image_path: referencePath,
        status: "APPROVED",
        unavailable_code: null,
      },
      ...[
        { role: "TARGET_SHOT", identity: shotId },
        { role: "STORYBOARD", identity: boardId },
      ].map((item) => ({
        reference: {
          reference_id: id(),
          role: item.role,
          resource_id: item.identity,
          content_sha256: hash(item.identity),
          asset_id: null,
          asset_type: null,
        },
        name: item.role,
        image_path: null,
        status: null,
        unavailable_code: null,
      })),
    ];
    const record: ComparisonReview["record"] = {
      sequence: 2,
      recorded_by: session.user.id,
      recorded_at: now,
      review: {
        result_id: reviewId,
        conclusion: human ? "HUMAN_REVIEW" : "REVISION_REQUIRED",
        summary: "箱盖状态与本轮精确分镜不符，请对照原图及道具参考。",
        risk: human ? "HIGH" : "MEDIUM",
        suggested_action: human ? "HUMAN_REVIEW" : "REGENERATE",
        target,
        confidence: {
          level: human ? "LOW" : "HIGH",
          uncertainty: human
            ? [
                {
                  reason: "MISSING_REFERENCE",
                  detail: "箱盖被遮挡，需要 Owner 核实精确图片。",
                },
              ]
            : [],
        },
        issues: [
          {
            issue_id: id(),
            category: "PROP",
            risk: human ? "HIGH" : "MEDIUM",
            summary: "红色箱子应保持关闭。",
            evidence_ids: [evidenceId],
            suggested_action: human ? "HUMAN_REVIEW" : "REGENERATE",
          },
        ],
        evidence: [
          {
            evidence_id: evidenceId,
            reference_id: candidateRef,
            basis: "核对候选图右下角箱盖与固定道具图。",
            region: { x: 0.5, y: 0.5, width: 0.3, height: 0.3 },
          },
        ],
        versions: ["AGENT", "PROMPT", "SCHEMA", "MODEL"].map((kind) => ({
          id: id(),
          kind,
          name: `fixture.review.${kind}`,
          version: "fixture/1",
          digest: hash(kind),
        })),
        rerun: {
          previous_review_id: oldReviewId,
          previous_sha256: hash(oldReviewId),
          reason: "补充道具近景后重审",
        },
      },
    };
    const old = structuredClone(record);
    old.sequence = 1;
    Object.assign(old.review, {
      result_id: oldReviewId,
      conclusion: "PASS",
      summary: "早期审查通过；最新重审有待处理问题。",
      risk: "NONE",
      suggested_action: "NONE",
      issues: [],
      confidence: { level: "HIGH", uncertainty: [] },
      rerun: null,
    });
    const row: HumanReview = {
      id: id(),
      can_approve: true,
      request_sha256: hash(version.id + "human"),
      status: "WAITING_REVIEW",
      blocking_code: null,
      requested_by: session.user.id,
      created_at: now,
      snapshot: {
        version_id: version.id,
        run_id: runId,
        thread_id: runId,
        object_id: reviewId,
        input_sha256: hash(reviewId),
        content_sha256: version.checksum,
      },
      decision: null,
      resume: null,
    };
    return {
      project_id: project.id,
      asset: {
        id: assetIds[index],
        project_id: project.id,
        asset_type: "SHOT_IMAGE",
        name: index ? "修订候选图" : "原始候选图",
        status: "DRAFT",
        current_version_id: null,
        lock_version: 2,
        revision: 7,
        metadata: {
          description: "固定绘图",
          tags: [],
          appearance: {
            age_range: "",
            appearance: "",
            hairstyle: "",
            clothing: "",
            build: "",
            expressions: "",
            colors: "",
            materials: "",
          },
          usage_scope: { kind: "PROJECT", episode_ids: [] },
        },
        creation_source: null,
        created_by: session.user.id,
        character_profile: null,
        scene_profile: null,
        prop_profile: null,
        created_at: now,
        updated_at: now,
      },
      version,
      candidate: {
        id: candidateIds[index],
        job_id: id(),
        source: "GENERATED",
        media_check: {
          status: "PASSED",
          error_code: null,
          references_valid: true,
          image: { width: 240, height: 160, media_type: "image/png" },
        },
      },
      image_path: imagePath(index),
      latest_review_id: reviewId,
      reviews: [
        { record, references },
        { record: old, references },
      ],
      next_offset: null,
      images: [],
      images_truncated: false,
      revisions: [
        {
          record: {
            revision_id: revisionId,
            parent_revision_id: null,
            root_candidate_id: candidateIds[0],
            round: 1,
            max_rounds: 3,
            status: "QUEUED",
            job_id: id(),
            target: {
              candidate_id: candidateIds[0],
              asset_version_id: versionIds[0],
            },
            request: {
              review_id: oldReviewId,
              action: "REGENERATE",
              issue_ids: record.review.issues.map((item) => item.issue_id),
            },
            evidence_ids: [evidenceId],
            created_at: now,
          },
          feedback: [
            {
              feedback_id: id(),
              sequence: 1,
              review_id: reviewId,
              action: human ? "HUMAN_REVIEW" : "REVISE",
              reason: "对照修订新图与冻结参考后，保留本轮依据。",
              status: human ? "HUMAN_REVIEW" : "REVISION_QUEUED",
              source: "MODEL",
              human_review_request_id: human ? row.id : null,
              created_at: now,
            },
          ],
        },
      ],
      human_reviews: human ? [row] : [],
      next_human_offset: null,
      actions: {
        owner: true,
        writable: true,
        blocker: {
          code: human
            ? "HUMAN_DECISION_REQUIRED"
            : "CONTINUITY_REVISION_REQUIRED",
          message: human
            ? "候选须由 Owner 处理精确版本"
            : "候选须修订并对新版本重新审查",
        },
        can_submit: false,
        can_approve: false,
        can_reject: false,
        can_select: false,
      },
    };
  });
  for (const model of models)
    model.images = models.map((source, index) => ({
      candidate_id: candidateIds[index],
      asset_id: assetIds[index],
      asset_name: source.asset.name,
      version: source.version,
      round: index,
      revision_id: index ? revisionId : null,
      image_path: imagePath(index),
    }));
  const graph: Graph = {
    nodes: models.map((model) => ({
      id: "asset_version:" + model.version.id,
      kind: "asset_version",
      title: model.asset.name,
      status: "DRAFT",
      resource_id: model.version.id,
      asset_id: model.asset.id,
      asset_type: "SHOT_IMAGE",
      version_no: 1,
      preview_path: model.image_path,
      checksum: model.version.checksum,
    })),
    edges: [],
    layout: { revision: 0, positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
  };
  await page.route(`**/api/v1/episodes/${episode.id}/canvas`, (route) =>
    route.fulfill({ json: graph }),
  );
  const mutations: { path: string; body: Record<string, unknown> }[] = [];
  const reads: string[] = [];
  const control: {
    readStatus?: number;
    interceptRead?: (
      route: Route,
      model: ContinuityComparison,
    ) => Promise<void>;
    mutationError?: (model: ContinuityComparison) => void;
  } = {};
  const failure = (
    route: Route,
    status: number,
    code: string,
    message: string,
  ) => route.fulfill({ status, json: { error: { code, message } } });
  await page.route(
    `**/api/v1/projects/${project.id}/assets/**`,
    async (route) => {
      const request = route.request(),
        path = new URL(request.url()).pathname;
      const model = models.find((item) =>
        path.includes(`/versions/${item.version.id}/`),
      );
      if (!model) return failure(route, 404, "NOT_FOUND", "精确图片不存在");
      if (path.endsWith("/continuity")) {
        reads.push(request.url());
        if (control.readStatus)
          return failure(
            route,
            control.readStatus,
            control.readStatus === 403 ? "FORBIDDEN" : "NOT_FOUND",
            "当前项目权限已变化，请重新核对。",
          );
        if (control.interceptRead) return control.interceptRead(route, model);
        return route.fulfill({ json: model });
      }
      if (path.endsWith("/image"))
        return route.fulfill({
          contentType: "image/png",
          body: path.includes("/references/")
            ? referencePng
            : model === models[0]
              ? originalPng
              : revisedPng,
        });
      const body = request.postDataJSON();
      mutations.push({ path, body });
      if (control.mutationError) {
        control.mutationError(model);
        return failure(
          route,
          409,
          "VERSION_CONFLICT",
          "版本或审查已变化，请刷新后重试。",
        );
      }
      if (path.endsWith("/state")) {
        model.version.status = body.status;
        model.version.revision++;
      }
      if (path.endsWith("/select")) {
        model.asset.current_version_id = model.version.id;
        model.asset.revision++;
      }
      model.actions.can_submit =
        !model.actions.blocker && model.version.status === "DRAFT";
      model.actions.can_approve =
        !model.actions.blocker && model.version.status === "READY_FOR_REVIEW";
      model.actions.can_reject = model.version.status === "READY_FOR_REVIEW";
      model.actions.can_select =
        !model.actions.blocker && model.version.status === "APPROVED";
      return route.fulfill({
        json: path.endsWith("/select") ? model.asset : model.version,
      });
    },
  );
  await page.route("**/api/v1/human-reviews/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    const model = models.find((item) =>
      item.human_reviews.some((row) => path.includes(row.id)),
    );
    if (!model) return failure(route, 404, "NOT_FOUND", "人工请求不存在");
    const row = model.human_reviews[0],
      body = request.postDataJSON();
    mutations.push({ path, body });
    if (path.endsWith("/decisions")) {
      row.decision = {
        id: id(),
        actor_id: session.user.id,
        created_at: now,
        decision_json: body,
      };
      row.status = "DECIDED";
    } else {
      const approved = row.decision?.decision_json.decision === "APPROVE";
      row.status = "RESUMED";
      row.resume = {
        id: id(),
        created_at: now,
        result_json: { status: approved ? "APPROVED" : "REJECTED" },
      };
      model.actions.blocker = approved
        ? null
        : {
            code: "HUMAN_DECISION_REQUIRED",
            message: "人工拒绝的候选不能正式选用",
          };
      model.actions.can_submit = approved;
    }
    return route.fulfill({
      json: path.endsWith("/decisions") ? row.decision : row.resume,
    });
  });
  await page.route(
    `**/api/v1/projects/${project.id}/dependency-impact?*`,
    (route) =>
      route.fulfill({
        json: {
          digest: hash("impact"),
          summary: { REVIEW: 0, GENERATE: 0, RENDER: 0 },
          affected: [],
        },
      }),
  );
  const href = (index = 1) =>
    `/projects/${project.id}/episodes/${episode.id}?node=asset_version%3A${versionIds[index]}`;
  const open = async (index = 1) => {
    await page.goto(href(index));
    await page
      .getByRole("button", { name: "逐图比较与审核", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "逐图比较与连续性审核",
      exact: true,
    });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByLabel("当前候选状态", { exact: true }),
    ).toBeVisible();
    return dialog;
  };
  return { project, episode, models, mutations, reads, control, href, open };
}
