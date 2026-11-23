import { expect, type Page, type Route } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";
import type { ProductionPage } from "../src/features/editor/shot-production-types";

export async function shotProductionFixture(
  page: Page,
  {
    singleShot = false,
    outputProfile,
  }: {
    singleShot?: boolean;
    outputProfile?: { width: number; height: number; fps: number };
  } = {},
) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `shot-production-${crypto.randomUUID()}@example.com`,
      password: "production-test-password",
      display_name: "批量生产验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "星光镜头生产", output_profile: outputProfile },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    {
      headers,
      data: { episode_no: 1, title: "提灯过桥", target_duration_ms: 30000 },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  const sceneId = crypto.randomUUID();
  const sourceResponse = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "提灯过桥",
        summary: "女孩循着星光寻找归途。",
        estimated_duration_ms: 30000,
        characters: ["女孩"],
        scenes: [
          {
            scene_id: sceneId,
            scene_no: 1,
            title: "湖边的小桥",
            action: "女孩提灯走过小桥，看见星光落入掌心。",
            dialogue: [],
          },
        ],
      },
    },
  });
  expect(sourceResponse.status()).toBe(201);
  const source = await sourceResponse.json();
  expect(
    (
      await page.request.post(prefix + `/scripts/${source.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  const boardResponse = await page.request.post(prefix + "/storyboards", {
    headers,
    data: {
      expected_version_no: 0,
      script_version_id: source.id,
      content: {
        shots: (singleShot
          ? ["在桥边回头"]
          : ["提灯走入森林", "在桥边回头", "星光落在掌心"]
        ).map((visual_description, index) => ({
          shot_no: index + 1,
          scene_id: sceneId,
          scene_no: 1,
          duration_ms: singleShot ? 30000 : 10000,
          shot_size: "medium",
          visual_description,
          characters: ["女孩"],
          camera_movement: "static",
          subtitle: "",
        })),
      },
    },
  });
  expect(boardResponse.status()).toBe(201);
  const board = await boardResponse.json();
  expect(
    (
      await page.request.post(prefix + `/storyboards/${board.id}/review`, {
        headers,
        data: { decision: "APPROVE" },
      })
    ).status(),
  ).toBe(200);
  const productionPath = prefix + `/storyboards/${board.id}/production`;
  const generationPath = `/api/v1/projects/${project.id}/generation-jobs`;
  async function state() {
    const response = await page.request.get(productionPath, { headers });
    expect(response.status()).toBe(200);
    return response.json() as Promise<ProductionPage>;
  }
  async function jobs() {
    const response = await page.request.get(generationPath, { headers });
    expect(response.status()).toBe(200);
    return response.json() as Promise<
      { id: string; status: string; input_manifest_id: string }[]
    >;
  }
  async function open(navigate = true) {
    if (navigate) {
      await page.goto(`/projects/${project.id}/episodes/${episode.id}`);
      await openWorkspacePanels(page);
      await page.getByRole("radio", { name: "版本列表", exact: true }).click();
      const row = page.locator(`[data-node-id="storyboard:${board.id}"]`);
      await expect(row).toHaveCount(1);
      await row.click();
      await expect(page.locator(".inspector .section-heading h3")).toHaveText(
        "分镜 v1",
      );
    }
    await page
      .getByRole("button", { name: "批量生产镜头", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "镜头批量生成、审核与重试",
      exact: true,
    });
    await expect(
      dialog.getByRole("table", { name: "镜头生产列表" }).getByRole("row"),
    ).toHaveCount(singleShot ? 2 : 4);
    return dialog;
  }
  return {
    project,
    episode,
    board,
    source,
    headers,
    productionPath,
    generationPath,
    state,
    jobs,
    open,
  };
}

export const tinyPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
  "base64",
);
export async function apiFailure(
  route: Route,
  code: string,
  message: string,
  status = 409,
) {
  await route.fulfill({ status, json: { error: { code, message } } });
}
