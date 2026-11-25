"use client";

import { useCallback, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "@/features/rendering/hooks/use-render-mutation";
import { audioApi } from "../api";
import type {
  AudioHistory,
  AudioLine,
  AudioSnapshot,
  AudioTask,
  AudioVoice,
} from "../types";
import useAudioPage from "../use-audio-page";
import { AudioFeedback, AudioPagination } from "./audio-fields";
import AudioImport from "./audio-import";
import AudioPlayer from "./audio-player";
import AudioTaskDetail from "./audio-task";
import AudioVersionActions from "./audio-version-actions";
import VoicePicker from "./voice-picker";
import VoiceProfileEditor from "./voice-profile-editor";

const historyId = (row: AudioHistory) => row.binding.id;
const jobId = (row: AudioTask) => row.job_id;
export default function AudioLinePanel({
  projectId,
  snapshot,
  line,
  scope,
  owner,
  writable,
  unavailable,
  onOpenRender,
}: {
  projectId: string;
  snapshot: AudioSnapshot;
  line: AudioLine;
  scope: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  onOpenRender: () => void;
}) {
  const [voiceDraft, setVoice] = useState<AudioVoice | null>(null);
  const [editProfile, setEditProfile] = useState(false);
  const [speedDraft, setSpeed] = useState("");
  const [instructionsDraft, setInstructions] = useState("");
  const fetchProfile = useCallback(
    (signal: AbortSignal) =>
      audioApi.lineVoiceProfile(snapshot.id, line.line_id, signal),
    [snapshot.id, line.line_id],
  );
  const profile = useRenderDetail(scope, fetchProfile, unavailable);
  const ready = profile.data;
  const voice = ready?.profile ? ready.voice : voiceDraft;
  const performance = ready?.selection?.performance;
  const expressive =
    performance &&
    (performance.emotion ||
      performance.purpose ||
      performance.interactions.length > 0);
  const performanceUnavailable =
    expressive && !ready.supported_parameters.includes("instructions");
  const fetchHistory = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.history(snapshot.id, line.line_id, offset, signal),
    [snapshot.id, line.line_id],
  );
  const history = useAudioPage(scope, fetchHistory, historyId, unavailable);
  const fetchTasks = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.tasks(snapshot.id, line.line_id, offset, signal),
    [snapshot.id, line.line_id],
  );
  const tasks = useAudioPage(scope, fetchTasks, jobId, unavailable);
  const [versionId, setVersionId] = useState("");
  const [selectedJob, setSelectedJob] = useState("");
  const selected =
    history.rows.find((row) => row.version.id === versionId) ?? history.rows[0];
  const assetId = selected?.version.asset_id ?? "";
  const fetchAsset = useCallback(
    (signal: AbortSignal) => audioApi.asset(projectId, assetId, signal),
    [projectId, assetId],
  );
  const asset = useRenderDetail(
    `${scope}:${assetId}`,
    fetchAsset,
    unavailable,
    !!assetId,
  );
  const fetchVersion = useCallback(
    (signal: AbortSignal) =>
      audioApi.version(projectId, assetId, selected?.version.id ?? "", signal),
    [projectId, assetId, selected?.version.id],
  );
  const version = useRenderDetail(
    `${scope}:${selected?.version.id ?? ""}`,
    fetchVersion,
    unavailable,
    !!selected,
  );
  const reusing = !!selected && selected.binding.snapshot_id !== snapshot.id;
  const activeJobId = selectedJob || tasks.rows[0]?.job_id;
  const currentAsset = asset.data?.id === assetId ? asset.data : null;
  const currentVersion =
    version.data?.id === selected?.version.id &&
    version.data?.asset_id === assetId
      ? version.data
      : null;
  const locked = currentAsset?.status === "LOCKED";
  const replacingLocked = !!(
    ready?.selection &&
    locked &&
    currentVersion?.stale
  );
  const rerunSource = reusing || replacingLocked ? undefined : selected;
  const lockedSource = locked && !!rerunSource;
  const supported = ready?.supported_parameters ?? [];
  const speedRange = ready?.parameter_ranges.speed;
  const speed = speedDraft === "" ? undefined : Number(speedDraft);
  const invalidSpeed =
    speedRange !== undefined &&
    speed !== undefined &&
    (!Number.isFinite(speed) ||
      speed < speedRange.minimum ||
      speed > speedRange.maximum);
  const instructions = instructionsDraft.trim();
  const performanceParameters = {
    ...(speedRange && speed !== undefined ? { speed } : {}),
    ...(supported.includes("instructions") && instructions
      ? { instructions }
      : {}),
  };
  const generationScope = `${scope}:tts:${ready?.profile?.id ?? ""}:${voice?.id ?? ""}:${rerunSource?.version.id ?? "new"}`;
  const mutation = useRenderMutation(generationScope, writable, unavailable);
  const lineagePending =
    !!selected &&
    (!currentAsset || !currentVersion || asset.loading || version.loading);
  const unknown = tasks.rows.some((row) =>
    ["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"].includes(row.status),
  );
  const reloadHistory = useCallback(() => {
    setVersionId("");
    void history.load();
    void tasks.load();
    if (assetId) void asset.load();
  }, [history.load, tasks.load, asset.load, assetId]);
  async function generate() {
    if (
      !voice ||
      !ready ||
      performanceUnavailable ||
      invalidSpeed ||
      lockedSource ||
      lineagePending ||
      unknown ||
      history.loading ||
      history.error ||
      tasks.error
    )
      return;
    const pending = pendingCreationKey(
      `${generationScope}:${JSON.stringify(performanceParameters)}`,
    );
    await mutation.mutate(
      (signal) =>
        audioApi.createTask(
          snapshot.id,
          line.line_id,
          {
            creation_key: pending.key,
            voice_version_id: voice.id,
            ...(Object.keys(performanceParameters).length
              ? { performance_parameters: performanceParameters }
              : {}),
            ...(ready.profile
              ? { voice_profile_version_id: ready.profile.id }
              : {}),
            ...(rerunSource && currentAsset
              ? {
                  source_audio_version_id: rerunSource.version.id,
                  expected_revision: currentAsset.revision,
                }
              : {}),
          },
          !!rerunSource,
          signal,
        ),
      (row) => {
        pending.clear();
        tasks.prepend(row);
        setSelectedJob(row.job_id);
      },
      "本句配音任务已创建。生成结果须检查、登记并审核后才能选用。",
    );
  }
  return (
    <section aria-label="台词配音详情" className="flex min-w-0 flex-col gap-6">
      <header className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">
          场景 {line.scene_no} ·{" "}
          {line.line_type === "narration" ? "旁白" : "对白"}
        </p>
        <h3 className="text-xl font-semibold">{line.speaker}</h3>
        <p className="whitespace-pre-wrap break-words text-base leading-relaxed">
          {line.text}
        </p>
        {line.emotion && (
          <p className="text-sm text-muted-foreground">
            剧本情绪：{line.emotion}
          </p>
        )}
      </header>
      <VoicePicker
        projectId={projectId}
        speaker={line.speaker}
        characterId={ready?.character_asset_id ?? null}
        profiled={!!ready?.profile}
        scope={scope + ":voices"}
        writable={writable}
        selected={voiceDraft}
        onSelected={setVoice}
        unavailable={unavailable}
      />
      <AudioFeedback error={profile.error} />
      {ready?.selection && voice && (
        <section aria-label="本句声音档案" className="grid gap-1 text-sm">
          <p>
            声音档案 v{ready.profile?.version_no} · 音色 v{voice.version_no} ·
            故事时点 {ready.selection.story_time}
          </p>
          {ready.selection.change_reason && (
            <p>剧情变声 · {ready.selection.change_reason}</p>
          )}
          {performance?.purpose && <p>场景目的 · {performance.purpose}</p>}
          {performance?.emotion_start && performance.emotion_end && (
            <p>
              情绪变化 · {performance.emotion_start} → {performance.emotion_end}
            </p>
          )}
          {performance?.interactions.map((action, index) => (
            <p key={index}>{action}</p>
          ))}
        </section>
      )}
      {ready?.unavailable_reason && <Notice>{ready.unavailable_reason}</Notice>}
      {performanceUnavailable && (
        <Notice>
          当前音色协议缺少情绪说明，请改用支持的音色或导入表演录音。
        </Notice>
      )}
      {writable && ready?.character_asset_id && (
        <div className="flex min-w-0 flex-col gap-3">
          <Button
            className="self-start"
            type="button"
            variant="outline"
            aria-expanded={editProfile}
            onClick={() => setEditProfile((open) => !open)}
          >
            {editProfile ? "关闭声音档案编辑" : "编辑跨集声音档案"}
          </Button>
          {editProfile && (
            <VoiceProfileEditor
              key={ready.profile?.id ?? "initial"}
              projectId={projectId}
              characterId={ready.character_asset_id}
              profile={ready.profile}
              selected={voiceDraft}
              unavailable={unavailable}
              onSaved={() => {
                void profile.load();
                void history.load();
                if (selected) void version.load();
              }}
            />
          )}
        </div>
      )}
      {writable && (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            生成只作用于本句，可能产生供应商费用。重生成另存候选，旧配音、字幕来源和已冻结成片保留。
          </p>
          {ready?.selection && speedRange && (
            <Field>
              <FieldLabel htmlFor="audio-line-speed">本句语速</FieldLabel>
              <Input
                id="audio-line-speed"
                type="number"
                min={speedRange.minimum}
                max={speedRange.maximum}
                step="any"
                value={speedDraft}
                placeholder={`沿用音色语速 ${voice?.parameters.speed ?? 1}`}
                disabled={mutation.busy}
                onChange={(event) => setSpeed(event.target.value)}
              />
              {invalidSpeed && (
                <Notice>
                  语速须在 {speedRange.minimum} 到 {speedRange.maximum} 之间。
                </Notice>
              )}
            </Field>
          )}
          {ready?.selection && supported.includes("instructions") && (
            <Field>
              <FieldLabel htmlFor="audio-line-instructions">
                补充本句表演说明
              </FieldLabel>
              <Textarea
                id="audio-line-instructions"
                maxLength={1000}
                rows={2}
                value={instructionsDraft}
                disabled={mutation.busy}
                onChange={(event) => setInstructions(event.target.value)}
              />
            </Field>
          )}
          {unknown && (
            <Notice>
              本句仍有结果待核清的任务，请先在任务中心完成对账，再决定是否重新生成。
            </Notice>
          )}
          <Button
            disabled={
              mutation.busy ||
              !voice ||
              !ready ||
              !!performanceUnavailable ||
              invalidSpeed ||
              lockedSource ||
              lineagePending ||
              unknown ||
              history.loading ||
              !!history.error ||
              tasks.loading ||
              !!tasks.error
            }
            onClick={() => void generate()}
          >
            {mutation.busy
              ? "正在创建任务…"
              : rerunSource
                ? "仅重生成本句配音"
                : "生成本句配音"}
          </Button>
          <AudioFeedback {...mutation} />
        </div>
      )}
      <section
        aria-label="本句音频历史"
        className="flex min-w-0 flex-col gap-3 border-t pt-5"
      >
        <h4 className="font-semibold">音频历史</h4>
        <p className="text-xs text-muted-foreground">
          按登记时间从新到旧排列。试听和比较候选后，再明确选用。
        </p>
        {history.rows.length > 0 && (
          <Field>
            <FieldLabel htmlFor="audio-history-version">
              本句音频版本
            </FieldLabel>
            <ChoiceSelect
              id="audio-history-version"
              value={selected?.version.id ?? ""}
              placeholder="选择音频版本"
              disabled={history.loading || mutation.busy}
              options={history.rows.map((row) => ({
                value: row.version.id,
                label: `v${row.version.version_no} · ${row.binding.snapshot_id !== snapshot.id ? "复用录音" : row.tts_job_id ? "生成配音" : "导入录音"} · ${row.binding.duration_ms} 毫秒`,
              }))}
              onValueChange={setVersionId}
            />
          </Field>
        )}
        <AudioPagination list={history} label="音频历史" />
        {!history.loading && !history.error && history.rows.length === 0 && (
          <Notice>
            本句尚无已登记音频。可以生成配音或导入录音；保留无声须到渲染清单逐句确认。
          </Notice>
        )}
        <AudioFeedback error={asset.error || version.error} />
        {selected && (
          <>
            <AudioPlayer
              key={`${scope}:${selected.binding.id}`}
              projectId={projectId}
              fileId={selected.binding.file_id}
              unavailable={unavailable}
            />
            {reusing && (
              <Notice>该台词及表演要求保持一致，可直接复用已批准录音。</Notice>
            )}
            {currentAsset && currentVersion && !reusing && (
              <AudioVersionActions
                key={`${currentVersion.id}:${currentVersion.revision}:${currentAsset.revision}`}
                projectId={projectId}
                asset={currentAsset}
                version={currentVersion}
                scope={`${scope}:version:${currentVersion.id}`}
                owner={owner}
                writable={writable}
                onSaved={() => {
                  void asset.load();
                  void version.load();
                  void history.load();
                }}
                unavailable={unavailable}
              />
            )}
          </>
        )}
      </section>
      <section
        aria-label="本句配音任务列表"
        className="flex min-w-0 flex-col gap-3 border-t pt-5"
      >
        <h4 className="font-semibold">配音任务</h4>
        {tasks.rows.length > 0 && (
          <Field>
            <FieldLabel htmlFor="audio-task-choice">本句任务</FieldLabel>
            <ChoiceSelect
              id="audio-task-choice"
              value={activeJobId ?? ""}
              disabled={tasks.loading || mutation.busy}
              options={tasks.rows.map((row, index) => ({
                value: row.job_id,
                label: `${index + 1}. ${row.created_at} · ${row.asset_version_id ? "已登记" : "尚未登记"}`,
              }))}
              onValueChange={setSelectedJob}
            />
          </Field>
        )}
        <AudioPagination list={tasks} label="本句任务" />
        {activeJobId && (
          <AudioTaskDetail
            key={`${scope}:${activeJobId}`}
            projectId={projectId}
            episodeId={snapshot.episode_id}
            snapshotId={snapshot.id}
            lineId={line.line_id}
            jobId={activeJobId}
            scope={`${scope}:task:${activeJobId}`}
            writable={writable}
            onRegistered={reloadHistory}
            onUpdated={tasks.replace}
            unavailable={unavailable}
          />
        )}
      </section>
      {writable && !history.loading && !history.error && !lineagePending && (
        <AudioImport
          key={`${scope}:import:${currentAsset?.id ?? "new"}`}
          projectId={projectId}
          snapshotId={snapshot.id}
          lineId={line.line_id}
          scope={scope}
          asset={reusing ? null : currentAsset}
          writable={writable && (!locked || reusing)}
          onImported={reloadHistory}
          unavailable={unavailable}
        />
      )}
      <section
        aria-label="静音与字幕处理"
        className="flex min-w-0 flex-col gap-3 border-t pt-5"
      >
        <h4 className="font-semibold">本句保留无声</h4>
        <p className="text-sm text-muted-foreground">
          静音与仅字幕决定在渲染清单中逐句确认，必须填写原因。请选择对应剧本来源的时间轴；决定随精确清单保存。没有配音不会自动视为已静音。
        </p>
        <Button variant="outline" onClick={onOpenRender}>
          打开渲染清单逐句确认
        </Button>
      </section>
    </section>
  );
}
