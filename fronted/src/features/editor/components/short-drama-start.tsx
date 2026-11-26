"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import VoicePicker from "@/features/audio/components/voice-picker";
import ImpactButton from "@/features/dependencies/components/impact-button";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import { api, ApiError, message } from "@/lib/api/client";
import { productionDeliveryPath, productionTaskPath } from "@/lib/routes";
import { runErrorMessage } from "@/lib/run-errors";

type ShortDramaIssue = {
  phase: string;
  code: string;
  service_kind: string | null;
  supplier: string | null;
  model: string | null;
  task?: { id: string; family: "WORKFLOW" | "GENERATION" };
};

type ShortDramaPreflight = {
  issues: ShortDramaIssue[];
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
  disabled,
  onStarted,
  onUnavailable,
}: Props) {
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
    readiness.issues.length === 0 &&
    readiness.blocked_voices.length === 0 &&
    rights;

  async function start(impactDigest?: string) {
    if (pending || (!intent.current && !ready)) return false;
    intent.current ??= {
      key: crypto.randomUUID(),
      body: JSON.stringify({
        script_version_id: scriptId,
        impact_digest: impactDigest,
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
      {preflight.error && <Notice tone="error">{preflight.error}</Notice>}
      {readiness?.issues.map((issue) => (
        <ShortDramaIssueNotice
          key={JSON.stringify([
            issue.phase,
            issue.code,
            issue.service_kind,
            issue.supplier,
            issue.model,
          ])}
          issue={issue}
        />
      ))}
      {(preflight.error || !!readiness?.issues.length) && (
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          disabled={controlsLocked || preflight.loading}
          onClick={() => void preflight.load()}
        >
          重新检查制作准备
        </Button>
      )}
      {readiness?.blocked_voices.map(({ speaker, reason }) => (
        <Notice key={`${speaker}:${reason}`} tone="error">
          {speaker} · {reason}
        </Notice>
      ))}
      {!!readiness?.needed_voices.length && !readiness.issues.length && !readiness.blocked_voices.length && (
        <p className="text-sm text-muted-foreground">制作时会为未选音色的角色和旁白自动分配。</p>
      )}
      {!!readiness?.needed_voices.length && (
        <details>
          <summary className="cursor-pointer text-sm">手动选择音色</summary>
          <div className="mt-2 flex flex-col gap-2">
            {readiness.needed_voices.map(({ speaker, character_asset_id }) => {
              const scope = JSON.stringify([projectId, scriptId, speaker, character_asset_id]);
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
          </div>
        </details>
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
  RECONCILING: "等待核对原请求",
  DELIVERED: "成片已生成",
  FAILED: "制作已停止",
};

const issuePhaseLabel: Record<string, string> = {
  PLANNING: "制作规划",
  ART: "美术设计",
  STORYBOARD: "分镜编排",
  AUDIO: "对白和音效",
  SHOTS: "镜头制作",
  RENDERING: "成片渲染",
};

const issueServiceLabel: Record<string, string> = {
  TEXT: "文字模型",
  IMAGE: "图像模型",
  VIDEO: "视频模型",
  SPEECH: "配音模型",
  REVIEW: "质检模型",
};

const reconciliationCodes = new Set([
  "GATEWAY_RECONCILIATION_REQUIRED",
  "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN",
  "SHORT_DRAMA_TTS_UNCERTAIN",
]);

const configurationCodes = new Set([
  "SHORT_DRAMA_TEXT_SERVICE_REQUIRED",
  "SHORT_DRAMA_SPEECH_SERVICE_REQUIRED",
  "SHORT_DRAMA_TEXT_CAPABILITY_REQUIRED",
  "SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED",
  "AUDIO_PERFORMANCE_UNSUPPORTED",
  "AUDIO_VOICE_CATALOG_UNAVAILABLE",
  "AUDIO_VOICE_CATALOG_QUERY_FAILED",
  "GATEWAY_CAPABILITY_REJECTED",
  "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED",
  "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED",
  "SHORT_DRAMA_VIDEO_FORMAT_UNSUPPORTED",
  "PROVIDER_SIZE_UNSUPPORTED",
  "PROVIDER_REFERENCES_UNSUPPORTED",
  "PROVIDER_PARAMETERS_UNSUPPORTED",
  "PROVIDER_TASK_UNSUPPORTED",
  "SERVICE_INCOMPLETE",
  "SERVICE_KEY_UNAVAILABLE",
  "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
  "GATEWAY_PROVIDER_ACCESS_DENIED",
  "PROVIDER_REJECTED",
  "INPUT_INVALID",
]);

const errorLabel: Record<string, string> = {
  ACCOUNT_BALANCE_INSUFFICIENT: "账户余额不足",
  SHORT_DRAMA_TEXT_SERVICE_REQUIRED: "请配置用于制作规划和分镜的文字模型。",
  SHORT_DRAMA_SPEECH_SERVICE_REQUIRED: "请配置用于合成对白的配音模型。",
  SHORT_DRAMA_TEXT_CAPABILITY_REQUIRED:
    "当前文字模型缺少制作编排所需的工具调用或结构化输出能力。请选择兼容的文字模型。",
  SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED:
    "当前文字模型无法满足镜头质检的图片输入要求。请选择能分析镜头图片的文字模型。",
  AUDIO_PERFORMANCE_UNSUPPORTED:
    "当前配音模型不支持对白所需的表演设置。请调整配音设置或更换配音模型。",
  AUDIO_VOICE_CATALOG_UNAVAILABLE: "当前配音模型没有可用音色目录，请选择提供音色目录的模型。",
  AUDIO_VOICE_CATALOG_QUERY_FAILED: "查询当前模型的音色失败，请检查供应商配置后重试。",
  SHORT_DRAMA_VOICE_SELECTION_INVALID: "自动分配返回了无效音色。请手选音色后重新制作。",
  INPUT_INVALID: "模型不接受本步骤的输入。请检查模型名称和可用的生成参数。",
  PROVIDER_REJECTED: "供应商拒绝了请求。请核对模型是否可用，以及该模型接受的输入。",
  PROVIDER_UNAVAILABLE: "当前供应商不可用，请检查服务地址或稍后查看原任务。",
  PROVIDER_FAILED: "供应商未返回符合本步骤要求的结果。请核对模型能力和原任务。",
  GATEWAY_CAPABILITY_REJECTED:
    "当前模型不支持这一步需要的输入或输出。请选择兼容模型。",
  SHORT_DRAMA_VIDEO_FORMAT_UNSUPPORTED:
    "当前视频模型不支持本镜头需要的输出规格。请更换视频模型。",
  PROVIDER_SIZE_UNSUPPORTED:
    "当前模型不支持所需的画面尺寸。请调整项目输出规格或更换模型。",
  PROVIDER_REFERENCES_UNSUPPORTED:
    "当前模型不支持参考素材输入。请选择支持本镜头参考素材的模型。",
  PROVIDER_PARAMETERS_UNSUPPORTED:
    "当前模型不支持本任务的生成参数。请调整设置或更换模型。",
  PROVIDER_TASK_UNSUPPORTED: "当前模型不支持本步骤的生成操作。请选择兼容模型。",
  SERVICE_INCOMPLETE: "当前模型服务配置不完整。请检查供应商、模型和 API Key。",
  SERVICE_KEY_UNAVAILABLE:
    "当前服务的 API Key 不可用。请检查模型服务中的密钥配置。",
  SHORT_DRAMA_VOICE_REQUIRED: "剧本角色缺少可用的已批准声音。",
  SHORT_DRAMA_SFX_REQUIRED: "分镜所需的音效缺少已选用、已授权的素材。",
  SHORT_DRAMA_ART_PROTECTED: "美术设计涉及已批准或锁定的资产。",
  SHORT_DRAMA_INPUT_CHANGED: "定稿剧本或制作规格发生变化。",
  SHORT_DRAMA_VISUAL_SERVICE_REQUIRED: "请配置此制作环节所需的模型。",
  SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED:
    "当前视频模型不支持使用镜头照片作为首帧。请选择支持首帧输入的视频模型。",
  SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED:
    "当前分镜的动态表演或口型需要人工核验。",
  SHORT_DRAMA_VISUAL_HUMAN_REQUIRED: "部分镜头未达到自动交付的质量要求。",
  SHORT_DRAMA_VISUAL_RESULT_UNKNOWN: "供应商的生成结果需要核对。",
  SHORT_DRAMA_TTS_UNCERTAIN: "供应商的配音结果需要核对。",
  SHORT_DRAMA_AUDIO_DURATION: "部分配音超过了对应镜头的时长。",
  SHORT_DRAMA_EXPORT_WARNINGS: "成片有待核查的渲染警告。",
};

function ShortDramaIssueNotice({
  issue,
  runPath,
  canRetryBalance,
}: {
  issue: ShortDramaIssue;
  runPath?: string | null;
  canRetryBalance?: boolean;
}) {
  const step = issuePhaseLabel[issue.phase] ?? issue.phase;
  const service = issue.service_kind
    ? (issueServiceLabel[issue.service_kind] ?? issue.service_kind)
    : null;
  const context = [step, service, issue.supplier, issue.model]
    .filter(Boolean)
    .join(" · ");
  const reconcile = reconciliationCodes.has(issue.code);
  const balance = issue.code === "ACCOUNT_BALANCE_INSUFFICIENT";
  return (
    <Notice tone="error" className="basis-full">
      <p className="font-medium">{context}</p>
      <p>
        {reconcile
          ? "供应商尚未确认本次调用结果。请在供应商后台核对原请求，确认结果后再继续。"
          : (errorLabel[issue.code] ?? runErrorMessage(issue.code))}
      </p>
      {balance && (
        <p>
          {canRetryBalance
            ? "请在供应商账户充值，再继续本次制作。"
            : "请核对供应商账单和原请求的处理状态。"}
        </p>
      )}
      {configurationCodes.has(issue.code) && (
        <p>打开页面右上角的「模型服务配置」。</p>
      )}
      {(reconcile || (balance && !canRetryBalance)) && runPath && (
        <Link className="self-start underline" href={runPath}>
          查看原任务
        </Link>
      )}
    </Notice>
  );
}

type Progress = {
  run_id: string;
  status: string;
  phase: string;
  error_code: string | null;
  issue: ShortDramaIssue | null;
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
          { signal: controller.signal, cache: "no-store" },
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
      {progress.issue ? (
        <ShortDramaIssueNotice
          issue={progress.issue}
          canRetryBalance={progress.can_retry_balance}
          runPath={productionTaskPath({
            id: progress.issue.task?.id ?? progress.run_id,
            episode_id: episodeId,
            project_id: projectId,
            family: progress.issue.task?.family ?? "WORKFLOW",
          })}
        />
      ) : progress.error_code ? (
        <span>
          {errorLabel[progress.error_code] ??
            runErrorMessage(progress.error_code)}
        </span>
      ) : null}
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
