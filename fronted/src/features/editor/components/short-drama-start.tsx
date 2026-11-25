"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import VoicePicker from "@/features/audio/components/voice-picker";
import ImpactButton from "@/features/dependencies/components/impact-button";
import SubtitleFontPicker from "@/features/rendering/components/subtitle-font-picker";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import { api, ApiError, message } from "@/lib/api/client";
import { productionDeliveryPath } from "@/lib/routes";

type ShortDramaPreflight = {
  needed_voices: { speaker: string; character_asset_id: string | null }[];
  blocked_voices: { speaker: string; reason: string }[];
};

type Props = {
  episodeId: string;
  projectId: string;
  scriptId: string;
  version: number;
  awaitingApproval: boolean;
  unsavedScript: boolean;
  hasDialogue: boolean;
  disabled: boolean;
  onStarted: () => Promise<void>;
  onUnavailable: (cause: unknown) => void;
};

export default function ShortDramaStart({
  episodeId,
  projectId,
  scriptId,
  version,
  awaitingApproval,
  unsavedScript,
  hasDialogue,
  disabled,
  onStarted,
  onUnavailable,
}: Props) {
  const [font, setFont] = useState("");
  const [rights, setRights] = useState(false);
  const [pending, setPending] = useState(false);
  const [retryError, setRetryError] = useState("");
  const intent = useRef<{ body: string; key: string } | null>(null);
  const preflightPath = `/episodes/${episodeId}/short-drama/preflight?script_version_id=${encodeURIComponent(scriptId)}`;
  const readPreflight = useCallback(
    (signal: AbortSignal) =>
      api<ShortDramaPreflight>(preflightPath, { signal, cache: "no-store" }),
    [preflightPath],
  );
  const preflight = useRenderDetail(
    preflightPath,
    readPreflight,
    onUnavailable,
  );
  const readiness = preflight.data;
  const controlsLocked = disabled || pending || !!intent.current;
  const ready =
    !controlsLocked &&
    !unsavedScript &&
    !!readiness &&
    !preflight.loading &&
    readiness.needed_voices.length === 0 &&
    readiness.blocked_voices.length === 0 &&
    rights &&
    (!hasDialogue || !!font);

  async function start(impactDigest?: string) {
    if (pending || (!intent.current && !ready)) return false;
    intent.current ??= {
      key: crypto.randomUUID(),
      body: JSON.stringify({
        script_version_id: scriptId,
        impact_digest: impactDigest,
        font_file_id: font || null,
        generated_media_rights_confirmed: rights,
      }),
    };
    setPending(true);
    setRetryError("");
    try {
      await api(`/episodes/${episodeId}/short-drama`, {
        method: "POST",
        body: intent.current.body,
        headers: { "Idempotency-Key": intent.current.key },
      });
      await onStarted();
      intent.current = null;
    } catch (cause) {
      if (
        cause instanceof ApiError &&
        cause.status < 500 &&
        cause.status !== 408
      ) {
        intent.current = null;
        void preflight.load();
        throw cause;
      }
      setRetryError(message(cause));
    } finally {
      setPending(false);
    }
  }

  return (
    <section
      aria-label="短剧制作"
      className="flex flex-col gap-3 rounded-lg border p-4"
    >
      {unsavedScript && <Notice>请先保存剧本修改，再确认制作。</Notice>}
      {preflight.loading && (
        <p role="status" className="text-sm">
          正在核对制作准备…
        </p>
      )}
      {preflight.error && (
        <Notice tone="error">
          {preflight.error}
          <Button
            variant="outline"
            size="sm"
            onClick={() => void preflight.load()}
          >
            重新检查制作准备
          </Button>
        </Notice>
      )}
      {readiness?.blocked_voices.map(({ speaker, reason }) => (
        <Notice key={`${speaker}:${reason}`} tone="error">
          {speaker} · {reason}
        </Notice>
      ))}
      {readiness?.needed_voices.map(({ speaker, character_asset_id }) => {
        const scope = JSON.stringify([
          projectId,
          scriptId,
          speaker,
          character_asset_id,
        ]);
        return (
          <VoicePicker
            key={scope}
            projectId={projectId}
            speaker={speaker}
            characterId={character_asset_id}
            saveOnly
            scope={scope}
            writable={!controlsLocked}
            onSelected={preflight.refresh}
            unavailable={onUnavailable}
          />
        );
      })}
      {hasDialogue && (
        <SubtitleFontPicker
          projectId={projectId}
          value={font}
          onChange={setFont}
          disabled={controlsLocked}
          unavailable={onUnavailable}
        />
      )}
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={rights}
          onChange={(event) => setRights(event.target.checked)}
          disabled={controlsLocked}
        />
        我确认本次生成的画面和配音具备用于此短剧的授权。
      </label>
      <p className="text-xs text-muted-foreground">
        使用剧本 v{version} 在后台制作。关掉页面后仍会继续。
      </p>
      {retryError && <Notice tone="error">{retryError}</Notice>}
      {intent.current ? (
        <Button
          disabled={pending}
          onClick={() =>
            void start().catch((error) => setRetryError(message(error)))
          }
        >
          {pending ? "正在提交…" : "核对并重试本次提交"}
        </Button>
      ) : (
        <ImpactButton
          endpoint={
            awaitingApproval
              ? `/episodes/${episodeId}/scripts/${scriptId}/dependency-impact`
              : undefined
          }
          disabled={!ready}
          onConfirm={start}
          confirmedLabel="确认影响并生成短剧"
          impactNote="确认后会开始后台制作。"
        >
          确认剧本并生成短剧
        </ImpactButton>
      )}
    </section>
  );
}

const phaseLabel: Record<string, string> = {
  PLANNING: "正在规划制作",
  ART: "正在设计角色、场景与道具",
  STORYBOARD: "正在编排分镜",
  AUDIO: "正在处理对白和音效",
  SHOTS: "正在生成并检查镜头",
  RENDERING: "正在渲染成片",
  RECONCILING: "正在核对首次生成请求",
  DELIVERED: "成片已生成",
  FAILED: "制作已停止",
};

const errorLabel: Record<string, string> = {
  ACCOUNT_BALANCE_INSUFFICIENT: "账户余额不足",
  SHORT_DRAMA_VOICE_REQUIRED: "剧本角色缺少可用的已批准声音。",
  SHORT_DRAMA_SFX_REQUIRED: "分镜所需的音效缺少已选用、已授权的素材。",
  SHORT_DRAMA_ART_PROTECTED: "美术设计涉及已批准或锁定的资产。",
  SHORT_DRAMA_INPUT_CHANGED: "定稿剧本或制作规格发生变化。",
  SHORT_DRAMA_VISUAL_SERVICE_REQUIRED: "此账号缺少已配置的短剧图像或视频服务。",
  SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED:
    "当前视频服务不支持使用镜头照片作为首帧。",
  SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED:
    "当前分镜的动态表演或口型需要人工核验。",
  SHORT_DRAMA_VISUAL_HUMAN_REQUIRED: "部分镜头未达到自动交付的质量要求。",
  SHORT_DRAMA_VISUAL_RESULT_UNKNOWN: "供应商的生成结果需要核对。",
  SHORT_DRAMA_TTS_UNCERTAIN: "供应商的配音结果需要核对。",
  SHORT_DRAMA_AUDIO_DURATION: "部分配音超过了对应镜头的时长。",
  SHORT_DRAMA_EXPORT_WARNINGS: "成片有待核查的渲染警告。",
};

type Progress = {
  run_id: string;
  status: string;
  phase: string;
  error_code: string | null;
  revision: number;
  can_retry_balance: boolean;
  delivery_id?: string;
  render_progress?: number;
  stopped_phase?: string;
};

export function ShortDramaProgress({
  episodeId,
  projectId,
  refreshRevision,
}: {
  episodeId: string;
  projectId: string;
  refreshRevision?: number;
}) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [retryRevision, setRetryRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      let delay = 5000;
      try {
        const value = await api<Progress | null>(
          `/episodes/${episodeId}/short-drama`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        setProgress(value);
        setError("");
        if (!value || !["RUNNING", "QUEUED"].includes(value.status))
          delay = 15000;
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(message(cause));
        delay = 15000;
      }
      timer = window.setTimeout(() => void poll(), delay);
    }
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [episodeId, refreshRevision, retryRevision]);

  if (!progress) return error ? <Notice tone="error">{error}</Notice> : null;
  async function retryBalance(run: Progress) {
    setRetrying(true);
    setError("");
    try {
      await api(`/runs/${run.run_id}/short-drama/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ expected_revision: run.revision }),
      });
      setRetryRevision((revision) => revision + 1);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setRetrying(false);
    }
  }

  const delivery =
    progress.delivery_id &&
    productionDeliveryPath({
      id: progress.delivery_id,
      episode_id: episodeId,
      project_id: projectId,
    });
  return (
    <section
      aria-label="短剧制作进度"
      aria-live="polite"
      className="flex flex-wrap items-center gap-3 rounded-lg border bg-card px-4 py-3 text-sm"
    >
      <strong>
        {progress.status === "CANCELLED"
          ? "制作已取消"
          : (phaseLabel[progress.phase] ?? "正在制作短剧")}
      </strong>
      {progress.phase === "RENDERING" &&
        typeof progress.render_progress === "number" && (
          <span>{progress.render_progress}%</span>
        )}
      {progress.error_code && (
        <span>
          {errorLabel[progress.error_code] ??
            `制作在当前环节停止（${progress.error_code}）。`}
        </span>
      )}
      {progress.can_retry_balance && (
        <Button
          variant="outline"
          size="sm"
          disabled={retrying}
          onClick={() => void retryBalance(progress)}
        >
          {retrying ? "正在恢复" : "充值后继续制作"}
        </Button>
      )}
      {delivery && (
        <Button variant="outline" size="sm" asChild>
          <Link href={delivery}>播放或下载成片</Link>
        </Button>
      )}
      {error && <span>{error}</span>}
    </section>
  );
}
