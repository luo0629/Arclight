import { test, expect } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

test("TXT-08：余额耗尽、限流和密钥错误分别提示，保留手动重试", async ({
  page,
}) => {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `text-quota-${crypto.randomUUID()}@example.com`,
      password: "text-provider-quota-test",
      display_name: "额度提示验收",
    },
  });
  expect(registered.status()).toBe(201);
  const headers = {
    Authorization: `Bearer ${(await registered.json()).access_token}`,
  };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "文字服务提示" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: { episode_no: 1, title: "余额不足" },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const runs = [
    "GATEWAY_QUOTA_EXHAUSTED",
    "GATEWAY_PROVIDER_RATE_LIMITED",
    "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
    "GATEWAY_RECONCILIATION_REQUIRED",
  ].map((error_code) => ({
    id: crypto.randomUUID(),
    episode_id: episode.id,
    workflow_type: "script_storyboard",
    status: "FAILED",
    revision: 1,
    error_code,
    pending_review: null,
    created_at: new Date().toISOString(),
  }));
  await page.route(`**/episodes/${episode.id}/runs?*`, (route) =>
    route.fulfill({ json: runs }),
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "余额不足或额度已耗尽" }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "请求过于频繁" }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "密钥无效或已失效" }),
  ).toBeVisible();
  await expect(
    page.getByRole("alert").filter({ hasText: "模型调用结果尚未确认" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "重试任务", exact: true }),
  ).toHaveCount(4);
  const task = page.locator(".task").filter({ hasText: runs[0].id });
  await page.route(`**/runs/${runs[0].id}/retry`, (route) =>
    route.fulfill({ status: 202, json: {} }),
  );
  const retried = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().endsWith(`/runs/${runs[0].id}/retry`),
  );
  await task.getByRole("button", { name: "重试任务", exact: true }).click();
  await retried;
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
});
