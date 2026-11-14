"use client";

import { useCallback, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
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
  const [voice, setVoice] = useState<AudioVoice | null>(null);
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
  const selected = versionId
    ? history.rows.find((row) => row.version.id === versionId)
    : history.rows[0];
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
  const generationScope = `${scope}:tts:${voice?.id ?? ""}:${selected?.version.id ?? "new"}`;
  const mutation = useRenderMutation(generationScope, writable, unavailable);
  const activeJobId = selectedJob || tasks.rows[0]?.job_id;
  const currentAsset = asset.data?.id === assetId ? asset.data : null;
  const currentVersion =
    version.data?.id === selected?.version.id &&
    version.data?.asset_id === assetId
      ? version.data
      : null;
  const locked = currentAsset?.status === "LOCKED";
  const lineagePending =
    !!selected &&
    (!currentAsset || !currentVersion || asset.loading || version.loading);
  const unknown = tasks.rows.some((row) =>
    ["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"].includes(row.status),
  );
  const voicePriced =
    voice?.provider_configuration.maximum_cost_per_output !== null &&
    typeof voice?.provider_configuration.pricing_version === "string" &&
    !!voice.provider_configuration.pricing_version;
  const reloadHistory = useCallback(() => {
    setVersionId("");
    void history.load();
    void tasks.load();
    if (assetId) void asset.load();
  }, [history.load, tasks.load, asset.load, assetId]);
  async function generate() {
    if (
      !voice ||
      locked ||
      lineagePending ||
      unknown ||
      !voicePriced ||
      history.loading ||
      history.error ||
      tasks.error
    )
      return;
    const pending = pendingCreationKey(generationScope);
    await mutation.mutate(
      (signal) =>
        audioApi.createTask(
          snapshot.id,
          line.line_id,
          {
            creation_key: pending.key,
            voice_version_id: voice.id,
            ...(selected && currentAsset
              ? {
                  source_audio_version_id: selected.version.id,
                  expected_revision: currentAsset.revision,
                }
              : {}),
          },
          !!selected,
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
        scope={scope + ":voices"}
        writable={writable}
        selected={voice}
        onSelected={setVoice}
        unavailable={unavailable}
      />
      {writable && (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted-foreground">
            生成只作用于本句，可能产生供应商费用，沿用项目现有报价与预算。重生成另存候选，旧配音、字幕来源和已冻结成片保留。
          </p>
          {voice && !voicePriced && (
            <Notice>
              此音色的保存配置缺少可信报价，请更新语音服务报价后另存音色版本。
            </Notice>
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
              !voicePriced ||
              locked ||
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
              : selected
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
                label: `v${row.version.version_no} · ${row.tts_job_id ? "生成配音" : "导入录音"} · ${row.binding.duration_ms} 毫秒`,
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
            <details className="text-xs text-muted-foreground">
              <summary>查看本句精确来源</summary>
              <dl className="mt-2 grid gap-2 break-all">
                <div>
                  <dt>台词 ID</dt>
                  <dd>{line.line_id}</dd>
                </div>
                <div>
                  <dt>剧本版本</dt>
                  <dd>{snapshot.script_version_id}</dd>
                </div>
                <div>
                  <dt>音频版本</dt>
                  <dd>{selected.version.id}</dd>
                </div>
                <div>
                  <dt>音频文件</dt>
                  <dd>{selected.binding.file_id}</dd>
                </div>
                <div>
                  <dt>依赖清单</dt>
                  <dd>{selected.binding.dependency_manifest_id}</dd>
                </div>
                {selected.source_audio_version_id && (
                  <div>
                    <dt>重生成来源音频</dt>
                    <dd>{selected.source_audio_version_id}</dd>
                  </div>
                )}
              </dl>
            </details>
            {currentAsset && currentVersion && (
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
          asset={currentAsset}
          writable={writable && !locked}
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
