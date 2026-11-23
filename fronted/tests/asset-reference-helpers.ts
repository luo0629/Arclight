import { expect, type Page } from "@playwright/test";
const referencePng =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAFUlEQVR4nGNkYPjPgA0wYRVlGMkSADciARf1YEhDAAAAAElFTkSuQmCC";

export async function approvedProp(
  page: Page,
  headers: Record<string, string>,
  projectId: string,
  name: string,
) {
  const root = `/api/v1/projects/${projectId}/assets`;
  const response = await page.request.post(root, {
    headers,
    data: { name, asset_type: "PROP" },
  });
  expect(response.status(), await response.text()).toBe(201);
  const asset = await response.json();
  const versions = `${root}/${asset.id}/versions`;
  const created = await page.request.post(versions, {
    headers,
    data: {
      expected_revision: asset.revision,
      creation_key: crypto.randomUUID(),
      original_name: "prop.png",
      media_type: "image/png",
      content_base64: referencePng,
      source_note: "测试参考图",
      rights_note: "测试用合成图片",
      rights_confirmed: true,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  let version = await created.json();
  for (const status of ["READY_FOR_REVIEW", "APPROVED"]) {
    const update = await page.request.patch(`${versions}/${version.id}/state`, {
      headers,
      data: { expected_revision: version.revision, status },
    });
    expect(update.status(), await update.text()).toBe(200);
    version = await update.json();
  }
  return {
    asset_id: asset.id as string,
    asset_version_id: version.id as string,
  };
}
