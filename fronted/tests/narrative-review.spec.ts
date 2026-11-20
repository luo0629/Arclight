import { expect, test } from "@playwright/test";
import { productionFixture } from "./project-production-helpers";
import { closeDirectory } from "./workspace-helpers";

test("SERIES-06：精确叙事队列保留证据限制，内容决定与结束审查分开提交", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const runId = crypto.randomUUID(),
    reportId = crypto.randomUUID(),
    humanId = crypto.randomUUID();
  const scene = {
    episode_id: env.episodes[0].id,
    episode_no: 1,
    script_version_id: crypto.randomUUID(),
    scene_id: crypto.randomUUID(),
    line_id: null,
    shot_id: null,
    candidate_id: null,
    time_ms: null,
  };
  const detail = {
    status: "WAITING_REVIEW",
    blocking_code: null,
    error_code: null,
    work: {
      project_id: env.project.id,
      run_id: runId,
      request: { scope: "SCENE" },
      evidence: [
        {
          id: crypto.randomUUID(),
          kind: "SCENE",
          location: scene,
          value: { scene: { action: "<script>不能作为指令执行</script>" } },
        },
      ],
    },
    report: {
      report_id: reportId,
      input_sha256: "a".repeat(64),
      conclusion: "HUMAN_REVIEW",
      limitations: ["未取得完整声音观察"],
      previous_report_id: null,
      assessment: {
        summary: "仅检查精确提供的叙事证据",
        checks: [
          {
            category: "KNOWLEDGE",
            status: "CLEAR",
            summary: "角色知情未超出故事时点",
            evidence_ids: [],
            issues: [],
          },
        ],
      },
    },
    human_review: {
      id: humanId,
      request_sha256: "b".repeat(64),
      status: "WAITING_REVIEW",
      blocking_code: null,
      snapshot: { version_id: reportId },
      decision: null as null | {
        id: string;
        decision_json: { decision: string; comment: string };
      },
      resume: null as null | { id: string; result_json: { status: string } },
    },
  };
  env.reviews.push(
    env.createReview({
      id: humanId,
      source: "HUMAN_REVIEW",
      kind: "NARRATIVE",
      object_id: reportId,
      version_id: reportId,
      run_id: runId,
    }),
  );
  const mutations: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/projects/*/narrative-reviews**", (route) => {
    const url = new URL(route.request().url());
    return route.fulfill({
      json: url.pathname.endsWith(runId)
        ? detail
        : [{ run_id: runId, scope: "SCENE", status: detail.status }],
    });
  });
  await page.route(`**/api/v1/human-reviews/${humanId}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as Record<string, unknown>;
    mutations.push({ path, body });
    if (path.endsWith("/decisions")) {
      detail.human_review.status = "DECIDED";
      detail.human_review.decision = {
        id: crypto.randomUUID(),
        decision_json: {
          decision: String(body.decision),
          comment: String(body.comment),
        },
      };
      return route.fulfill({ json: detail.human_review.decision });
    }
    detail.status = "SUCCEEDED";
    detail.human_review.status = "RESUMED";
    detail.human_review.resume = {
      id: crypto.randomUUID(),
      result_json: { status: "APPROVED" },
    };
    return route.fulfill({ json: detail.human_review.resume });
  });
  const queue = await env.openProduction("reviews");
  await queue.getByRole("link", { name: "查看精确审核" }).click();
  await closeDirectory(page);
  const report = page.getByLabel("精确叙事报告", { exact: true });
  await expect(report).toContainText("角色知情未超出故事时点");
  const approve = report.getByRole("button", {
    name: "保存本次内容批准",
    exact: true,
  });
  await expect(approve).toBeDisabled();
  await report
    .getByLabel("内容处理意见（必填）")
    .fill("连续观看并核对全部精确来源。");
  await report.getByRole("checkbox").check();
  await expect(approve).toBeDisabled();
  await report
    .getByLabel("未取得完整声音观察", { exact: true })
    .fill("已人工试听完整配音并核实知情安排。");
  await approve.click();
  await expect(
    report.getByRole("button", { name: "确认决定并结束本次审查" }),
  ).toBeVisible();
  expect(mutations).toHaveLength(1);
  expect(mutations[0].body).toMatchObject({
    version_id: reportId,
    request_sha256: "b".repeat(64),
    narrative_content: {
      report_id: reportId,
      input_sha256: "a".repeat(64),
      continuous_viewing_confirmed: true,
      episode_ids: [scene.episode_id],
      limitation_resolutions: ["已人工试听完整配音并核实知情安排。"],
    },
  });
  await report.getByRole("button", { name: "确认决定并结束本次审查" }).click();
  await expect(report).toContainText("未改变素材选用或整剧交付状态");
  expect(mutations).toHaveLength(2);
  expect(mutations[1].body).toMatchObject({
    decision_id: detail.human_review.decision!.id,
    request_sha256: "b".repeat(64),
  });
});

test("SERIES-06：只读角色不能发起或批准叙事审查", async ({ page }) => {
  const env = await productionFixture(page);
  env.control.role = "viewer";
  await page.route("**/api/v1/projects/*/narrative-reviews**", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto(env.productionHref("reviews"));
  await closeDirectory(page);
  const section = page.getByLabel("叙事连续性审查", { exact: true });
  await section.getByText("发起精确叙事审查", { exact: true }).click();
  await expect(
    section.getByRole("button", { name: "开始叙事审查", exact: true }),
  ).toBeDisabled();
  await expect(section.getByLabel("精确送审请求（JSON）")).toBeDisabled();
});

test("SERIES-06：补证问题独立显示，回答不冒充内容批准", async ({ page }) => {
  const env = await productionFixture(page);
  const runId = crypto.randomUUID();
  const question = {
    loop_id: crypto.randomUUID(),
    question_id: crypto.randomUUID(),
    input_sha256: "c".repeat(64),
    request: {
      question: "请补充信件去向的事实来源",
      options: ["补充", "停止"],
    },
  };
  const detail = {
    status: "RUNNING",
    blocking_code: null,
    error_code: null,
    work: {
      project_id: env.project.id,
      run_id: runId,
      request: {},
      evidence: [],
    },
    report: null,
    human_review: null,
    question: question as typeof question | null,
  };
  const sent: Record<string, unknown>[] = [];
  await page.route("**/api/v1/projects/*/narrative-reviews**", (route) =>
    route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith(runId)
        ? detail
        : [{ run_id: runId, scope: "SCENE", status: detail.status }],
    }),
  );
  await page.route(
    `**/api/v1/runs/${runId}/continuity/${question.loop_id}/answers`,
    (route) => {
      sent.push(route.request().postDataJSON());
      detail.question = null;
      detail.status = "QUEUED";
      return route.fulfill({
        status: 202,
        json: { run_id: runId, status: "QUEUED" },
      });
    },
  );
  await page.goto(env.productionHref("reviews"));
  await closeDirectory(page);
  const section = page.getByLabel("叙事连续性审查", { exact: true });
  await section.getByRole("button", { name: /场景.*RUNNING/ }).click();
  const form = section.getByLabel("叙事审查补证问题", { exact: true });
  await expect(form).toContainText(question.request.question);
  await expect(
    form.getByRole("button", { name: "提交补证回答并继续审查" }),
  ).toBeDisabled();
  await form.getByRole("radio", { name: "补充", exact: true }).click();
  await form.getByRole("button", { name: "提交补证回答并继续审查" }).click();
  await expect(form).toHaveCount(0);
  expect(sent).toEqual([
    {
      question_id: question.question_id,
      input_sha256: question.input_sha256,
      decision: "ANSWER",
      answer: "补充",
    },
  ]);
  await expect(
    section.getByRole("button", { name: "保存本次内容批准" }),
  ).toHaveCount(0);
});
