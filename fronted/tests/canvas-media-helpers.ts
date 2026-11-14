import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, type Page, type Route } from "@playwright/test";
import type { Graph, GraphNode } from "../src/types/api";
import type { AudioFile } from "../src/features/audio/types";
import { timelineFixture } from "./timeline-helpers";
import { closeDirectory } from "./workspace-helpers";

const id = () => crypto.randomUUID();
const sha = (value: Buffer) => createHash("sha256").update(value).digest("hex");

// Deterministic original PCM waveform, not a provider-quality or listening test.
function waveform() {
  const rate = 16000,
    samples = rate * 2;
  const bytes = Buffer.alloc(44 + samples * 2);
  bytes.write("RIFF", 0);
  bytes.writeUInt32LE(bytes.length - 8, 4);
  bytes.write("WAVEfmt ", 8);
  bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20);
  bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(rate, 24);
  bytes.writeUInt32LE(rate * 2, 28);
  bytes.writeUInt16LE(2, 32);
  bytes.writeUInt16LE(16, 34);
  bytes.write("data", 36);
  bytes.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++)
    bytes.writeInt16LE(
      Math.round(3000 * Math.sin((i * 440 * Math.PI * 2) / rate)),
      44 + i * 2,
    );
  return bytes;
}

export async function canvasMediaFixture(page: Page) {
  const env = await timelineFixture(page);
  env.setMedia(await readFile(resolve("tests/fixtures/timeline-preview.mp4")));
  const png = await readFile(
    resolve("../backend/tests/fixtures/continuity_rules/prop.png"),
  );
  const wav = waveform();
  const version = (
    type: string,
    title: string,
    extra: Partial<GraphNode> = {},
  ): GraphNode => {
    const resource_id = id();
    return {
      id: "asset-version:" + resource_id,
      kind: "asset_version",
      resource_id,
      asset_id: id(),
      asset_type: type,
      title,
      status: "DRAFT",
      version_no: 1,
      checksum: sha(wav),
      ...extra,
    };
  };
  const voice = version("AUDIO", "雨停了 · 原句配音", {
    preview_kind: "AUDIO",
    media_file_id: id(),
    media_type: "audio/wav",
    media_episode_id: env.episodes[0].id,
  });
  const music = version("MUSIC", "街角雨声 · 环境音效", {
    preview_kind: "AUDIO",
    media_file_id: id(),
    media_type: "audio/wav",
    current: true,
  });
  const image = version("SHOT_IMAGE", "红色箱子 · 镜头候选", {
    preview_kind: "IMAGE",
    checksum: sha(png),
    current: false,
  });
  image.preview_path = `/projects/${env.project.id}/assets/${image.asset_id}/versions/${image.resource_id}/image`;
  const history = version("SHOT_IMAGE", "红色箱子 · 历史选用", {
    ...image,
    title: "红色箱子 · 历史选用",
    resource_id: id(),
    version_no: 2,
    current: true,
    status: "STALE",
    stale: true,
  });
  history.id = "asset-version:" + history.resource_id;
  history.preview_path = `/projects/${env.project.id}/assets/${history.asset_id}/versions/${history.resource_id}/image`;
  const timeline = version("TIMELINE", "雨后街角 · 五轨剪辑", {
    resource_id: env.original.id,
    asset_id: env.original.asset_id,
    preview_kind: "TIMELINE",
    media_episode_id: env.episodes[0].id,
    checksum: env.original.content_sha256,
    status: "APPROVED",
  });
  timeline.id = "asset-version:" + timeline.resource_id;
  const movie = (index: number, title: string) => {
    const row = env.deliveries[index];
    const node = version("RENDER", title, {
      resource_id: row.asset_version_id,
      asset_id: row.asset_id,
      version_no: row.version_no,
      preview_kind: "DELIVERY",
      delivery_id: row.id,
      media_episode_id: row.episode_id,
      checksum: row.sha256,
      status: "APPROVED",
    });
    node.id = "asset-version:" + node.resource_id;
    return node;
  };
  const delivery = movie(0, "雨后街角 · 正式成片");
  const preview = movie(1, "雨后街角 · 预览成片");
  const missing = version("AUDIO", "尚未登记的声音");
  const asset: GraphNode = {
    id: "asset:" + image.asset_id,
    kind: "asset",
    resource_id: image.asset_id,
    asset_id: image.asset_id,
    asset_type: "SHOT_IMAGE",
    title: "红色箱子 · 图片资产",
    status: "ACTIVE",
  };
  const nodes = [
    asset,
    image,
    history,
    voice,
    music,
    timeline,
    delivery,
    preview,
    missing,
  ];
  const value: Graph = {
    nodes,
    edges: [
      {
        id: "dependency:" + id(),
        source: image.id,
        target: timeline.id,
        relation: "dependency",
        dependency_type: "REFERENCE_DEPENDENCY",
        dependency_manifest_id: env.original.dependency_manifest_id,
        label: "参考依赖",
      },
    ],
    layout: {
      revision: 1,
      positions: Object.fromEntries(
        nodes.map((n, i) => [
          n.id,
          { x: (i % 3) * 280, y: Math.floor(i / 3) * 340 },
        ]),
      ),
      viewport: { x: 35, y: 45, zoom: 0.7 },
    },
  };
  const files = [voice, music].map((node): AudioFile => ({
    id: node.media_file_id!,
    project_id: env.project.id,
    asset_version_id: node.resource_id!,
    checksum: node.checksum!,
    byte_size: wav.length,
    media_type: "audio/wav",
    original_name: "original-wave.wav",
    source_note: "Original offline waveform",
    rights_note: "Original test fixture",
    rights_confirmed: true,
  }));
  const control: {
    status?: number;
    metadata?: Partial<AudioFile>;
    corrupt?: boolean;
    audioGate?: Promise<void>;
    timelineGate?: Promise<void>;
    graphGate?: Promise<void>;
    layoutConflict?: boolean;
  } = {};
  const reads: string[] = [],
    writes: { path: string; body: Record<string, unknown> }[] = [];
  const fail = (route: Route) =>
    route.fulfill({
      status: control.status ?? 403,
      json: { error: { code: "FORBIDDEN", message: "当前项目权限已撤回。" } },
    });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    const ep = env.episodes.find((e) =>
      path.startsWith(`/api/v1/episodes/${e.id}/canvas`),
    );
    if (ep) {
      if (control.graphGate) await control.graphGate;
      if (control.status) return fail(route);
      if (request.method() === "PUT") {
        const body = request.postDataJSON();
        writes.push({ path, body });
        if (control.layoutConflict)
          return route.fulfill({
            status: 409,
            json: {
              error: {
                code: "LAYOUT_CONFLICT",
                message: "布局已更新，请刷新后重试。",
              },
            },
          });
        value.layout = { ...body, revision: value.layout.revision + 1 };
        return route.fulfill({ json: value.layout });
      }
      return route.fulfill({ json: { ...value, episode_id: ep.id } });
    }
    if (
      path === `/api/v1/timelines/${env.original.id}` &&
      control.timelineGate
    ) {
      reads.push(path);
      await control.timelineGate;
      return route.fulfill({ json: env.original });
    }
    if (
      [image.preview_path, history.preview_path].some(
        (p) => "/api/v1" + p === path,
      )
    ) {
      reads.push(path);
      if (control.status) return fail(route);
      return route.fulfill({ contentType: "image/png", body: png });
    }
    const file = files.find((f) =>
      path.startsWith(`/api/v1/projects/${env.project.id}/files/${f.id}`),
    );
    if (file) {
      reads.push(path);
      if (control.status) return fail(route);
      if (control.audioGate) await control.audioGate;
      if (path.endsWith("/content"))
        return route.fulfill({
          contentType: "audio/wav",
          body: control.corrupt ? Buffer.from("bad audio") : wav,
        });
      return route.fulfill({ json: { ...file, ...control.metadata } });
    }
    return route.fallback();
  });
  const href = (node?: GraphNode, view = "list", index = 0) =>
    `/projects/${env.project.id}/episodes/${env.episodes[index].id}?view=${view}${node ? "&node=" + encodeURIComponent(node.id) : ""}`;
  const open = async (node?: GraphNode, view = "list") => {
    await page.goto(href(node, view));
    await closeDirectory(page);
    await expect(
      page.getByRole("region", { name: "剧集工作区", exact: true }),
    ).toHaveAttribute("aria-busy", "false");
    return page.getByRole("region", { name: "画布资产详情", exact: true });
  };
  const select = async (node: GraphNode) => {
    const list = page.getByRole("region", {
      name: "内容与媒体列表",
      exact: true,
    });
    await list.locator(`button[data-node-id="${node.id}"]`).click();
    return page.getByRole("region", { name: "画布资产详情", exact: true });
  };
  return {
    ...env,
    graph: value,
    files,
    voice,
    music,
    image,
    history,
    timeline,
    delivery,
    preview,
    missing,
    controlMedia: control,
    mediaReads: reads,
    layoutWrites: writes,
    nodes,
    href,
    openCanvas: open,
    selectNode: select,
  };
}
