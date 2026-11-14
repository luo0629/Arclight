"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { Notice, StatusBadge } from "@/components/ui-kit";
import ImpactButton from "@/features/dependencies/components/impact-button";
import useRenderMutation from "@/features/rendering/hooks/use-render-mutation";
import type { AssetVersion } from "@/types/api";
import { audioApi } from "../api";
import type { AudioAsset } from "../types";
import { AudioFeedback } from "./audio-fields";

export default function AudioVersionActions({
  projectId,
  asset,
  version,
  scope,
  owner,
  writable,
  onSaved,
  unavailable,
}: {
  projectId: string;
  asset: AudioAsset;
  version: AssetVersion;
  scope: string;
  owner: boolean;
  writable: boolean;
  onSaved: () => void;
  unavailable: (cause: unknown) => void;
}) {
  const [comment, setComment] = useState("");
  const mutation = useRenderMutation(scope, writable, unavailable);
  const locked = asset.status === "LOCKED" || version.status === "LOCKED";
  const impact = `/projects/${projectId}/dependency-impact?asset_version_id=${version.id}&change_kind=LIFECYCLE`;
  async function change(status: string, digest?: string) {
    let saved = false;
    await mutation.mutate(
      (signal) =>
        audioApi.changeState(
          projectId,
          asset.id,
          version.id,
          {
            expected_revision: version.revision,
            status,
            comment: comment.trim(),
            ...(digest ? { impact_digest: digest } : {}),
          },
          signal,
        ),
      () => {
        saved = true;
        onSaved();
      },
      "本句版本状态已保存，历史内容保持原样。",
    );
    return saved;
  }
  async function select(digest?: string) {
    let saved = false;
    await mutation.mutate(
      (signal) =>
        audioApi.select(
          projectId,
          asset.id,
          version.id,
          {
            expected_revision: asset.revision,
            expected_lock_version: asset.lock_version,
            ...(digest ? { impact_digest: digest } : {}),
          },
          signal,
        ),
      () => {
        saved = true;
        onSaved();
      },
      "已选用本句的精确音频版本。已有时间轴和成片不会被静默替换。",
    );
    return saved;
  }
  async function toggleLock() {
    if (!asset.current_version_id) return;
    await mutation.mutate(
      (signal) =>
        audioApi.lock(
          projectId,
          asset.id,
          asset.current_version_id!,
          {
            expected_revision: asset.revision,
            expected_lock_version: asset.lock_version,
          },
          asset.status === "LOCKED",
          signal,
        ),
      () => onSaved(),
      "本句选用音频的锁定状态已更新。",
    );
  }
  return (
    <section
      aria-label="本句音频审核与选用"
      className="flex min-w-0 flex-col gap-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={version.status} />
        {version.stale && <StatusBadge status="STALE" />}
        {asset.current_version_id === version.id && (
          <span className="text-sm">当前选用版本</span>
        )}
      </div>
      {locked && (
        <Notice>
          本句选用已锁定。请由项目拥有者解锁后再生成、替换或修改选用。
        </Notice>
      )}
      {writable && !locked && (
        <>
          <Field>
            <FieldLabel htmlFor="audio-version-comment">
              音频审核说明
            </FieldLabel>
            <Textarea
              id="audio-version-comment"
              maxLength={1000}
              value={comment}
              disabled={mutation.busy}
              onChange={(event) => setComment(event.target.value)}
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            {version.status === "DRAFT" && (
              <Button
                disabled={mutation.busy}
                onClick={() => void change("READY_FOR_REVIEW")}
              >
                提交音频审核
              </Button>
            )}
            {owner && version.status === "READY_FOR_REVIEW" && (
              <>
                <Button
                  disabled={mutation.busy}
                  onClick={() => void change("APPROVED")}
                >
                  批准本句音频
                </Button>
                <ImpactButton
                  variant="outline"
                  disabled={mutation.busy || !comment.trim()}
                  endpoint={impact}
                  onConfirm={(digest) => change("REJECTED", digest)}
                >
                  拒绝本句音频
                </ImpactButton>
              </>
            )}
            {version.status === "APPROVED" && !version.stale && (
              <ImpactButton
                disabled={
                  mutation.busy || asset.current_version_id === version.id
                }
                endpoint={
                  asset.current_version_id
                    ? `/projects/${projectId}/dependency-impact?asset_version_id=${asset.current_version_id}`
                    : undefined
                }
                onConfirm={select}
              >
                选用本句音频
              </ImpactButton>
            )}
          </div>
        </>
      )}
      {owner && writable && asset.current_version_id && (
        <Button
          variant="outline"
          disabled={mutation.busy}
          onClick={() => void toggleLock()}
        >
          {asset.status === "LOCKED" ? "解锁本句选用音频" : "锁定本句选用音频"}
        </Button>
      )}
      <AudioFeedback {...mutation} />
    </section>
  );
}
