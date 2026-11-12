import { expect, test } from "@playwright/test";
import type { Run } from "../src/types/api";
import { mediaFixture } from "./media-tasks-helpers";

// Backend TEST-16 executes the actual story-to-MP4 chain. These controlled
// public receipts verify that its six creative/reviewer runs and six image
// jobs remain understandable in the browser, including the human gates.
for (const mobile of [false, true]) {
  test(`TEST-16：${mobile ? "手机" : "桌面"}任务记录区分创作、逐图审查和局部重生成`, async ({
    page,
  }, testInfo) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const env = await mediaFixture(page);
    env.jobs.splice(
      0,
      env.jobs.length,
      ...Array.from({ length: 6 }, () =>
        env.createJob("SUCCEEDED", env.episodes[0].id),
      ),
    );
    const runs: Run[] = Array.from({ length: 6 }, (_, index) => ({
      id: crypto.randomUUID(),
      episode_id: env.episodes[0].id,
      workflow_type: index === 0 ? "script_storyboard" : "continuity_review",
      status: "SUCCEEDED",
      revision: index === 5 ? 3 : 1,
      pending_review: null,
      error_code: null,
      created_at: "2026-11-12T07:00:00Z",
    }));
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === `/api/v1/episodes/${env.episodes[0].id}/runs`)
        return route.fulfill({ json: runs });
      const run = runs.find((row) => path === `/api/v1/runs/${row.id}/events`);
      if (run)
        return route.fulfill({
          json: [
            {
              id: crypto.randomUUID(),
              event_type: "workflow.finished",
              created_at: run.created_at,
              payload_json: { reason: "OWNER_SELECTED_REVIEWED_IMAGE" },
            },
          ],
        });
      return route.fallback();
    });
    await env.open();
    await expect(
      page.getByRole("button", { name: /^查看媒体任务 / }),
    ).toHaveCount(6);
    const cards = page.locator("article.task");
    await expect(cards).toHaveCount(6);
    await expect(cards.filter({ hasText: "画面连续性审查" })).toHaveCount(5);
    await expect(cards.filter({ hasText: "剧本与分镜创作" })).toHaveCount(1);
    await expect(
      page.getByRole("button", { name: "批准并继续", exact: true }),
    ).toHaveCount(0);
    const last = page.locator(`#run-${runs[5].id}`);
    await last
      .getByRole("button", { name: "查看执行记录", exact: true })
      .click();
    const history = page.getByRole("region", { name: "执行记录", exact: true });
    await expect(history).toContainText(runs[5].id);
    await expect(history).toContainText("workflow.finished");
    await last.scrollIntoViewIfNeeded();
    await expect(last).toBeInViewport();
    await page.screenshot({
      path: testInfo.outputPath(
        `continuity-history-${mobile ? "mobile" : "desktop"}.png`,
      ),
    });
  });
}

for (const [destination, message] of [
  ["HUMAN_REVIEW", "审查已暂停，请在素材审查页处理人工决定。"],
  ["REVISION_REQUIRED", "画面需要局部修订，请在素材审查页处理。"],
  ["READY_FOR_REVIEW", "审查已完成，等待人工批准并选用素材。"],
]) {
  test(`TEST-16：${destination}审查不显示文本批准入口`, async ({ page }) => {
    const env = await mediaFixture(page);
    const run: Run = {
      id: crypto.randomUUID(),
      episode_id: env.episodes[0].id,
      workflow_type: "continuity_review",
      status: "WAITING_REVIEW",
      revision: 1,
      created_at: "2026-11-12T07:00:00Z",
      error_code: null,
      pending_review: { resource_type: "continuity_review", destination },
    };
    await page.route(
      `**/api/v1/episodes/${env.episodes[0].id}/runs*`,
      (route) => route.fulfill({ json: [run] }),
    );
    await env.open();
    const card = page.locator(`#run-${run.id}`);
    await expect(card).toContainText("画面连续性审查");
    await expect(card).toContainText(message);
    await expect(
      card.getByRole("button", { name: "批准并继续", exact: true }),
    ).toHaveCount(0);
    await expect(
      card.getByRole("button", { name: "驳回该版本", exact: true }),
    ).toHaveCount(0);
    await expect(card.getByText("等待审核：分镜")).toHaveCount(0);
    expect(env.mutations).toHaveLength(0);
  });
}
