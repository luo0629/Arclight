import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

const png =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAFUlEQVR4nGNkYPjPgA0wYRVlGMkSADciARf1YEhDAAAAAElFTkSuQmCC";

async function fixture(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `impact-${crypto.randomUUID()}@example.com`,
      password: "impact-test-password",
      display_name: "影响验收",
    },
  });
  expect(registered.status()).toBe(201);
  const headers = {
    Authorization: "Bearer " + (await registered.json()).access_token,
  };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "精确变更影响" },
    })
  ).json();
  const root = `/api/v1/projects/${project.id}`;
  async function asset(name: string, asset_type: string) {
    const response = await page.request.post(root + "/assets", {
      headers,
      data: { name, asset_type },
    });
    expect(response.status()).toBe(201);
    return response.json();
  }
  async function image(
    assetId: string,
    approved = false,
    dependency_manifest_id?: string,
  ) {
    const path = root + `/assets/${assetId}`;
    const current = await (await page.request.get(path, { headers })).json();
    const response = await page.request.post(path + "/versions", {
      headers,
      data: {
        expected_revision: current.revision,
        creation_key: crypto.randomUUID(),
        original_name: "test.png",
        media_type: "image/png",
        content_base64: png,
        source_note: "本地测试绘图",
        rights_note: "测试自有图片",
        rights_confirmed: true,
        dependency_manifest_id,
      },
    });
    expect(response.status()).toBe(201);
    let version = await response.json();
    if (approved)
      for (const status of ["READY_FOR_REVIEW", "APPROVED"]) {
        const changed = await page.request.patch(
          path + `/versions/${version.id}/state`,
          { headers, data: { expected_revision: version.revision, status } },
        );
        expect(changed.status()).toBe(200);
        version = await changed.json();
      }
    return version;
  }
  const source = await asset("上游角色", "CHARACTER");
  const first = await image(source.id, true);
  const latest = await (
    await page.request.get(root + `/assets/${source.id}`, { headers })
  ).json();
  const chosen = await page.request.post(
    root + `/assets/${source.id}/versions/${first.id}/select`,
    {
      headers,
      data: {
        expected_revision: latest.revision,
        expected_lock_version: latest.lock_version,
      },
    },
  );
  expect(chosen.status()).toBe(200);
  const second = await image(source.id, true);
  async function downstream(name: string) {
    const target = await asset(name, "SCENE");
    const response = await page.request.post(root + "/dependency-manifests", {
      headers,
      data: {
        creation_key: crypto.randomUUID(),
        target_asset_id: target.id,
        inputs: [
          {
            source_asset_version_id: first.id,
            dependency_type: "REFERENCE_DEPENDENCY",
          },
        ],
      },
    });
    expect(response.status()).toBe(201);
    const manifest = await response.json();
    const version = await image(target.id, false, manifest.id);
    return { target, version, manifest };
  }
  const child = await downstream("下游场景");
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page.getByRole("button", { name: /上游角色/ }).click();
  await page
    .getByRole("button", { name: "查看资产版本 v2 · 已批准", exact: true })
    .click();
  return { root, headers, source, first, second, child, downstream };
}

test("DAG-04：变更前展示真实下游，取消不写入，确认后保留历史清单", async ({
  page,
}, testInfo) => {
  const f = await fixture(page);
  const select = page.getByRole("button", { name: "选用此版本", exact: true });
  await select.click();
  const dialog = page.getByRole("dialog", {
    name: "确认变更影响",
    exact: true,
  });
  await expect(dialog).toContainText("下游场景 · v1 · 重新生成");
  await expect(dialog).toContainText(f.child.version.id);
  await page.screenshot({
    path: testInfo.outputPath("dependency-impact.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("dependency-impact-mobile.png"),
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "取消变更", exact: true }).click();
  const sourcePath = f.root + `/assets/${f.source.id}`;
  expect(
    (await (await page.request.get(sourcePath, { headers: f.headers })).json())
      .current_version_id,
  ).toBe(f.first.id);
  await select.click();
  await dialog
    .getByRole("button", { name: "确认影响并继续", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("已选用此精确版本", { exact: true }),
  ).toBeVisible();
  const output = await (
    await page.request.get(
      f.root + `/assets/${f.child.target.id}/versions/${f.child.version.id}`,
      { headers: f.headers },
    )
  ).json();
  expect(output.stale).toBe(true);
  expect(
    await (
      await page.request.get(
        f.root + `/dependency-manifests/${f.child.manifest.id}`,
        { headers: f.headers },
      )
    ).json(),
  ).toEqual(f.child.manifest);
});

test("DAG-04：分析失败可重试，并发新增下游拒绝旧确认后重新展示", async ({
  page,
}) => {
  const f = await fixture(page);
  let fail = true;
  await page.route("**/dependency-impact?*", async (route) => {
    if (fail) {
      fail = false;
      await route.fulfill({
        status: 503,
        json: { error: { code: "UNAVAILABLE", message: "影响分析暂不可用" } },
      });
    } else await route.continue();
  });
  await page.getByRole("button", { name: "选用此版本", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "影响分析暂不可用" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "重试影响分析", exact: true }).click();
  const dialog = page.getByRole("dialog", {
    name: "确认变更影响",
    exact: true,
  });
  await expect(dialog).toContainText("下游场景");
  const extra = await f.downstream("并发增加的场景");
  await dialog
    .getByRole("button", { name: "确认影响并继续", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "下游影响已变化" }),
  ).toBeVisible();
  expect(
    (
      await (
        await page.request.get(f.root + `/assets/${f.source.id}`, {
          headers: f.headers,
        })
      ).json()
    ).current_version_id,
  ).toBe(f.first.id);
  await page.getByRole("button", { name: "重试影响分析", exact: true }).click();
  await expect(dialog).toContainText(extra.version.id);
  await expect(dialog).toContainText("重新生成 2");
  await dialog
    .getByRole("button", { name: "确认影响并继续", exact: true })
    .click();
  await expect(
    page.getByText("已选用此精确版本", { exact: true }),
  ).toBeVisible();
});

test("DAG-04：切换精确版本丢弃迟到影响分析并恢复新版本操作", async ({
  page,
}) => {
  const f = await fixture(page);
  let seen!: () => void;
  let release!: () => void;
  const started = new Promise<void>((resolve) => {
    seen = resolve;
  });
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    `**/dependency-impact?asset_version_id=${f.second.id}&*`,
    async (route) => {
      const response = await route.fetch();
      seen();
      await gate;
      await route.fulfill({ response }).catch(() => {});
    },
  );
  await page.getByLabel("版本状态说明", { exact: true }).fill("失效说明");
  await page.getByRole("button", { name: "标记版本失效", exact: true }).click();
  await started;
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 已批准", exact: true })
    .click();
  await page.getByLabel("版本状态说明", { exact: true }).fill("当前版本的说明");
  const invalidate = page.getByRole("button", {
    name: "标记版本失效",
    exact: true,
  });
  await expect(invalidate).toBeEnabled();
  release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await invalidate.click();
  const dialog = page.getByRole("dialog", {
    name: "确认变更影响",
    exact: true,
  });
  await expect(dialog).toContainText(f.child.version.id);
  await dialog.getByRole("button", { name: "取消变更", exact: true }).click();
});
