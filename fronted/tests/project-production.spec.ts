import { expect, test } from "@playwright/test";
import type { ContentVersion, Graph, Run } from "../src/types/api";
import { deferred } from "./media-tasks-helpers";
import { continuityFixture } from "./continuity-helpers";
import { renderFixture } from "./render-helpers";
import {
  chooseProductionFilter,
  productionFixture,
  productionSecret,
} from "./project-production-helpers";
import { chooseProject, closeDirectory } from "./workspace-helpers";

test.use({ timezoneId: "Asia/Shanghai" });

test("UI-07 UI7-04/09：目录页外渲染任务读取精确清单和时间轴，轮询后按最新修订取消", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const manifest = env.manifests[0];
  const job = env.job(manifest);
  env.jobs.push(job);
  await page.route(
    `**/api/v1/render-manifests/${manifest.id}/jobs?*`,
    (route) => route.fulfill({ json: [] }),
  );
  await page.goto(`${env.href()}&render_job=${job.id}`);
  await closeDirectory(page);
  const selected = page.getByRole("region", {
    name: "当前渲染任务",
    exact: true,
  });
  await expect(selected).toContainText(`任务 ${job.id} · 修订 1`);
  for (const path of [
    `/render-jobs/${job.id}`,
    `/render-manifests/${manifest.id}`,
    `/timelines/${manifest.snapshot.timeline_id}`,
  ])
    expect(env.reads.some((url) => new URL(url).pathname.endsWith(path))).toBe(
      true,
    );
  job.status = "RUNNING";
  job.revision = 4;
  job.progress = 37;
  await expect(selected).toContainText("修订 4", { timeout: 10000 });
  await expect(selected.getByRole("progressbar")).toHaveAttribute(
    "value",
    "37",
  );
  await selected.getByRole("button", { name: "取消渲染", exact: true }).click();
  await expect(selected).toContainText("修订 5");
  const cancellations = env.mutations.filter((row) =>
    row.path.endsWith(`/render-jobs/${job.id}/cancel`),
  );
  expect(cancellations).toHaveLength(1);
  expect(cancellations[0].body).toMatchObject({ expected_revision: 4 });
  expect(job.status).toBe("CANCELLED");
});

test("UI-07 UI7-04/06：三类生产任务使用服务端20项分页及精确原任务入口", async ({
  page,
}, info) => {
  const env = await productionFixture(page);
  env.tasks.splice(
    0,
    1,
    ...Array.from({ length: 41 }, (_, index) =>
      env.createTask(
        index % 3 === 0
          ? { family: "WORKFLOW", task_type: "TEXT" }
          : index % 3 === 1
            ? { family: "GENERATION", task_type: "IMAGE" }
            : { family: "RENDER", task_type: "RENDER" },
      ),
    ),
  );
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(20);
  await expect(panel).toContainText("共 41 项；第 1 页");
  for (const [index, query] of [
    [0, `view=tasks&run=${env.tasks[0].id}`],
    [1, `view=tasks&generation_job=${env.tasks[1].id}`],
    [2, `view=render&render_job=${env.tasks[2].id}`],
  ] as const)
    await expect(
      panel.getByRole("link", {
        name: `查看任务详情 ${env.tasks[index].id}`,
        exact: true,
      }),
    ).toHaveAttribute(
      "href",
      `/projects/${env.project.id}/episodes/${env.episodes[0].id}?${query}`,
    );
  await panel.screenshot({
    path: info.outputPath("ui07-production-desktop.png"),
  });
  await panel.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(panel).toContainText(`任务 ${env.tasks[20].id}`);
  await expect(panel).not.toContainText(`任务 ${env.tasks[0].id}`);
  await expect(panel).toContainText("共 41 项；第 2 页");
  await panel.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(panel.getByRole("article")).toHaveCount(1);
  await expect(panel).toContainText(`任务 ${env.tasks[40].id}`);
  await expect(
    panel.getByRole("button", { name: "下一页", exact: true }),
  ).toBeDisabled();
  await panel.getByRole("button", { name: "上一页", exact: true }).click();
  await expect(panel).toContainText("共 41 项；第 2 页");
  expect(
    env.productionReads.map((url) => [
      url.searchParams.get("limit"),
      url.searchParams.get("offset"),
    ]),
  ).toEqual(
    expect.arrayContaining([
      ["20", "0"],
      ["20", "20"],
      ["20", "40"],
    ]),
  );
});

test("UI-07 UI7-05：组合剧集类型状态错误及本地时间转UTC，重置恢复全项目", async ({
  page,
}) => {
  const env = await productionFixture(page);
  env.tasks.push(
    env.createTask({ task_type: "TEXT", family: "WORKFLOW" }),
    env.createTask({ episode_id: null, episode_no: null, episode_title: null }),
  );
  const panel = await env.openProduction();
  await chooseProductionFilter(
    page,
    "剧集",
    `第 1 集 ${env.episodes[0].title}`,
  );
  await chooseProductionFilter(page, "任务类型", "图片生成");
  await chooseProductionFilter(page, "任务状态", "失败");
  await chooseProductionFilter(page, "错误类别", "供应商不可用");
  await panel.getByLabel("开始时间", { exact: true }).fill("2026-11-11T08:30");
  await panel.getByLabel("结束时间", { exact: true }).fill("2026-11-11T10:30");
  await panel.getByRole("button", { name: "应用筛选", exact: true }).click();
  await expect(panel.getByRole("article")).toHaveCount(1);
  const url = env.productionReads.at(-1)!;
  expect(Object.fromEntries(url.searchParams)).toEqual({
    episode_id: env.episodes[0].id,
    task_type: "IMAGE",
    status: "FAILED",
    created_from: "2026-11-11T00:30:00.000Z",
    created_to: "2026-11-11T02:30:00.000Z",
    error_code: "PROVIDER_UNAVAILABLE",
    limit: "20",
    offset: "0",
  });
  await panel.getByRole("button", { name: "重置筛选", exact: true }).click();
  await expect(panel.getByRole("article")).toHaveCount(3);
  await expect(panel.getByLabel("开始时间", { exact: true })).toHaveValue("");
  expect(Object.fromEntries(env.productionReads.at(-1)!.searchParams)).toEqual({
    limit: "20",
    offset: "0",
  });
});

test("UI-07 UI7-05：相同及倒置时间范围在浏览器阻断且不提交请求", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(1);
  const count = env.productionReads.length;
  await panel.getByLabel("开始时间", { exact: true }).fill("2026-11-11T10:00");
  for (const end of ["2026-11-11T10:00", "2026-11-11T09:00"]) {
    await panel.getByLabel("结束时间", { exact: true }).fill(end);
    await panel.getByRole("button", { name: "应用筛选", exact: true }).click();
    await expect(panel).toContainText("结束时间必须晚于开始时间");
    expect(env.productionReads).toHaveLength(count);
  }
  await panel.getByRole("button", { name: "重置筛选", exact: true }).click();
  await expect(panel).not.toContainText("结束时间必须晚于开始时间");
});

for (const status of [503, 403]) {
  test(`UI-07 UI7-07/08：${status}清空旧任务与计数，错误仅显示安全摘要并可刷新`, async ({
    page,
  }) => {
    const env = await productionFixture(page);
    const panel = await env.openProduction();
    await expect(panel.getByRole("article")).toHaveCount(1);
    env.productionControl.status = status;
    await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
    await expect(panel).toContainText(
      status === 403 ? "当前项目不可访问" : "生产列表暂时无法读取",
    );
    await expect(panel.getByRole("article")).toHaveCount(0);
    await expect(panel).toContainText("共 0 项；第 1 页");
    await expect(page.locator("body")).not.toContainText(productionSecret);
    env.productionControl.status = undefined;
    await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
    await expect(panel.getByRole("article")).toHaveCount(1);
  });
}

test("UI-07 UI7-08：筛选变更隔离迟到旧页，旧任务和旧总数不会回填", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const image = env.tasks[0];
  const text = env.createTask({ family: "WORKFLOW", task_type: "TEXT" });
  env.tasks.push(text);
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(2);
  const started = deferred();
  const gate = deferred();
  const finished = deferred();
  env.productionControl.read = async (route, url) => {
    if (url.searchParams.has("task_type")) return false;
    started.release();
    await gate.promise;
    await route
      .fulfill({ json: { items: [image], total: 99, limit: 20, offset: 0 } })
      .catch(() => {});
    finished.release();
    return true;
  };
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  await started.promise;
  try {
    await chooseProductionFilter(page, "任务类型", "文字创作");
    await panel.getByRole("button", { name: "应用筛选", exact: true }).click();
    await expect(panel).toContainText(`任务 ${text.id}`);
    await expect(panel).toContainText("共 1 项；第 1 页");
  } finally {
    gate.release();
  }
  await finished.promise;
  await expect(panel).not.toContainText(`任务 ${image.id}`);
  await expect(panel).toContainText("共 1 项；第 1 页");
});

test("UI-07 UI7-08：切换项目隔离旧项目迟到响应", async ({ page }) => {
  const env = await productionFixture(page);
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(1);
  const started = deferred();
  const gate = deferred();
  const finished = deferred();
  env.productionControl.read = async (route, url) => {
    if (!url.pathname.includes(env.project.id)) return false;
    started.release();
    await gate.promise;
    await route
      .fulfill({ json: { items: env.tasks, total: 88, limit: 20, offset: 0 } })
      .catch(() => {});
    finished.release();
    return true;
  };
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  await started.promise;
  try {
    await chooseProject(page, env.otherProject);
    await page.getByRole("button", { name: "项目任务", exact: true }).click();
    await expect(panel).toContainText(env.otherProject.name);
    await expect(panel).toContainText(`任务 ${env.otherTasks[0].id}`);
  } finally {
    gate.release();
  }
  await finished.promise;
  await expect(panel).not.toContainText(`任务 ${env.tasks[0].id}`);
  await expect(panel).toContainText("共 1 项；第 1 页");
});

test("UI-07 UI7-07：白名单摘要隐藏内部快照并从资源身份重建本地链接", async ({
  page,
}) => {
  const env = await productionFixture(page);
  Object.assign(env.tasks[0], {
    error_code: productionSecret,
    workflow_type: productionSecret,
    system_prompt: productionSecret,
    credentials: productionSecret,
    stack: productionSecret,
    detail_url: "https://untrusted.example/private",
  });
  const panel = await env.openProduction();
  await expect(panel).toContainText("错误摘要：内部故障");
  await expect(page.locator("body")).not.toContainText(productionSecret);
  await expect(
    panel.getByRole("link", { name: /^查看任务详情 / }),
  ).toHaveAttribute(
    "href",
    `/projects/${env.project.id}/episodes/${env.episodes[0].id}?view=tasks&generation_job=${env.tasks[0].id}`,
  );
});

test("UI-07 UI7-08：390px键盘筛选及分页可用并保存截图", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await productionFixture(page);
  env.tasks.push(...Array.from({ length: 20 }, () => env.createTask()));
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(20);
  const type = panel.getByRole("combobox", { name: "任务类型", exact: true });
  await type.focus();
  await expect(type).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(
    page.getByRole("option", { name: "全部", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Home");
  await expect(
    page.getByRole("option", { name: "全部", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(
    page.getByRole("option", { name: "文字创作", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(
    page.getByRole("option", { name: "图片生成", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(type).toContainText("图片生成");
  const apply = panel.getByRole("button", { name: "应用筛选", exact: true });
  await apply.focus();
  await expect(apply).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel).toContainText("共 21 项；第 1 页");
  const next = panel.getByRole("button", { name: "下一页", exact: true });
  await next.focus();
  await expect(next).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel).toContainText("共 21 项；第 2 页");
  await expect(panel.getByRole("article")).toHaveCount(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await panel.screenshot({
    path: info.outputPath("ui07-production-mobile.png"),
  });
});

test("UI-07 UI7-04/08：未分集generation_job入口精确读取UI-05详情并服从只读权限", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const job = env.terminal("FAILED");
  job.episode_id = null;
  env.control.role = "viewer";
  env.tasks[0] = env.createTask({
    id: job.id,
    episode_id: null,
    episode_no: null,
    episode_title: null,
  });
  const panel = await env.openProduction();
  await panel.getByRole("link", { name: `查看任务详情 ${job.id}` }).click();
  await expect(page).toHaveURL(
    new RegExp(`section=tasks&generation_job=${job.id}$`),
  );
  const detail = page.getByRole("region", {
    name: `媒体任务详情 ${job.id}`,
    exact: true,
  });
  await expect(detail.getByLabel("媒体任务修订", { exact: true })).toHaveText(
    "修订 7",
  );
  await expect(panel).toContainText("当前为只读状态");
  await expect(
    detail.getByRole("button", { name: "重试媒体任务", exact: true }),
  ).toHaveCount(0);
  expect(
    env.reads.some((url) =>
      new URL(url).pathname.endsWith(`/generation-jobs/${job.id}`),
    ),
  ).toBe(true);
  expect(env.mutations).toHaveLength(0);
});

test("UI-07 UI7-02/06/07：待审队列独立分页并保留各来源精确版本和原run", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const episode = `/projects/${env.project.id}/episodes/${env.episodes[0].id}`;
  const run = crypto.randomUUID();
  const rows = [
    env.createReview({ source: "OUTLINE", kind: "OUTLINE" }),
    env.createReview({ source: "SCRIPT", kind: "SCRIPT", run_id: run }),
    env.createReview({ source: "STORYBOARD", kind: "STORYBOARD" }),
    env.createReview({ source: "WORKFLOW", kind: "WORKFLOW", run_id: run }),
    env.createReview({ source: "HUMAN_REVIEW", kind: "REFERENCE" }),
    env.createReview({
      source: "HUMAN_REVIEW",
      kind: "EXCEPTIONAL_SHOT",
      asset_id: crypto.randomUUID(),
    }),
    env.createReview({ source: "HUMAN_REVIEW", kind: "EXPORT" }),
    env.createReview({ source: "HUMAN_REVIEW", kind: "DELIVERY" }),
  ];
  const paths = [
    `${episode}?view=outline&outline=${rows[0].version_id}`,
    `${episode}?view=list&node=script:${rows[1].version_id}&run=${run}`,
    `${episode}?view=list&node=storyboard:${rows[2].version_id}`,
    `${episode}?view=tasks&run=${run}`,
    `/projects/${env.project.id}?section=assets&proposal=${rows[4].object_id}&proposal_version=${rows[4].version_id}`,
    `/projects/${env.project.id}?section=assets&asset=${rows[5].asset_id}&asset_review=${rows[5].version_id}&review=${rows[5].id}`,
    `${episode}?view=render&render_manifest=${rows[6].object_id}&review=${rows[6].id}`,
    `${episode}?view=render&delivery=${rows[7].object_id}`,
  ];
  env.reviews.push(
    ...rows,
    ...Array.from({ length: 13 }, () => env.createReview()),
  );
  env.reviews[20].blocking_code = productionSecret;
  Object.assign(env.reviews[20], { internal_diagnostic: productionSecret });
  const list = await env.openProduction("reviews");
  await expect(list.getByRole("article")).toHaveCount(20);
  for (const [index, path] of paths.entries())
    await expect(
      list.getByRole("article").nth(index).getByRole("link", {
        name: "查看精确审核",
        exact: true,
      }),
    ).toHaveAttribute("href", path);
  await page.getByRole("button", { name: "下一页待审", exact: true }).click();
  await expect(list.getByRole("article")).toHaveCount(1);
  await expect(list).toContainText(env.reviews[20].version_id!);
  await expect(list).toContainText("审核当前受阻，请在原工作区核对");
  await expect(page.locator("body")).not.toContainText(productionSecret);
  expect(
    env.productionReads.some(
      (url) =>
        url.pathname.endsWith("/pending-reviews") &&
        url.searchParams.get("limit") === "20" &&
        url.searchParams.get("offset") === "20",
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "上一页待审", exact: true }).click();
  await expect(list.getByRole("article")).toHaveCount(20);
});

test("UI-07 UI7-03/06：成片列表分页区分渲染产物与正式交付并打开精确历史", async ({
  page,
}) => {
  const env = await productionFixture(page);
  env.deliveries.push(
    ...Array.from({ length: 21 }, (_, index) =>
      env.createDelivery({ version_no: 21 - index }),
    ),
  );
  env.deliveries[0].is_delivery = true;
  env.deliveries[0].acceptance = {
    ...env.deliveries[0].acceptance,
    phase: "COMPLETED",
    is_delivery: true,
  };
  const list = await env.openProduction("deliveries");
  await expect(list.getByRole("article")).toHaveCount(20);
  await expect(list.getByRole("article").first()).toContainText("正式交付");
  await expect(list.getByRole("article").nth(1)).toContainText("渲染产物");
  await page.getByRole("button", { name: "下一页成片", exact: true }).click();
  await expect(list.getByRole("article")).toHaveCount(1);
  const old = env.deliveries[20];
  await expect(list).toContainText("成片 v1");
  await expect(
    list.getByRole("link", { name: "查看精确成片", exact: true }),
  ).toHaveAttribute(
    "href",
    `/projects/${env.project.id}/episodes/${env.episodes[0].id}?view=render&delivery=${old.id}`,
  );
  expect(
    env.productionReads.some(
      (url) =>
        url.pathname.endsWith("/deliveries") &&
        url.searchParams.get("offset") === "20",
    ),
  ).toBe(true);
});

test("UI-07 UI7-03：全项目成片入口精确GET读取未在剧集目录首屏的历史版本", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const old = env.delivery(0, 1, "PREVIEW");
  env.deliveries.splice(
    0,
    env.deliveries.length,
    ...Array.from({ length: 31 }, (_, index) =>
      env.delivery(0, 32 - index, "PREVIEW"),
    ),
    old,
  );
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== `/api/v1/projects/${env.project.id}/deliveries`)
      return route.fallback();
    return route.fulfill({
      json: {
        items: [
          {
            ...old,
            episode_no: 1,
            episode_title: env.episodes[0].title,
            detail_url: "/ignored-untrusted-destination",
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      },
    });
  });
  await page.goto(`/projects/${env.project.id}?section=deliveries`);
  await closeDirectory(page);
  await page.getByRole("link", { name: "查看精确成片", exact: true }).click();
  const detail = page.getByLabel("当前成片版本", { exact: true });
  await expect(detail).toContainText("成片版本 v1");
  await expect(
    page.getByRole("button", { name: "查看成片版本 1", exact: true }),
  ).toHaveCount(0);
  expect(
    env.reads.some(
      (url) =>
        new URL(url).pathname ===
        `/api/v1/episodes/${env.episodes[0].id}/deliveries/${old.id}`,
    ),
  ).toBe(true);
});

for (const canApprove of [true, false]) {
  test(`UI-07 UI7-02/08：跨页画面例外精确GET保留服务端can_approve=${canApprove}`, async ({
    page,
  }) => {
    const env = await continuityFixture(page, true);
    const model = env.models[1];
    const original = structuredClone(model.human_reviews[0]);
    model.human_reviews.length = 0;
    const summary = {
      id: original.id,
      source: "HUMAN_REVIEW",
      project_id: env.project.id,
      episode_id: env.episode.id,
      episode_no: 1,
      episode_title: env.episode.title,
      kind: "EXCEPTIONAL_SHOT",
      object_id: model.version.id,
      version_id: model.version.id,
      asset_id: model.asset.id,
      run_id: null,
      status: "WAITING_REVIEW",
      blocking_code: null,
      created_at: "2026-11-11T01:00:00Z",
      detail_url: "/ignored-untrusted-destination",
    };
    const exactReads: string[] = [];
    await page.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      if (
        url.pathname === `/api/v1/projects/${env.project.id}/pending-reviews`
      ) {
        const offset = Number(url.searchParams.get("offset"));
        return route.fulfill({
          json: {
            items:
              offset === 20
                ? [summary]
                : Array.from({ length: 20 }, () => ({
                    ...summary,
                    id: crypto.randomUUID(),
                  })),
            total: 21,
            limit: 20,
            offset,
          },
        });
      }
      if (url.pathname.endsWith(`/continuity/human-reviews/${original.id}`)) {
        exactReads.push(url.pathname);
        return route.fulfill({
          json: {
            ...original,
            project_id: env.project.id,
            snapshot: {
              ...original.snapshot,
              project_id: env.project.id,
              kind: "EXCEPTIONAL_SHOT",
            },
            can_approve: canApprove,
          },
        });
      }
      return route.fallback();
    });
    await page.goto(`/projects/${env.project.id}?section=reviews`);
    await closeDirectory(page);
    await expect(
      page.getByLabel("项目待审列表").getByRole("article"),
    ).toHaveCount(20);
    await page.getByRole("button", { name: "下一页待审", exact: true }).click();
    await expect(
      page.getByLabel("项目待审列表").getByRole("article"),
    ).toHaveCount(1);
    await page.getByRole("link", { name: "查看精确审核", exact: true }).click();
    const dialog = page.getByRole("dialog", {
      name: "逐图比较与连续性审核",
      exact: true,
    });
    const review = dialog.getByRole("region", {
      name: "精确版本人工处理",
      exact: true,
    });
    await expect(review).toContainText(original.id);
    await review
      .getByLabel("人工处理意见（必填）", { exact: true })
      .fill("核对本次精确图片与原始审核证据。");
    const accept = review.getByRole("button", {
      name: "接受本次例外",
      exact: true,
    });
    if (canApprove) await expect(accept).toBeEnabled();
    else await expect(accept).toBeDisabled();
    expect(exactReads).toContain(
      `/api/v1/projects/${env.project.id}/assets/${model.asset.id}/versions/${model.version.id}/continuity/human-reviews/${original.id}`,
    );
    expect(env.mutations).toHaveLength(0);
  });
}

test("UI-07 UI7-05/06：结果缩减自动回到有效页，筛选空结果与初始空态均不伪造记录", async ({
  page,
}) => {
  const env = await productionFixture(page);
  env.tasks.push(...Array.from({ length: 40 }, () => env.createTask()));
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(20);
  for (const expectedPage of [2, 3]) {
    await panel.getByRole("button", { name: "下一页", exact: true }).click();
    await expect(panel).toContainText(`第 ${expectedPage} 页`);
  }
  env.tasks.length = 21;
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  await expect(panel).toContainText("共 21 项；第 2 页");
  await expect(panel.getByRole("article")).toHaveCount(1);
  await chooseProductionFilter(page, "任务类型", "文字创作");
  await panel.getByRole("button", { name: "应用筛选", exact: true }).click();
  await expect(panel).toContainText("没有符合条件的任务");
  await expect(panel).toContainText("共 0 项；第 1 页");
  expect(env.productionReads.at(-1)!.searchParams.get("offset")).toBe("0");
  await panel.getByRole("button", { name: "重置筛选", exact: true }).click();
  await expect(panel.getByRole("article")).toHaveCount(20);
  env.tasks.length = 0;
  await panel.getByRole("button", { name: "刷新任务", exact: true }).click();
  await expect(
    panel.getByRole("heading", { name: "没有符合条件的任务", exact: true }),
  ).toBeVisible();
  await expect(panel.getByRole("article")).toHaveCount(0);
});

test("UI-07 UI7-04：不同family的同编号任务保留独立条目及精确入口", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const id = crypto.randomUUID();
  env.tasks.splice(
    0,
    1,
    env.createTask({ id, family: "WORKFLOW", task_type: "TEXT" }),
    env.createTask({ id, family: "GENERATION", task_type: "IMAGE" }),
    env.createTask({ id, family: "RENDER", task_type: "RENDER" }),
  );
  const panel = await env.openProduction();
  await expect(panel.getByRole("article")).toHaveCount(3);
  const links = panel.getByRole("link", {
    name: `查看任务详情 ${id}`,
    exact: true,
  });
  await expect(links).toHaveCount(3);
  for (const [index, query] of [
    `view=tasks&run=${id}`,
    `view=tasks&generation_job=${id}`,
    `view=render&render_job=${id}`,
  ].entries())
    await expect(links.nth(index)).toHaveAttribute(
      "href",
      `/projects/${env.project.id}/episodes/${env.episodes[0].id}?${query}`,
    );
});

test("UI-07 UI7-02/09：精确文字版本前往原审核run，原run不在当前剧集目录仍可达", async ({
  page,
}) => {
  const env = await productionFixture(page);
  const episode = env.episodes[0];
  const runId = crypto.randomUUID();
  const version: ContentVersion = {
    id: crypto.randomUUID(),
    episode_id: episode.id,
    version_no: 1,
    status: "READY_FOR_REVIEW",
    run_id: runId,
    content_json: {
      title: "原工作流的精确待审核稿",
      summary: "沿用原运行审核。",
      characters: [],
      scenes: [
        { scene_no: 1, title: "书店", action: "推开书店的门。", dialogue: [] },
      ],
      estimated_duration_ms: 30000,
    },
  };
  const run: Run = {
    id: runId,
    episode_id: episode.id,
    workflow_type: "script",
    status: "WAITING_REVIEW",
    revision: 7,
    pending_review: { resource_type: "script", version_id: version.id },
    error_code: null,
    created_at: "2026-11-11T01:00:00Z",
  };
  const firstPageRuns = Array.from({ length: 100 }, () => ({
    ...run,
    id: crypto.randomUUID(),
    status: "SUCCEEDED",
    pending_review: null,
  }));
  const graph: Graph = {
    nodes: [
      {
        id: `script:${version.id}`,
        kind: "script",
        resource_id: version.id,
        title: "原工作流的精确待审核稿",
        status: version.status,
        version_no: 1,
      },
    ],
    edges: [],
    layout: { revision: 0, positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
  };
  env.reviews.push(
    env.createReview({
      source: "HUMAN_REVIEW",
      kind: "SCRIPT",
      object_id: version.id,
      version_id: version.id,
      run_id: runId,
    }),
  );
  const exactReads: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === `/api/v1/episodes/${episode.id}/scripts`)
      return route.fulfill({ json: [version] });
    if (path === `/api/v1/episodes/${episode.id}/canvas`)
      return route.fulfill({ json: graph });
    if (path === `/api/v1/episodes/${episode.id}/runs`)
      return route.fulfill({
        json: url.searchParams.get("offset") === "0" ? firstPageRuns : [run],
      });
    if (path === `/api/v1/runs/${runId}`) {
      exactReads.push(path);
      return route.fulfill({ json: run });
    }
    return route.fallback();
  });
  await env.openProduction("reviews");
  await page.getByRole("link", { name: "查看精确审核", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("node"))
    .toBe(`script:${version.id}`);
  expect(new URL(page.url()).searchParams.get("run")).toBe(runId);
  await page
    .getByRole("button", { name: "前往任务审核此版本", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`view=tasks.*run=${runId}`));
  const selected = page.getByRole("article", {
    name: `所选创作任务 ${runId}`,
    exact: true,
  });
  await expect(selected).toContainText(version.id);
  await expect(selected).toContainText("修订 7");
  expect(exactReads).toContain(`/api/v1/runs/${runId}`);
  expect(firstPageRuns.some((row) => row.id === runId)).toBe(false);
});
