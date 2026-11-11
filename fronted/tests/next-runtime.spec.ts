import { expect, test } from "@playwright/test";

test("Next.js 首页：元信息、脚本 nonce 与客户端页面正常运行", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const violations: string[] = [];
    Object.assign(window, { cspViolations: violations });
    document.addEventListener("securitypolicyviolation", (event) => {
      violations.push(`${event.effectiveDirective}: ${event.blockedURI}`);
    });
  });
  const response = await page.goto("/");
  if (!response) throw new Error("Home page response is required");
  expect(response.status()).toBe(200);
  const policy = response.headers()["content-security-policy"] ?? "";
  expect(policy).toContain("frame-ancestors 'none'");
  expect(policy.split(";").map((directive) => directive.trim())).toContain(
    "media-src 'self' blob:",
  );
  expect(policy).toContain("'strict-dynamic'");
  expect(policy).toMatch(/'nonce-[^']+'/);
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");
  await expect(page).toHaveTitle("Arclight · 让灵感长成好故事");
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator(".login-page")).toBeVisible();
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  // Inspect document scripts, excluding scripts injected later by devtools/HMR.
  const scripts = await page.evaluate(
    (html) => {
      const document = new DOMParser().parseFromString(html, "text/html");
      return Array.from(
        document.querySelectorAll("script:not([src])"),
        (element) => element.getAttribute("nonce") ?? "",
      );
    },
    await response.text(),
  );
  expect(scripts.length).toBeGreaterThan(0);
  for (const nonce of scripts) {
    expect(nonce).not.toBe("");
    expect(policy).toContain(`'nonce-${nonce}'`);
  }
  expect(
    await page.evaluate(() => Reflect.get(window, "cspViolations")),
  ).toEqual([]);
  expect(errors).toEqual([]);
});

test("Next.js 未知地址：返回 404 并可返回首页", async ({ page }) => {
  const response = await page.goto("/missing-page-for-migration-check");
  expect(response?.status()).toBe(404);
  await expect(
    page.getByRole("heading", { name: "没有找到这个页面" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "返回首页" }).click();
  await expect(page.locator(".login-page")).toBeVisible();
  await expect(page).toHaveURL("/");
});

test("Next.js 同源代理：保留 Origin 校验、会话 Cookie 与认证状态", async ({
  page,
  baseURL,
}) => {
  if (!baseURL) throw new Error("baseURL is required");
  const credentials = {
    email: `${crypto.randomUUID()}@example.com`,
    password: "next-proxy-session-check",
    display_name: "代理会话验收",
  };
  const blocked = await page.request.post("/api/v1/auth/register", {
    headers: { Origin: "https://outside.invalid" },
    data: credentials,
  });
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe("CROSS_ORIGIN_AUTH");
  const headers = { Origin: new URL(baseURL).origin };
  const registered = await page.request.post("/api/v1/auth/register", {
    headers,
    data: credentials,
  });
  expect(registered.status()).toBe(201);
  expect(registered.headers()["set-cookie"]).toContain("HttpOnly");
  const session = await registered.json();
  const refreshed = await page.request.post("/api/v1/auth/refresh", {
    headers,
  });
  expect(refreshed.status()).toBe(200);
  expect((await refreshed.json()).user.id).toBe(session.user.id);
  const loggedOut = await page.request.post("/api/v1/auth/logout", { headers });
  expect(loggedOut.status()).toBe(204);
  expect(
    (await page.request.post("/api/v1/auth/refresh", { headers })).status(),
  ).toBe(401);
});
