import { expect, test } from "@playwright/test";
import type { LipSyncCue, LipSyncRequest } from "../src/features/video/types";
import { mockedVideoFixture } from "./video-candidates-helpers";

test("VIDEO-04：校正表单提交精确配音和参考帧，原候选不自动选用", async ({
  page,
}) => {
  const env = await mockedVideoFixture(page);
  env.first.lip_sync_required = true;
  env.first.actions!.can_lip_sync = true;
  env.first.admission!.blocked_code = "LIP_SYNC_REQUIRED";
  const cue: LipSyncCue = {
    line_id: "line-01",
    audio_version_id: crypto.randomUUID(),
    start_ms: 400,
    closed_frame_ms: 0,
    open_frame_ms: 125,
    mouth: { x: 3750, y: 5235, width: 2500, height: 1875 },
  };
  let saved: LipSyncRequest | undefined;
  await page.route(
    `**/generation-candidates/${env.first.id}/lip-sync`,
    async (route) => {
      if (route.request().method() === "GET")
        return route.fulfill({
          json: {
            candidate_id: env.first.id,
            requires_video_rights_confirmation: true,
            dialogue: [
              {
                line_id: cue.line_id,
                speaker: "林",
                text: "等一下。",
                presentation: "visible",
                lipsync: "required",
                exemption_reason: "",
                sources: [
                  {
                    audio_version_id: cue.audio_version_id,
                    duration_ms: 800,
                    name: "精确配音.wav",
                  },
                ],
              },
              {
                line_id: "line-02",
                speaker: "旁白",
                text: "风停了。",
                presentation: "voice_over",
                lipsync: "not_applicable",
                exemption_reason: "旁白不出镜。",
                sources: [],
              },
            ],
          },
        });
      saved = route.request().postDataJSON() as LipSyncRequest;
      return route.fulfill({
        status: 201,
        json: {
          ...env.second,
          source: "REPLACEMENT",
          replaces_candidate_id: env.first.id,
          lip_sync_required: true,
          lip_sync: {
            engine: "local-mouth-pose/1",
            source_candidate_id: env.first.id,
            source_video_version_id: env.first.asset_version_id,
            source_video_sha256: env.first.version!.checksum,
            script_version_id: crypto.randomUUID(),
            output_sha256: env.second.version!.checksum,
            lines: [
              {
                ...cue,
                audio_file_id: crypto.randomUUID(),
                audio_sha256: "c".repeat(64),
                duration_ms: 800,
              },
            ],
          },
        },
      });
    },
  );
  await env.openCanvas(env.videoNodes[0]);
  const detail = page.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  const sync = detail.getByRole("region", {
    name: "可见对白口型校正",
    exact: true,
  });
  await sync.getByRole("button", { name: "设置口型校正", exact: true }).click();
  await expect(sync.getByLabel("精确配音版本", { exact: true })).toHaveValue(
    cue.audio_version_id,
  );
  await expect(sync).toContainText("旁白 · 旁白：旁白不出镜。");
  await sync.getByLabel("台词开始（ms）", { exact: true }).fill("400");
  await sync.getByLabel("闭嘴帧（ms）", { exact: true }).fill("0");
  await sync.getByLabel("张嘴帧（ms）", { exact: true }).fill("125");
  await sync.getByLabel("口部左边", { exact: true }).fill("3750");
  await sync.getByLabel("口部上边", { exact: true }).fill("5235");
  await sync.getByLabel("口部宽度", { exact: true }).fill("2500");
  await sync.getByLabel("口部高度", { exact: true }).fill("1875");
  await sync
    .getByLabel("原候选可作口型衍生的使用权声明", { exact: true })
    .fill("原创动画，允许局部校正。");
  await sync
    .getByRole("button", { name: "生成并另存口型候选", exact: true })
    .click();
  await expect(detail).toContainText("请播放检查后重新送审；原候选未改动。");
  expect(saved).toEqual({
    expected_revision: env.asset.revision,
    creation_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
    source_rights_note: "原创动画，允许局部校正。",
    cues: [cue],
  });
  await expect(detail).toContainText(env.first.id);
  await expect(
    detail.getByRole("button", { name: "提交视频版本审核", exact: true }),
  ).toBeDisabled();
});
