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

test("DAG-05：按新精确输入另存图片候选，重新审核且保留原清单", async ({
  page,
}) => {
  const f = await fixture(page);
  await page.getByRole("button", { name: "选用此版本", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认影响并继续", exact: true })
    .click();
  await expect(
    page.getByText("已选用此精确版本", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /下游场景/ }).click();
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 已失效", exact: true })
    .click();
  await page
    .getByLabel("版本状态说明", { exact: true })
    .fill("人工确认新角色参考仍适用此图");
  const rebuild = page.getByRole("region", {
    name: "确认重建输入",
    exact: true,
  });
  await expect(
    rebuild.getByLabel("重建输入版本 1", { exact: true }),
  ).toHaveValue(f.second.id);
  await rebuild
    .getByRole("button", { name: "按新输入另存并重新确认此图", exact: true })
    .click();
  const details = page.getByLabel("精确资产版本详情", { exact: true });
  await expect(details.getByRole("heading", { name: /^版本 v/ })).toContainText(
    "版本 v2 · 候选草稿",
  );
  await page.getByRole("button", { name: "提交版本审核", exact: true }).click();
  await page.getByRole("button", { name: "批准此版本", exact: true }).click();
  await expect(details.getByRole("heading", { name: /^版本 v/ })).toContainText(
    "已批准",
  );
  const versions = await (
    await page.request.get(f.root + `/assets/${f.child.target.id}/versions`, {
      headers: f.headers,
    })
  ).json();
  expect(versions).toHaveLength(2);
  expect(versions[0].stale).toBe(true);
  expect(versions[1].stale).toBe(false);
  expect(versions[0].checksum).toBe(versions[1].checksum);
  const manifest = await (
    await page.request.get(
      f.root + `/dependency-manifests/${versions[1].dependency_manifest_id}`,
      { headers: f.headers },
    )
  ).json();
  expect(manifest.snapshot.inputs[0].source_asset_version_id).toBe(f.second.id);
  expect(manifest.snapshot.context.rebuild_from_version_id).toBe(
    f.child.version.id,
  );
  expect(
    await (
      await page.request.get(
        f.root + `/dependency-manifests/${f.child.manifest.id}`,
        { headers: f.headers },
      )
    ).json(),
  ).toEqual(f.child.manifest);
});

test("DAG-05：新输入导入替换图片，失败重试沿用清单和候选标识", async ({
  page,
}) => {
  const f = await fixture(page);
  await page.getByRole("button", { name: /下游场景/ }).click();
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 候选草稿", exact: true })
    .click();
  await page
    .getByLabel("版本状态说明", { exact: true })
    .fill("替换为新制作图片");
  await page
    .getByRole("button", { name: "按新输入导入替换图片", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("版本图片", { exact: true }).setInputFiles({
    name: "new.png",
    mimeType: "image/png",
    buffer: Buffer.from(png, "base64"),
  });
  await dialog
    .getByLabel("图片来源说明", { exact: true })
    .fill("新输入下人工制作");
  await dialog
    .getByLabel("图片使用权说明", { exact: true })
    .fill("自有测试图片");
  await dialog
    .getByRole("checkbox", {
      name: "我确认拥有此图片的使用权并同意保存到项目私有目录",
    })
    .check();
  let dropped = false;
  await page.route(`**/assets/${f.child.target.id}/versions`, async (route) => {
    if (route.request().method() === "POST" && !dropped) {
      dropped = true;
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      await route.abort();
    } else await route.continue();
  });
  await dialog
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await dialog
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const versions = await (
    await page.request.get(f.root + `/assets/${f.child.target.id}/versions`, {
      headers: f.headers,
    })
  ).json();
  expect(versions).toHaveLength(2);
  expect(versions[1].dependency_manifest_id).toBeTruthy();
});

test("DAG-05：只重生成所选失效分镜，新来源固定并等待重新审核", async ({
  page,
}) => {
  const f = await fixture(page);
  const episodeResponse = await page.request.post(f.root + "/episodes", {
    headers: f.headers,
    data: { episode_no: 1, title: "局部重建" },
  });
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const content = {
    title: "精确故事",
    summary: "旧故事场景",
    estimated_duration_ms: 30000,
    characters: ["主角"],
    scenes: [{ scene_no: 1, title: "雨夜", action: "主角等待", dialogue: [] }],
  };
  async function script(expected: number, summary: string) {
    const response = await page.request.post(prefix + "/scripts", {
      headers: f.headers,
      data: { expected_version_no: expected, content: { ...content, summary } },
    });
    expect(response.status()).toBe(201);
    const row = await response.json();
    expect(
      (
        await page.request.post(prefix + `/scripts/${row.id}/review`, {
          headers: f.headers,
          data: { decision: "APPROVE" },
        })
      ).status(),
    ).toBe(200);
    return row;
  }
  const first = await script(0, "旧故事场景");
  const created = await page.request.post(prefix + "/storyboards", {
    headers: f.headers,
    data: {
      expected_version_no: 0,
      script_version_id: first.id,
      content: {
        shots: [
          {
            shot_no: 1,
            scene_no: 1,
            duration_ms: 30000,
            visual_description: "旧场景画面",
            characters: ["主角"],
          },
        ],
      },
    },
  });
  expect(created.status()).toBe(201);
  const board = await created.json();
  expect(
    (
      await page.request.post(prefix + `/storyboards/${board.id}/review`, {
        headers: f.headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  const second = await script(1, "新故事场景");
  const projectId = f.root.split("/").at(-1);
  await page.goto(`/projects/${projectId}/episodes/${episode.id}?view=list`);
  await openWorkspacePanels(page);
  await page.locator(".version-row").filter({ hasText: "分镜" }).click();
  const panel = page.getByRole("region", {
    name: "按需重生成分镜",
    exact: true,
  });
  await expect(panel).toBeVisible();
  await panel
    .getByRole("combobox", { name: "重生成来源剧本" })
    .selectOption(second.id);
  await panel
    .getByRole("button", { name: "从所选来源重新生成分镜", exact: true })
    .click();
  await expect(
    page.getByText("等待审核：分镜版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  const versions = await (
    await page.request.get(prefix + "/storyboards", { headers: f.headers })
  ).json();
  expect(versions).toHaveLength(2);
  expect(versions[0].script_version_id).toBe(second.id);
  expect(versions[0].status).toBe("READY_FOR_REVIEW");
  expect(versions[1].id).toBe(board.id);
  expect(versions[1].status).toBe("STALE");
  expect(versions[1].content_json).toEqual(board.content_json);
  expect(
    (
      await (
        await page.request.get(
          f.root +
            `/assets/${f.child.target.id}/versions/${f.child.version.id}`,
          { headers: f.headers },
        )
      ).json()
    ).stale,
  ).toBe(false);
});
