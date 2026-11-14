import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { renderFixture } from "./render-helpers";
import type { Timeline, TimelineClip } from "../src/features/rendering/types";
import type { SubtitleTrack } from "../src/features/rendering/subtitle-api";

export async function timelineFixture(page: Page) {
  const env = await renderFixture(page);
  const original = env.timelines[0];
  const video = original.snapshot.tracks.find((t) => t.kind === "VIDEO")!;
  video.clips[0].duration_ms = 1500;
  video.clips.push({
    ...structuredClone(video.clips[0]),
    id: crypto.randomUUID(),
    shot_id: crypto.randomUUID(),
    ordinal: 1,
    start_ms: 1500,
  });
  const clip = (kind: string): TimelineClip => ({
    ...structuredClone(video.clips[0]),
    id: crypto.randomUUID(),
    source_asset_version_id: crypto.randomUUID(),
    source_sha256: "b".repeat(64),
    start_ms: kind === "SUBTITLE" ? 250 : 0,
    duration_ms: kind === "SUBTITLE" ? 500 : 1000,
    shot_id: kind === "SFX" ? video.clips[0].shot_id : null,
    review_id: null,
    visual: null,
    script_version_id: ["SUBTITLE", "VOICE"].includes(kind)
      ? env.sourceBoards[0].script_version_id!
      : null,
    line_id: ["SUBTITLE", "VOICE"].includes(kind) ? "rain-line" : null,
    audio:
      kind === "MUSIC"
        ? {
            purpose_role: "BACKGROUND",
            gain_milli: 650,
            fade_in_ms: 100,
            fade_out_ms: 100,
            envelope: [
              { offset_ms: 0, gain_milli: 500 },
              { offset_ms: 1000, gain_milli: 800 },
            ],
          }
        : null,
  });
  for (const track of original.snapshot.tracks.filter(
    (t) => t.kind !== "VIDEO",
  ))
    track.clips = [clip(track.kind)];
  const voice = original.snapshot.tracks.find((t) => t.kind === "VOICE")!
    .clips[0];
  const subtitleClip = original.snapshot.tracks.find(
    (t) => t.kind === "SUBTITLE",
  )!.clips[0];
  subtitleClip.audio_version_id = voice.source_asset_version_id;
  const subtitle: SubtitleTrack = {
    id: subtitleClip.source_asset_version_id,
    asset_id: crypto.randomUUID(),
    version_no: 1,
    status: "APPROVED",
    stale: false,
    checksum: subtitleClip.source_sha256,
    timeline_id: original.id,
    clips: [subtitleClip],
    cues: [
      {
        cue_no: 1,
        text: "雨停了。",
        start_ms: 250,
        duration_ms: 500,
        line_id: "rain-line",
        script_version_id: voice.script_version_id!,
        audio_asset_version_id: voice.source_asset_version_id,
      },
    ],
  };
  const subtitles = [subtitle];
  env.dialogueLines.push({
    script_version_id: voice.script_version_id!,
    line_id: "rain-line",
    text: "雨停了。",
  });
  const fonts = [
    {
      id: crypto.randomUUID(),
      project_id: env.project.id,
      original_name: "Noto Sans SC.ttf",
      media_type: "font/ttf",
      rights_confirmed: true,
      source_note: "测试字体",
      rights_note: "OFL",
    },
  ];
  const control: typeof env.control & {
    saveGate?: Promise<void>;
    conflict?: boolean;
    networkFailure?: boolean;
    wrongSave?: boolean;
    subtitleConflict?: boolean;
  } = env.control;
  const saves: { id: string; body: Record<string, unknown> }[] = [];
  const subtitleWrites: Record<string, unknown>[] = [];
  const saved = new Map<string, Timeline>();
  await page.route("**/api/v1/timelines/*", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    const base = env.timelines.find((t) =>
      route
        .request()
        .url()
        .endsWith("/" + t.id),
    );
    if (!base) return route.fallback();
    const body = route.request().postDataJSON();
    saves.push({ id: base.id, body });
    if (control.saveGate) await control.saveGate;
    if (control.conflict)
      return route.fulfill({
        status: 409,
        json: {
          error: { code: "VERSION_CONFLICT", message: "时间轴已有更新版本" },
        },
      });
    let result = saved.get(body.creation_key);
    if (!result) {
      result = {
        ...structuredClone(base),
        id: crypto.randomUUID(),
        version_no: base.version_no + 1,
        status: "DRAFT",
        based_on_timeline_id: base.id,
        snapshot: structuredClone(body.snapshot),
        content_sha256: createHash("sha256")
          .update(JSON.stringify(body.snapshot))
          .digest("hex"),
      };
      for (const t of result.snapshot.tracks) {
        t.id = crypto.randomUUID();
        for (const c of t.clips) c.id = crypto.randomUUID();
      }
      env.timelines.unshift(result);
      saved.set(body.creation_key, result);
    }
    if (control.networkFailure) {
      control.networkFailure = false;
      return route.abort("failed");
    }
    return route.fulfill({
      status: 201,
      json: control.wrongSave
        ? { ...result, based_on_timeline_id: crypto.randomUUID() }
        : result,
    });
  });
  await page.route(`**/api/v1/projects/${env.project.id}/files?*`, (route) =>
    route.fulfill({ json: fonts }),
  );
  await page.route(
    `**/api/v1/episodes/${env.episodes[0].id}/subtitle-tracks*`,
    (route) => route.fulfill({ json: subtitles }),
  );
  await page.route("**/api/v1/subtitle-tracks/**", async (route) => {
    const source = subtitles.find((row) =>
      route.request().url().includes(row.id),
    );
    if (!source) return route.fallback();
    if (route.request().method() === "GET")
      return route.fulfill({ json: source });
    const body = route.request().postDataJSON();
    subtitleWrites.push(body);
    if (control.subtitleConflict)
      return route.fulfill({
        status: 409,
        json: {
          error: { code: "VERSION_CONFLICT", message: "字幕目录已有新版本" },
        },
      });
    const next = structuredClone(source);
    next.id = crypto.randomUUID();
    next.version_no++;
    next.status = "DRAFT";
    next.checksum = "c".repeat(64);
    next.source_cues = source.source_cues ?? source.cues;
    for (const change of body.changes) {
      const cue = next.cues.find((cue) => cue.cue_no === change.cue_no)!;
      Object.assign(cue, {
        text: change.text,
        start_ms: change.start_ms,
        duration_ms: change.end_ms - change.start_ms,
      });
    }
    next.clips = next.cues.map((cue, ordinal) => ({
      ...structuredClone(subtitleClip),
      id: crypto.randomUUID(),
      ordinal,
      source_asset_version_id: next.id,
      source_sha256: next.checksum,
      start_ms: cue.start_ms,
      duration_ms: cue.duration_ms,
    }));
    subtitles.push(next);
    return route.fulfill({ status: 201, json: next });
  });
  await page.route(
    `**/api/v1/projects/${env.project.id}/assets/**`,
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (!path.includes(subtitle.asset_id)) return route.fallback();
      const version = subtitles.find((row) => path.includes(row.id));
      if (!version)
        return route.fulfill({
          json: {
            id: subtitle.asset_id,
            project_id: env.project.id,
            revision: subtitles.length - 1,
            status: "DRAFT",
            asset_type: "SUBTITLE",
          },
        });
      if (route.request().method() === "PATCH") {
        version.status = route.request().postDataJSON().status;
      }
      return route.fulfill({
        json: {
          id: version.id,
          asset_id: subtitle.asset_id,
          checksum: version.checksum,
          revision: version.status === "DRAFT" ? 0 : 1,
          status: version.status,
          stale: false,
        },
      });
    },
  );
  async function open() {
    const panel = await env.open();
    await panel
      .locator("summary")
      .filter({ hasText: /^时间轴编辑器/ })
      .click();
    const editor = panel.getByRole("region", {
      name: "时间轴编辑器",
      exact: true,
    });
    await expect(editor).toBeVisible();
    return { panel, editor };
  }
  return {
    ...env,
    original,
    fonts,
    subtitles,
    subtitleWrites,
    saves,
    control,
    openEditor: open,
  };
}
