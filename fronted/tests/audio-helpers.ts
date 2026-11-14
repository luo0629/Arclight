import { createHash } from "node:crypto";
import { expect, type Page, type Route } from "@playwright/test";
import type { AssetVersion, ContentVersion } from "../src/types/api";
import type {
  AudioAsset,
  AudioFile,
  AudioHistory,
  AudioImportResult,
  AudioLine,
  AudioResult,
  AudioSnapshot,
  AudioTask,
  AudioVoice,
} from "../src/features/audio/types";
import { renderFixture } from "./render-helpers";
import { closeDirectory } from "./workspace-helpers";

const id = () => crypto.randomUUID();
const now = "2026-11-14T08:00:00Z";
const hash = (bytes: Buffer) =>
  createHash("sha256").update(bytes).digest("hex");

function recording() {
  const data = Buffer.alloc(44 + 16000);
  data.write("RIFF", 0);
  data.writeUInt32LE(data.length - 8, 4);
  data.write("WAVEfmt ", 8);
  data.writeUInt32LE(16, 16);
  data.writeUInt16LE(1, 20);
  data.writeUInt16LE(1, 22);
  data.writeUInt32LE(8000, 24);
  data.writeUInt32LE(16000, 28);
  data.writeUInt16LE(2, 32);
  data.writeUInt16LE(16, 34);
  data.write("data", 36);
  data.writeUInt32LE(16000, 40);
  for (let sample = 0; sample < 8000; sample++)
    data.writeInt16LE(
      Math.round(5000 * Math.sin((sample * Math.PI * 440) / 4000)),
      44 + sample * 2,
    );
  return data;
}

// Real authentication and project/episode access; controlled audio receipts.
// Real persistence, decoding, rights and lifecycle are verified by backend tests.
export async function audioFixture(page: Page) {
  const env = await renderFixture(page);
  const scripts: ContentVersion[] = env.episodes.map((episode) => ({
    id: id(),
    episode_id: episode.id,
    version_no: 1,
    content_json: {},
    status: "APPROVED",
    run_id: null,
  }));
  const snapshots: AudioSnapshot[] = scripts.map((script) => ({
    id: id(),
    episode_id: script.episode_id,
    script_version_id: script.id,
    script_sha256: "a".repeat(64),
    line_count: 2,
    created_at: now,
  }));
  const lines: AudioLine[] = [
    {
      line_id: "line-one",
      scene_id: "scene-one",
      scene_no: 1,
      sequence_no: 0,
      speaker: "林舟",
      text: "我们沿着灯光走，天亮前就能回家。",
      line_type: "dialogue",
      emotion: "坚定",
    },
    {
      line_id: "line-two",
      scene_id: "scene-one",
      scene_no: 1,
      sequence_no: 1,
      speaker: "旁白",
      text: "远处的灯塔，终于又亮了起来。",
      line_type: "narration",
      emotion: "平静",
    },
  ];
  const voices: AudioVoice[] = lines.map((line) => ({
    id: id(),
    project_id: env.project.id,
    speaker: line.speaker,
    character_asset_id: null,
    version_no: 1,
    provider: "openai_tts",
    model: "gpt-4o-mini-tts",
    model_config_version: "voice-config:1",
    parameters: { voice: "alloy", speed: 1 },
    output_spec: { format: "wav" },
    provider_configuration: {
      maximum_cost_per_output: "1.00",
      pricing_version: "browser:1",
    },
    created_at: now,
  }));
  const histories = new Map<string, AudioHistory[]>();
  const tasks: AudioTask[] = [];
  const assets = new Map<string, AudioAsset>();
  const files: AudioFile[] = [];
  const media = recording();
  const reads: { path: string; authorization: string }[] = [];
  const mutations: { path: string; body: Record<string, unknown> }[] = [];
  const keys = new Map<string, AudioTask>();
  const importKeys = new Map<string, AudioImportResult>();
  const control = {
    readStatus: 0,
    taskStatus: "QUEUED",
    failTaskOnce: false,
    failImportOnce: false,
    failRegistration: false,
    corruptMedia: false,
    supported: ["voice", "speed", "instructions"],
    configured: true,
    gate: undefined as Promise<void> | undefined,
    mediaGate: undefined as Promise<void> | undefined,
    voiceConflict: false,
  };
  function audioFile(name = "灯塔录音.wav"): AudioFile {
    const row: AudioFile = {
      id: id(),
      project_id: env.project.id,
      asset_version_id: null,
      checksum: hash(media),
      byte_size: media.length,
      media_type: "audio/wav",
      original_name: name,
      source_note: "本人录制",
      rights_note: "本人授权本项目使用",
      rights_confirmed: true,
    };
    files.unshift(row);
    return row;
  }
  const sourceFile = audioFile();
  function asset(): AudioAsset {
    const row = {
      id: id(),
      project_id: env.project.id,
      asset_type: "AUDIO",
      status: "DRAFT",
      revision: 0,
      lock_version: 0,
      current_version_id: null,
    };
    assets.set(row.id, row);
    return row;
  }
  function addHistory(
    snapshot = snapshots[0],
    line = lines[0],
    target = asset(),
    source = sourceFile,
  ) {
    const rows = histories.get(`${snapshot.id}:${line.line_id}`) ?? [];
    const version: AssetVersion = {
      id: id(),
      asset_id: target.id,
      version_no: rows.length + 1,
      checksum: source.checksum,
      created_by: env.session.user.id,
      created_at: now,
      status: "DRAFT",
      stale: false,
      revision: 0,
      dependency_manifest_id: id(),
      archived_from_status: null,
      media: null,
      asset_snapshot: null,
      restored_from_version_id: null,
      restoration_note: null,
      provenance: null,
    };
    const row: AudioHistory = {
      version,
      binding: {
        id: id(),
        snapshot_id: snapshot.id,
        line_id: line.line_id,
        asset_version_id: version.id,
        dependency_manifest_id: version.dependency_manifest_id!,
        file_id: source.id,
        duration_ms: 1000,
      },
      tts_job_id: null,
      source_audio_version_id: null,
    };
    rows.unshift(row);
    histories.set(`${snapshot.id}:${line.line_id}`, rows);
    target.revision++;
    return row;
  }
  function addTask(
    status = "QUEUED",
    snapshot = snapshots[0],
    line = lines[0],
    target = asset(),
  ): AudioTask {
    const row = {
      job_id: id(),
      snapshot_id: snapshot.id,
      line_id: line.line_id,
      voice_version_id: voices[0].id,
      target_asset_id: target.id,
      status,
      revision: 19,
      error_code: null,
      asset_version_id: null,
      source_audio_version_id: null,
      created_at: now,
    };
    tasks.unshift(row);
    return row;
  }
  const validation = {
    media_type: "audio/wav",
    codec: "pcm_s16le",
    duration_ms: 1000,
    sample_rate: 8000,
    channels: 1,
    silence: {
      schema_version: "audio-silence/v1",
      threshold_dbfs: -50,
      threshold_pcm16: 104,
      silent: false,
      peak: 0.15,
      rms: 0.1,
      silent_sample_fraction: 0.01,
    },
  };
  const paginate = <T>(rows: T[], url: URL) =>
    rows.slice(
      Number(url.searchParams.get("offset") ?? 0),
      Number(url.searchParams.get("offset") ?? 0) +
        Number(url.searchParams.get("limit") ?? 30),
    );
  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({ status, json: { error: { code, message } } });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const scriptIndex = scripts.findIndex(
      (script) => path === `/api/v1/episodes/${script.episode_id}/scripts`,
    );
    if (scriptIndex >= 0 && method === "GET")
      return route.fulfill({ json: [scripts[scriptIndex]] });
    const audioPath =
      /\/(audio-snapshots|audio-voices|files|uploads)(\/|$)/.test(path) ||
      path.endsWith("/services/speech/configuration") ||
      [...assets.keys()].some((value) => path.includes(`/assets/${value}`)) ||
      path.endsWith("/dependency-impact");
    if (!audioPath) return route.fallback();
    if (method === "GET") {
      reads.push({
        path: request.url(),
        authorization: request.headers().authorization ?? "",
      });
      if (control.readStatus)
        return fail(
          route,
          control.readStatus,
          "FORBIDDEN",
          "当前来源权限已撤销。",
        );
    } else {
      const body = path.endsWith("/transfer")
        ? {}
        : (request.postDataJSON() ?? {});
      mutations.push({ path, body });
      if (env.control.role === "viewer" || env.control.archived)
        return fail(route, 403, "FORBIDDEN", "只读项目。");
    }
    if (path.endsWith("/services/speech/configuration"))
      return route.fulfill({
        json: {
          configured: control.configured,
          enabled: control.configured,
          message: control.configured ? "已配置" : "请先配置语音服务",
          model: "gpt-4o-mini-tts",
          supported_parameters: control.supported,
        },
      });
    if (path.endsWith("/audio-voices")) {
      if (method === "GET")
        return route.fulfill({
          json: paginate(
            voices.filter(
              (row) => row.speaker === url.searchParams.get("speaker"),
            ),
            url,
          ),
        });
      if (control.voiceConflict)
        return fail(
          route,
          409,
          "VERSION_CONFLICT",
          "音色已由其他成员更新，请刷新音色版本。",
        );
      const body = request.postDataJSON();
      const row = {
        ...voices[0],
        id: id(),
        speaker: body.speaker,
        parameters: body.parameters,
        version_no:
          Math.max(
            0,
            ...voices
              .filter((item) => item.speaker === body.speaker)
              .map((item) => item.version_no),
          ) + 1,
      };
      voices.unshift(row);
      return route.fulfill({ status: 201, json: row });
    }
    const snapshotEpisode = env.episodes.find(
      (row) => path === `/api/v1/episodes/${row.id}/audio-snapshots`,
    );
    if (snapshotEpisode) {
      if (method === "GET")
        return route.fulfill({
          json: paginate(
            snapshots.filter((row) => row.episode_id === snapshotEpisode.id),
            url,
          ),
        });
      const body = request.postDataJSON();
      const row = {
        ...snapshots[0],
        id: id(),
        episode_id: snapshotEpisode.id,
        script_version_id: body.script_version_id,
      };
      snapshots.unshift(row);
      return route.fulfill({ status: 201, json: row });
    }
    for (const snapshot of snapshots) {
      const root = `/api/v1/audio-snapshots/${snapshot.id}/lines`;
      if (path === root) return route.fulfill({ json: paginate(lines, url) });
      for (const line of lines) {
        const prefix = `${root}/${line.line_id}`;
        const history = histories.get(`${snapshot.id}:${line.line_id}`) ?? [];
        if (path === prefix + "/history")
          return route.fulfill({ json: paginate(history, url) });
        if (path === prefix + "/tts" && method === "GET")
          return route.fulfill({
            json: paginate(
              tasks.filter(
                (row) =>
                  row.snapshot_id === snapshot.id &&
                  row.line_id === line.line_id,
              ),
              url,
            ),
          });
        if (
          (path === prefix + "/tts" || path === prefix + "/tts/regenerate") &&
          method === "POST"
        ) {
          const body = request.postDataJSON();
          let row = keys.get(body.creation_key);
          if (!row) {
            const source = history.find(
              (item) => item.version.id === body.source_audio_version_id,
            );
            row = addTask(
              control.taskStatus,
              snapshot,
              line,
              source ? assets.get(source.version.asset_id)! : asset(),
            );
            row.voice_version_id = body.voice_version_id;
            row.source_audio_version_id = body.source_audio_version_id ?? null;
            keys.set(body.creation_key, row);
          }
          if (control.gate) await control.gate;
          if (control.failTaskOnce) {
            control.failTaskOnce = false;
            return route.abort("connectionfailed");
          }
          return route.fulfill({ status: 202, json: row });
        }
        const task = tasks.find(
          (row) =>
            row.snapshot_id === snapshot.id &&
            row.line_id === line.line_id &&
            (path === `${prefix}/tts/${row.job_id}` ||
              path === `${prefix}/tts/${row.job_id}/result`),
        );
        if (task) {
          if (method === "GET") return route.fulfill({ json: task });
          if (control.failRegistration)
            return fail(
              route,
              422,
              "INVALID_AUDIO",
              "音频机器检查未通过，未登记候选。",
            );
          const target = assets.get(task.target_asset_id)!;
          const added = addHistory(snapshot, line, target);
          added.tts_job_id = task.job_id;
          added.source_audio_version_id = task.source_audio_version_id;
          task.asset_version_id = added.version.id;
          const result: AudioResult = {
            job_id: task.job_id,
            attempt_id: id(),
            source_file_id: sourceFile.id,
            voice_version_id: task.voice_version_id,
            binding: added.binding,
            validation,
            asset_revision: target.revision,
          };
          return route.fulfill({ status: 201, json: result });
        }
        if (path === prefix + "/imports") {
          const body = request.postDataJSON();
          if (control.failImportOnce) {
            control.failImportOnce = false;
            return fail(
              route,
              409,
              "REVISION_CONFLICT",
              "资产版本已变化，请刷新后重试。",
            );
          }
          let result = importKeys.get(body.creation_key);
          if (!result) {
            const target = assets.get(body.target_asset_id) ?? asset();
            const added = addHistory(
              snapshot,
              line,
              target,
              files.find((file) => file.id === body.source_file_id)!,
            );
            result = {
              snapshot_id: snapshot.id,
              line_id: line.line_id,
              target_asset_id: target.id,
              asset_version_id: added.version.id,
              file_id: added.binding.file_id,
              source_file_id: body.source_file_id,
              dependency_manifest_id: added.binding.dependency_manifest_id,
              binding_id: added.binding.id,
              validation,
              asset_revision: target.revision,
              created_by: env.session.user.id,
              created_at: now,
            };
            importKeys.set(body.creation_key, result);
          }
          return route.fulfill({ status: 201, json: result });
        }
      }
    }
    if (path.endsWith("/files"))
      return route.fulfill({ json: paginate(files, url) });
    const file = files.find(
      (row) =>
        path === `/api/v1/projects/${env.project.id}/files/${row.id}` ||
        path === `/api/v1/projects/${env.project.id}/files/${row.id}/content`,
    );
    if (file) {
      if (!path.endsWith("/content")) return route.fulfill({ json: file });
      if (control.mediaGate) await control.mediaGate;
      const bytes = control.corruptMedia ? Buffer.from("corrupt") : media;
      return route.fulfill({ contentType: "audio/wav", body: bytes });
    }
    if (path.endsWith("/uploads"))
      return route.fulfill({
        status: 201,
        json: {
          upload_id: "recording-upload",
          method: "POST",
          body: "raw",
          headers: { "Content-Type": "audio/wav" },
          url: `/api/v1/projects/${env.project.id}/uploads/recording-upload/transfer`,
        },
      });
    if (path.endsWith("/transfer")) return route.fulfill({ status: 204 });
    if (path.endsWith("/recording-upload/complete")) {
      const row = audioFile("新上传录音.wav");
      const body = request.postDataJSON();
      Object.assign(row, body);
      return route.fulfill({ status: 201, json: row });
    }
    if (path.endsWith("/dependency-impact"))
      return route.fulfill({
        json: {
          digest: "audio-impact",
          summary: { REVIEW: 0, GENERATE: 0, RENDER: 1 },
          affected: [
            {
              asset_version_id: id(),
              asset_name: "已冻结成片",
              asset_type: "DELIVERY",
              version_no: 1,
              source_asset_version_id: url.searchParams.get("asset_version_id"),
              dependency_type: "RENDER_DEPENDENCY",
              depth: 1,
              action: "RENDER",
              stale: false,
            },
          ],
        },
      });
    for (const target of assets.values()) {
      const root = `/api/v1/projects/${env.project.id}/assets/${target.id}`;
      if (path === root) return route.fulfill({ json: target });
      const row = [...histories.values()]
        .flat()
        .find((item) => path.startsWith(`${root}/versions/${item.version.id}`));
      if (!row) continue;
      if (method === "GET") return route.fulfill({ json: row.version });
      const body = request.postDataJSON();
      if (path.endsWith("/state")) {
        row.version.status = body.status;
        row.version.revision++;
        return route.fulfill({ json: row.version });
      }
      if (path.endsWith("/select")) {
        target.current_version_id = row.version.id;
        target.status = "APPROVED";
        target.revision++;
      }
      if (path.endsWith("/lock")) {
        target.status = "LOCKED";
        target.revision++;
        target.lock_version++;
      }
      if (path.endsWith("/unlock")) {
        target.status = "APPROVED";
        target.revision++;
        target.lock_version++;
      }
      return route.fulfill({ json: target });
    }
    return fail(route, 404, "NOT_FOUND", "没有匹配的音频来源。");
  });
  async function open(index = 0) {
    await page.goto(
      `/projects/${env.project.id}/episodes/${env.episodes[index].id}?view=audio`,
    );
    await closeDirectory(page);
    const panel = page.getByRole("region", { name: "配音工作区", exact: true });
    await expect(panel).toBeVisible();
    await expect(
      panel.getByLabel("已保存的配音来源", { exact: true }),
    ).toBeEnabled();
    await expect(
      panel.getByRole("heading", { name: "林舟", exact: true }),
    ).toBeVisible();
    return panel;
  }
  return {
    ...env,
    scripts,
    snapshots,
    lines,
    voices,
    histories,
    tasks,
    assets,
    files,
    sourceFile,
    controlAudio: control,
    audioReads: reads,
    audioMutations: mutations,
    addHistory,
    addTask,
    media,
    open,
  };
}
