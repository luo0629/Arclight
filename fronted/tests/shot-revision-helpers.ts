import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, type Page, type Route } from "@playwright/test";
import type {
  RevisionCommand,
  RevisionIntent,
  RevisionPreview,
  RevisionReceipt,
} from "../src/features/editor/shot-revision-types";
import { apiFailure, shotProductionFixture } from "./shot-production-helpers";

// Authentication, project, script and storyboard are real. Controlled candidate and
// revision receipts exercise the browser contract. Backend tests cover actual generation,
// immutable versions, budget, database races and independent QA; this is no quality claim.
export async function shotRevisionFixture(page: Page) {
  const env = await shotProductionFixture(page);
  const model = await env.state();
  const shot = model.shots[0];
  const assetId = crypto.randomUUID();
  const versionId = crypto.randomUUID();
  const candidate = {
    id: crypto.randomUUID(),
    asset_id: assetId,
    version_id: versionId,
    version_no: 1,
    revision: 0,
    status: "DRAFT",
    can_submit: false,
    can_approve: false,
    can_reject: false,
    blocker: {
      code: "CONTINUITY_REVIEW_REQUIRED",
      message: "新候选须先完成连续性审查",
    },
    image_path: `/projects/${env.project.id}/assets/${assetId}/versions/${versionId}/image`,
  };
  shot.candidates = [candidate];
  const png = await readFile(
    resolve("../backend/tests/fixtures/continuity_rules/prop.png"),
  );
  const source = {
    candidate_id: candidate.id,
    asset_id: candidate.asset_id,
    version_id: candidate.version_id,
    version_no: candidate.version_no,
    storyboard_id: env.board.id,
    shot_id: shot.id,
    episode_id: env.episode.id,
    job_id: crypto.randomUUID(),
    input_manifest_id: crypto.randomUUID(),
    provider: "mock",
    model: "mock-image-v1",
    snapshot_digest: "a".repeat(64),
  };
  const endpoint = `/api/v1/projects/${env.project.id}/continuity-revisions/shots`;
  const requests: { key: string; command: RevisionCommand }[] = [];
  const previews: RevisionIntent[] = [];
  const receipts = new Map<
    string,
    { command: RevisionCommand; receipt: RevisionReceipt }
  >();
  let previewFailure: {
    code: string;
    message: string;
    status?: number;
  } | null = null;
  let loseNext = false,
    lossInjected = false,
    denyRead = false;
  let prepareOverride: ((preview: RevisionPreview) => RevisionPreview) | null =
    null;
  let submissionGate: (() => Promise<void>) | null = null;
  await page.route("**" + env.productionPath + "?**", (route) =>
    denyRead
      ? apiFailure(route, "FORBIDDEN", "当前项目角色没有此操作权限", 403)
      : route.fulfill({ json: model }),
  );
  await page.route("**/api/v1" + candidate.image_path, (route) =>
    route.fulfill({ contentType: "image/png", body: png }),
  );
  await page.route("**" + endpoint + "/preview", async (route) => {
    const intent = route.request().postDataJSON() as RevisionIntent;
    previews.push(intent);
    if (previewFailure)
      return apiFailure(
        route,
        previewFailure.code,
        previewFailure.message,
        previewFailure.status,
      );
    const preview: RevisionPreview = {
      intent,
      source,
      confirmation_digest: createHash("sha256")
        .update(JSON.stringify(intent))
        .digest("hex"),
      mode_description:
        "按修订要求整图重生成；原图仅供比较，不保证其他像素保持不变。",
      quote: {
        maximum_cost: "0.050000",
        currency: "USD",
        pricing_version: "ui04-browser-contract/1",
      },
      preserved_constraints: [
        "原角色身份、已批准参考和负面约束保留；新图必须独立审查。",
      ],
    };
    await route.fulfill({
      json: prepareOverride ? prepareOverride(preview) : preview,
    });
  });
  await page.route("**" + endpoint, async (route: Route) => {
    const command = route.request().postDataJSON() as RevisionCommand;
    const key = route.request().headers()["idempotency-key"];
    requests.push({ key, command });
    if (submissionGate) await submissionGate();
    let saved = receipts.get(key);
    const replay = Boolean(saved);
    if (saved && JSON.stringify(saved.command) !== JSON.stringify(command))
      return apiFailure(
        route,
        "IDEMPOTENCY_CONFLICT",
        "原幂等键已绑定另一请求",
      );
    if (!saved) {
      const receipt: RevisionReceipt = {
        job_id: crypto.randomUUID(),
        input_manifest_id: crypto.randomUUID(),
        source,
        status: "CREATED",
        mode: "PROMPT_REGENERATION",
      };
      saved = { command, receipt };
      receipts.set(key, saved);
    }
    if (loseNext) {
      loseNext = false;
      await route.abort("connectionreset");
      lossInjected = true;
    } else
      await route.fulfill({ status: replay ? 200 : 201, json: saved.receipt });
  });
  async function open(navigate = true) {
    const production = await env.open(navigate);
    await production
      .getByRole("combobox", { name: "镜头 1 审核候选", exact: true })
      .selectOption(candidate.id);
    await expect(
      production.getByRole("img", {
        name: `镜头 1 候选 1 · v${candidate.version_no}`,
        exact: true,
      }),
    ).toBeVisible();
    await production
      .getByRole("button", { name: "局部修订镜头 1", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "单镜头局部修订",
      exact: true,
    });
    await expect(
      dialog.getByRole("img", { name: "修订来源镜头 1 原图", exact: true }),
    ).toBeVisible();
    return dialog;
  }
  return {
    ...env,
    openProduction: env.open,
    model,
    candidate,
    source,
    endpoint,
    requests,
    previews,
    receipts,
    open,
    setPreviewFailure(value: typeof previewFailure) {
      previewFailure = value;
    },
    setPrepareOverride(value: typeof prepareOverride) {
      prepareOverride = value;
    },
    setSubmissionGate(value: typeof submissionGate) {
      submissionGate = value;
    },
    loseResponse() {
      loseNext = true;
    },
    lossInjected: () => lossInjected,
    denyRead() {
      denyRead = true;
    },
  };
}
