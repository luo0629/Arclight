"use client";

import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Notice, StatusBadge } from "@/components/ui-kit";
import { useAuth } from "@/features/auth/auth-context";
import ImpactButton from "@/features/dependencies/components/impact-button";
import { api, json } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import { candidatePath, uploadVideo } from "../api";
import {
  readVideoRestoration,
  restoreVideoHistory,
  type VideoRestoration,
} from "../history";
import type { VideoCandidate } from "../types";
import useVideoCandidate from "../hooks/use-video-candidate";
import VideoLipSyncPanel from "./video-lip-sync";
import VideoTemporalReview from "./video-temporal-review";

type Replacement = {
  file_id: string;
  creation_key: string;
  expected_revision: number;
};
export default function VideoCandidateDetail({
  projectId,
  candidateId,
  expectedStoryboardId,
  expectedShotId,
  expectedVersionId,
  expectedChecksum,
  selectedForShot,
  onChanged,
  onCreated,
  unavailable,
}: {
  projectId: string;
  candidateId: string;
  expectedStoryboardId?: string;
  expectedShotId?: string;
  expectedVersionId?: string;
  expectedChecksum?: string;
  selectedForShot?: boolean;
  onChanged: () => Promise<void>;
  onCreated?: (id: string) => Promise<void>;
  unavailable: (cause: unknown) => void;
}) {
  const { user } = useAuth();
  const state = useVideoCandidate(projectId, candidateId, unavailable);
  const { data, asset } = state;
  const id = useId();
  const [comment, setComment] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [rights, setRights] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [storageError, setStorageError] = useState("");
  const [pending, setPending] = useState<Replacement | null>(null);
  const [restorePending, setRestorePending] = useState<VideoRestoration | null>(
    null,
  );
  const storageKey =
    "arclight:video-replacement:v1:" +
    user.id +
    ":" +
    projectId +
    ":" +
    candidateId;
  useEffect(() => {
    try {
      setRestorePending(readVideoRestoration(storageKey + ":restore"));
      const saved = sessionStorage.getItem(storageKey);
      if (!saved) return;
      const row = JSON.parse(saved) as Replacement;
      if (
        !isResourceId(row.file_id) ||
        !isResourceId(row.creation_key) ||
        !Number.isSafeInteger(row.expected_revision)
      )
        throw new Error(
          "替换提交记录无效，请核对服务器候选后清除此标签页记录。",
        );
      setPending(row);
    } catch (cause) {
      setStorageError(
        cause instanceof Error ? cause.message : "无法读取替换提交记录。",
      );
    }
  }, [storageKey]);
  const version = data?.version;
  const origin = data?.video_source;
  const mismatch =
    !!data &&
    ((expectedStoryboardId !== undefined &&
      origin?.storyboard_id !== expectedStoryboardId) ||
      (expectedShotId !== undefined && origin?.shot_id !== expectedShotId) ||
      (expectedVersionId !== undefined && version?.id !== expectedVersionId) ||
      (expectedChecksum !== undefined &&
        version?.checksum !== expectedChecksum));
  const disabled =
    state.busy || state.loading || !state.fresh || mismatch || !!storageError;
  const endpoint = version
    ? "/projects/" +
      projectId +
      "/assets/" +
      version.asset_id +
      "/versions/" +
      version.id
    : "";
  async function change(status: string, impact_digest?: string) {
    if (!version || disabled) return false;
    const result = await state.perform(
      (signal) =>
        api(endpoint + "/state", {
          ...json("PATCH", {
            expected_revision: version.revision,
            status,
            comment: comment.trim(),
            impact_digest,
          }),
          signal,
        }),
      "版本状态已保存，历史视频保持原样。",
    );
    if (result) await onChanged();
    return !!result;
  }
  async function select(impact_digest?: string) {
    if (!version || !asset || disabled) return false;
    const result = await state.perform(
      (signal) =>
        api(endpoint + "/select", {
          ...json("POST", {
            expected_revision: asset.revision,
            expected_lock_version: asset.lock_version,
            impact_digest,
          }),
          signal,
        }),
      "已正式选用此精确视频版本。",
    );
    if (result) await onChanged();
    return !!result;
  }
  async function lock() {
    if (!asset || disabled) return;
    const action = asset.status === "LOCKED" ? "unlock" : "lock";
    const result = await state.perform(
      (signal) =>
        api(endpoint + "/" + action, {
          ...json("POST", {
            expected_revision: asset.revision,
            expected_lock_version: asset.lock_version,
          }),
          signal,
        }),
      action === "lock" ? "当前视频选用已锁定。" : "当前视频选用已解锁。",
    );
    if (result) await onChanged();
  }
  async function restore() {
    if (!asset || !version || (!restorePending && !comment.trim()) || disabled)
      return;
    const saved = await state.perform(
      (signal) =>
        restoreVideoHistory({
          endpoint,
          storageKey: storageKey + ":restore",
          version,
          assetRevision: asset.revision,
          comment,
          signal,
          changed: setRestorePending,
        }),
      "历史视频已另存新候选，仍需重新审查和选用。",
    );
    if (saved) await onChanged();
  }
  async function replace() {
    if (
      !data ||
      !asset ||
      disabled ||
      (!pending && (!file || !source.trim() || !rights.trim() || !confirmed))
    )
      return;
    const saved = await state.perform(async (signal) => {
      let request = pending;
      if (!request) {
        if (!file) throw new Error("请选择替换视频。");
        const uploaded = await uploadVideo(
          projectId,
          file,
          source,
          rights,
          signal,
        );
        request = {
          file_id: uploaded.id,
          creation_key: crypto.randomUUID(),
          expected_revision: asset.revision,
        };
        sessionStorage.setItem(storageKey, JSON.stringify(request));
        setPending(request);
      }
      const row = await api<VideoCandidate>(
        candidatePath(projectId, candidateId) + "/replacements",
        {
          ...json("POST", request),
          signal,
        },
      );
      if (
        row.project_id !== projectId ||
        row.media_kind !== "VIDEO" ||
        row.replaces_candidate_id !== candidateId ||
        row.source_file_id !== request.file_id ||
        row.version?.asset_id !== asset.id ||
        row.version.id !== row.asset_version_id
      )
        throw new Error("替换视频回执与原候选或上传文件不一致，请核验原提交。");
      sessionStorage.removeItem(storageKey);
      setPending(null);
      return row;
    }, "替换视频已保存为新候选，原版本和审核历史保持原样。");
    if (saved) {
      if (onCreated) await onCreated(saved.id);
      else await onChanged();
    }
  }
  return (
    <section
      aria-label="精确视频候选详情"
      className="flex min-w-0 flex-col gap-4 rounded-lg border p-3"
    >
      {state.loading && <p role="status">正在读取精确视频候选…</p>}
      {state.error && (
        <Notice role="alert" tone="error">
          {state.error}
        </Notice>
      )}
      {state.notice && <Notice role="status">{state.notice}</Notice>}
      {storageError && (
        <Notice role="alert" tone="error">
          {storageError}
        </Notice>
      )}
      <Button
        variant="outline"
        disabled={state.busy || state.loading}
        onClick={() => void state.load()}
      >
        刷新视频候选
      </Button>
      {mismatch && (
        <Notice role="alert" tone="error">
          视频来源与当前精确分镜、镜头或版本不一致，已停止读取和操作。
        </Notice>
      )}
      {data && !mismatch && (
        <>
          <h4>
            视频 v{version?.version_no ?? "—"} ·{" "}
            <StatusBadge status={version?.status ?? "DRAFT"} />
          </h4>
          <dl className="grid min-w-0 gap-1 break-words text-xs">
            <dt>来源</dt>
            <dd>
              {version?.restored_from_version_id
                ? "历史视频另存"
                : data.lip_sync
                  ? "本机口型校正"
                  : data.source === "GENERATED"
                    ? "原生成结果"
                    : "人工上传替换"}
            </dd>
            {version?.media && (
              <>
                <dt>原始文件 / 来源声明</dt>
                <dd>
                  {version.media.original_name} · {version.media.source_note}
                </dd>
                <dt>使用权声明</dt>
                <dd>
                  {version.media.rights_note} ·{" "}
                  {version.media.rights_confirmed ? "已确认" : "待确认"}
                </dd>
              </>
            )}
            <dt>选用状态</dt>
            <dd>
              {version && asset?.current_version_id === version.id
                ? selectedForShot === false
                  ? "已登记选用，当前证据已失效"
                  : "已选用此版本"
                : asset?.current_version_id
                  ? "已选用其他版本"
                  : "尚未选用"}
            </dd>
            {version?.restoration_note && (
              <>
                <dt>另存说明</dt>
                <dd>{version.restoration_note}</dd>
              </>
            )}
          </dl>
          {data.production && (
            <section
              aria-label="视频生成配置"
              className="flex flex-col gap-1 break-all text-xs"
            >
              <h5 className="font-semibold">
                {data.source === "GENERATED" &&
                !version?.restored_from_version_id
                  ? "冻结生成配置"
                  : "原生成任务配置"}
              </h5>
              {(data.source !== "GENERATED" ||
                version?.restored_from_version_id) && (
                <p>
                  此处保留原任务来源；当前文件来源以上方上传或历史另存声明为准。
                </p>
              )}
              <p>
                服务 / 模型：{data.production.provider} /{" "}
                {data.production.model} · 配置{" "}
                {data.production.model_config_version}
              </p>
              <p>提示词版本：{data.production.prompt_version}</p>
              <p className="whitespace-pre-wrap">
                提示词：{data.production.prompt}
              </p>
              {data.production.negative_prompt && (
                <p className="whitespace-pre-wrap">
                  负面提示词：{data.production.negative_prompt}
                </p>
              )}
            </section>
          )}
          {!!origin?.video_images.length && (
            <p className="text-xs">
              已固定 {origin.video_images.length} 张首帧／参考图
            </p>
          )}
          {data.media_check?.video ? (
            <section aria-label="视频技术检查" className="break-all text-xs">
              <h5 className="font-semibold">
                媒体检查 · {data.media_check.status}
              </h5>
              <p>
                {data.media_check.video.codec} ·{" "}
                {data.media_check.video.media_type} ·{" "}
                {data.media_check.video.width}×{data.media_check.video.height} ·{" "}
                {data.media_check.video.frame_rate} fps
              </p>
              <p>
                实测 {data.media_check.video.duration_ms} ms ·{" "}
                {data.media_check.video.byte_size} 字节 · 完整解码{" "}
                {data.media_check.video.decoded_frames} 帧 ·{" "}
                {data.media_check.video.has_audio ? "包含原声" : "无原声"}
              </p>
              <p>
                完整解码：
                {data.media_check.video.full_decode_verified
                  ? "已验证"
                  : "未验证"}{" "}
                · 精确参考：
                {data.media_check.references_valid ? "有效" : "无效"}
              </p>
            </section>
          ) : (
            <Notice>缺少完整视频媒体检查，不能作为可用镜头选用。</Notice>
          )}
          <VideoTemporalReview
            key={data.id}
            candidate={data}
            disabled={disabled}
            state={state}
            onChanged={onChanged}
          />
          <VideoLipSyncPanel
            key={data.id + ":" + version?.checksum}
            candidate={data}
            state={state}
            disabled={disabled}
            unavailable={unavailable}
            onChanged={onChanged}
            onCreated={onCreated}
          />
          <label htmlFor={id + "-comment"} className="text-sm">
            视频版本说明
          </label>
          <Textarea
            id={id + "-comment"}
            value={comment}
            maxLength={1000}
            disabled={disabled}
            onChange={(event) => setComment(event.target.value)}
          />
          {restorePending && (
            <Notice>
              历史另存提交待核对：{restorePending.comment}
              。继续沿用原精确输入、说明和提交标识。
            </Notice>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={disabled || !data.actions?.can_submit}
              onClick={() => void change("READY_FOR_REVIEW")}
            >
              提交视频版本审核
            </Button>
            <Button
              disabled={disabled || !data.actions?.can_approve}
              onClick={() => void change("APPROVED")}
            >
              批准此视频版本
            </Button>
            <ImpactButton
              variant="outline"
              disabled={
                disabled || !data.actions?.can_reject || !comment.trim()
              }
              endpoint={
                version
                  ? "/projects/" +
                    projectId +
                    "/dependency-impact?asset_version_id=" +
                    version.id +
                    "&change_kind=LIFECYCLE"
                  : undefined
              }
              onConfirm={(digest) => change("REJECTED", digest)}
            >
              拒绝此视频版本
            </ImpactButton>
            <ImpactButton
              disabled={
                disabled ||
                !data.actions?.can_select ||
                asset?.current_version_id === version?.id
              }
              endpoint={
                asset?.current_version_id
                  ? "/projects/" +
                    projectId +
                    "/dependency-impact?asset_version_id=" +
                    asset.current_version_id
                  : undefined
              }
              onConfirm={select}
            >
              正式选用此视频
            </ImpactButton>
            <Button
              variant="outline"
              disabled={
                disabled ||
                !(asset?.status === "LOCKED"
                  ? data.actions?.can_unlock
                  : data.actions?.can_lock)
              }
              onClick={() => void lock()}
            >
              {asset?.status === "LOCKED"
                ? "解锁当前视频选用"
                : "锁定当前视频选用"}
            </Button>
            <Button
              variant="outline"
              disabled={
                disabled ||
                !data.actions?.can_restore ||
                (!restorePending && !comment.trim())
              }
              onClick={() => void restore()}
            >
              {restorePending ? "核验原历史另存提交" : "从历史视频另存新候选"}
            </Button>
          </div>
          {data.actions?.can_replace && (
            <section
              aria-label="替换视频候选"
              className="flex flex-col gap-2 border-t pt-3 text-sm"
            >
              <h5 className="font-semibold">替换为新视频候选</h5>
              <p>新文件会完整解码并另存候选，原视频及其批准记录保留。</p>
              {pending ? (
                <Notice>
                  上次替换结果待核对，继续时沿用原文件与同一提交标识。
                </Notice>
              ) : (
                <>
                  <label htmlFor={id + "-file"}>替换 MP4 文件</label>
                  <Input
                    id={id + "-file"}
                    type="file"
                    accept="video/mp4,.mp4"
                    disabled={disabled}
                    onChange={(event) =>
                      setFile(event.target.files?.[0] ?? null)
                    }
                  />
                  <label htmlFor={id + "-source"}>视频来源说明</label>
                  <Input
                    id={id + "-source"}
                    value={source}
                    disabled={disabled}
                    onChange={(event) => setSource(event.target.value)}
                  />
                  <label htmlFor={id + "-rights"}>视频使用权说明</label>
                  <Input
                    id={id + "-rights"}
                    value={rights}
                    disabled={disabled}
                    onChange={(event) => setRights(event.target.value)}
                  />
                  <label className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      checked={confirmed}
                      disabled={disabled}
                      onChange={(event) => setConfirmed(event.target.checked)}
                    />
                    我确认拥有此视频的使用权
                  </label>
                </>
              )}
              <Button
                disabled={
                  disabled ||
                  (!pending &&
                    (!file || !source.trim() || !rights.trim() || !confirmed))
                }
                onClick={() => void replace()}
              >
                {pending ? "核验原视频替换提交" : "上传并另存替换候选"}
              </Button>
            </section>
          )}
        </>
      )}
    </section>
  );
}
