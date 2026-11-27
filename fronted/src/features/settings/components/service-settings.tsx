"use client";

import { useEffect, useRef, useState } from "react";
import {
  Type,
  Image,
  Video,
  AudioLines,
  ShieldCheck,
  Settings2,
  X,
  Check,
  Info,
  PlugZap,
} from "lucide-react";
import Dialog from "@/components/shared/app-dialog";
import { ModelIdInput } from "./model-id-input";
import ServiceProviders from "./service-providers";
import useServiceSettings, {
  type ServiceDraft,
} from "../hooks/use-service-settings";
import type {
  CapabilityCheck,
  ConnectionResult,
  ServiceKind,
} from "../service-settings-api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { FormLabel, Notice, LoadingState } from "@/components/ui-kit";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { ChoiceSelect } from "@/components/choice-select";
import { Spinner } from "@/components/ui/spinner";

const services = [
  { kind: "text" as const, label: "文字模型", icon: Type },
  { kind: "image" as const, label: "图片模型", icon: Image },
  { kind: "video" as const, label: "视频模型", icon: Video },
  { kind: "speech" as const, label: "语音服务", icon: AudioLines },
];
const capabilityLabels: Record<CapabilityCheck["status"], string> = {
  ready: "模型支持智能创作",
  partial: "仅通过部分能力检查",
  unverified: "智能创作能力尚未验证",
};
const connectionLabels: Record<ConnectionResult["status"], string> = {
  model_found: "已查到模型",
  reachable: "服务可达",
  failed: "检测连接失败",
};
const protocolNames: Record<string, string> = {
  openai_chat: "OpenAI Chat Completions",
  gemini: "Google Gemini",
  qwen_chat: "通义 DashScope",
  openai_image: "OpenAI 图片",
  cogview: "智谱 CogView",
  seedream: "火山 Seedream",
  siliconflow_image: "硅基流动图片",
  gemini_image: "Google Gemini 图片",
  seedance: "火山 Seedance",
  cogvideo: "智谱 CogVideo",
  wan: "通义万相",
  veo: "Google Veo",
  gemini_video: "Google Gemini 视频",
  xai_video: "xAI 视频",
  qwen_tts: "通义语音",
  qwen_audio_tts: "阿里云 Qwen Audio TTS",
  siliconflow_tts: "硅基流动语音",
  openai_tts: "OpenAI 语音",
};

export default function ServiceSettings({
  onDismiss,
}: {
  onDismiss: () => void;
}) {
  const {
    config,
    drafts,
    kind,
    draft,
    presets,
    preset,
    speechModel,
    modelSuggestions,
    stored,
    hasStoredKey,
    capability,
    keyReady,
    canVerify,
    canCheck,
    checking,
    connection,
    pendingAction,
    busy,
    error,
    saved,
    changeDraft,
    checkConnection,
    retryLoad,
    saveDraft,
    selectKind,
    selectProvider,
    verifyCapabilities,
  } = useServiceSettings();
  const [confirmClose, setConfirmClose] = useState(false);
  const confirmation = useRef<HTMLDivElement>(null);
  const modelInput = useRef<HTMLInputElement>(null);
  const [modelError, setModelError] = useState("");
  const protocol = draft.protocol || config?.protocols[kind][0];
  const baseUrlExample =
    presets.find((row) => row.protocol === protocol)?.base_url ??
    "https://provider.example";

  useEffect(() => {
    if (confirmClose) {
      confirmation.current?.scrollIntoView({ block: "nearest" });
      confirmation.current?.focus();
    }
  }, [confirmClose]);

  function clearFormFeedback() {
    setModelError("");
    setConfirmClose(false);
  }

  function change(patch: Partial<ServiceDraft>) {
    changeDraft(patch);
    clearFormFeedback();
  }

  function changeProvider(id: string) {
    if (id === draft.supplier) return;
    selectProvider(id);
    clearFormFeedback();
  }

  function dismiss() {
    if (Object.values(drafts).some((row) => row.dirty)) setConfirmClose(true);
    else onDismiss();
  }

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !draft.dirty) return;
    if (!draft.model.trim()) {
      setModelError("请填写供应商提供的模型 ID。");
      modelInput.current?.focus();
      return;
    }
    void saveDraft();
  }

  return (
    <Dialog
      labelledBy="service-settings-title"
      busy={busy}
      onDismiss={dismiss}
      className="flex max-h-[90dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-6xl"
    >
      <header className="flex shrink-0 items-start justify-between gap-4 border-b px-5 py-4 sm:px-6">
        <div className="flex items-start gap-3">
          <Settings2 className="mt-1 size-5 text-primary" aria-hidden="true" />
          <div>
            <h2 id="service-settings-title" className="text-lg">
              模型与服务
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              选择模型服务并保存到你的账号。
            </p>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="关闭模型配置"
          disabled={busy}
          onClick={dismiss}
        >
          <X />
        </Button>
      </header>
      {!config ? (
        <div className="p-6">
          {error ? (
            <Notice tone="error" role="alert">
              {error}
              <Button variant="outline" onClick={retryLoad}>
                重试读取配置
              </Button>
            </Notice>
          ) : (
            <LoadingState>正在读取账号服务配置…</LoadingState>
          )}
        </div>
      ) : (
        <div className="flex min-h-0 flex-col overflow-y-auto md:flex-row">
          <aside className="shrink-0 border-b bg-muted/40 p-3 md:w-52 md:border-r md:border-b-0 md:p-4">
            <ToggleGroup
              type="single"
              value={kind}
              onValueChange={(value) => {
                if (!value) return;
                selectKind(value as ServiceKind);
                clearFormFeedback();
              }}
              aria-label="服务类型"
              className="grid w-full grid-cols-2 gap-1.5 md:flex md:flex-col"
              disabled={busy}
            >
              {services.map((service) => (
                <ToggleGroupItem
                  key={service.kind}
                  value={service.kind}
                  aria-label={service.label}
                  className="h-11 w-full justify-start gap-2 rounded-md border border-border bg-background px-3 text-sm data-[state=on]:border-primary data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
                >
                  <service.icon
                    className="size-4 shrink-0"
                    aria-hidden="true"
                  />
                  {service.label}
                  {drafts[service.kind].dirty ? (
                    <span
                      className="size-1.5 shrink-0 rounded-full bg-current"
                      aria-label="未保存"
                    />
                  ) : null}
                  <Badge
                    variant="secondary"
                    className="ml-auto tabular-nums"
                    aria-label={`${config.suppliers.filter((row) => row.kind === service.kind).length} 个预设`}
                  >
                    {
                      config.suppliers.filter(
                        (row) => row.kind === service.kind,
                      ).length
                    }
                  </Badge>
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <p className="mt-5 hidden text-xs leading-relaxed text-muted-foreground md:block">
              每类服务单独保存，
              <br />
              创作任务使用发起者的配置。
            </p>
          </aside>
          <div className="flex min-w-0 flex-1 flex-col gap-6 p-4 sm:p-6 md:max-h-[calc(90dvh-87px)] md:overflow-y-auto">
            <Alert role="note">
              <Info aria-hidden="true" />
              <AlertDescription>
                服务商预设会带入已登记的连接参数和推荐模型。自定义服务需选择 API
                协议并填写 Base URL。
              </AlertDescription>
            </Alert>
            <ServiceProviders
              presets={presets}
              value={draft.supplier}
              disabled={busy}
              onSelect={changeProvider}
            />
            <form
              onSubmit={save}
              className="flex min-w-0 flex-col gap-4 border-t pt-5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {(preset?.name ?? "自定义服务") + "配置"}
                </h3>
                <span className="text-xs text-muted-foreground">
                  {hasStoredKey && !draft.clear_key
                    ? "用于新任务"
                    : "待配置密钥"}
                </span>
              </div>
              {draft.clear_key && (
                <Notice tone="info">
                  保存后清除 API Key，并保留服务配置。新任务需要个人服务。
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => change({ clear_key: false })}
                  >
                    保留原密钥
                  </Button>
                </Notice>
              )}
              {draft.supplier === "custom" && (
                <FormLabel>
                  供应商名称（选填）
                  <Input
                    aria-label="供应商名称（选填）"
                    maxLength={100}
                    placeholder="例如：我的模型服务"
                    value={draft.supplier_name}
                    disabled={busy}
                    onChange={(event) =>
                      change({ supplier_name: event.target.value })
                    }
                  />
                </FormLabel>
              )}
              <FormLabel>
                API Key
                <Input
                  type="password"
                  required={!hasStoredKey && !draft.clear_key}
                  autoComplete="new-password"
                  maxLength={2000}
                  value={draft.api_key}
                  disabled={busy || !config.can_store_keys}
                  placeholder={
                    hasStoredKey && !draft.clear_key
                      ? "已保存密钥；留空保持不变"
                      : "粘贴此服务商的 API Key"
                  }
                  onChange={(event) =>
                    change({
                      api_key: event.target.value,
                      clear_key: false,
                    })
                  }
                />
              </FormLabel>
              <div className="-mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>
                  {draft.clear_key
                    ? "保存后将清除密钥"
                    : hasStoredKey
                      ? "密钥已加密保存"
                      : stored?.has_key
                        ? "服务商或地址已更改，请填写对应密钥"
                        : "密钥仅加密保存在你的账号下"}
                </span>
                {hasStoredKey && !draft.clear_key && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() =>
                      change({
                        clear_key: true,
                        api_key: "",
                      })
                    }
                  >
                    清除已存密钥
                  </Button>
                )}
              </div>
              {!preset && (
                <FormLabel>
                  API 协议
                  <ChoiceSelect
                    aria-label="API 协议"
                    value={protocol}
                    disabled={busy}
                    onValueChange={(protocol) =>
                      change({ protocol, api_key: "", clear_key: false })
                    }
                    options={config.protocols[kind].map((value) => ({
                      value,
                      label: protocolNames[value] ?? value,
                    }))}
                  />
                </FormLabel>
              )}
              <FieldGroup className="grid min-w-0 gap-4 sm:grid-cols-2">
                {!preset && (
                  <Field data-disabled={busy}>
                    <FieldLabel htmlFor="service-base-url">Base URL</FieldLabel>
                    <Input
                      id="service-base-url"
                      aria-describedby="service-base-url-hint"
                      required
                      type="url"
                      placeholder={baseUrlExample}
                      maxLength={500}
                      value={draft.base_url}
                      disabled={busy}
                      onChange={(event) =>
                        change({
                          base_url: event.target.value,
                          api_key: "",
                          clear_key: false,
                        })
                      }
                    />
                    <FieldDescription id="service-base-url-hint">
                      填写公网 HTTPS API 基础地址，保留服务要求的路径前缀。示例
                      <span className="ml-1 break-all">{baseUrlExample}</span>
                    </FieldDescription>
                  </Field>
                )}
                <Field data-invalid={!!modelError} data-disabled={busy}>
                  <FieldLabel htmlFor="service-model">
                    模型 ID
                    <span className="text-xs font-normal text-muted-foreground">
                      （必填）
                    </span>
                  </FieldLabel>
                  <ModelIdInput
                    key={`${kind}:${draft.supplier}`}
                    id="service-model"
                    inputRef={modelInput}
                    invalid={!!modelError}
                    describedBy={
                      modelError ? "service-model-error" : "service-model-hint"
                    }
                    value={draft.model}
                    disabled={busy}
                    suggestions={modelSuggestions}
                    onValueChange={(model) => change({ model })}
                  />
                  <FieldDescription id="service-model-hint">
                    {speechModel?.description ??
                      (modelSuggestions.length === 0
                        ? "填写当前服务商支持的模型 ID。"
                        : kind === "speech"
                          ? "可选择 TTS 预设，或直接填写供应商支持的模型 ID。"
                          : "可选择推荐模型，或直接填写供应商支持的模型 ID。")}
                  </FieldDescription>
                  {modelError && (
                    <FieldError id="service-model-error">
                      {modelError}
                    </FieldError>
                  )}
                </Field>
              </FieldGroup>
              {preset && (
                <div className="space-y-1 text-xs text-muted-foreground">
                  <p>{protocolNames[preset.protocol] ?? preset.protocol}</p>
                  <p className="break-all">{preset.base_url}</p>
                </div>
              )}
              {capability && (
                <section
                  aria-label="智能创作能力"
                  className="flex flex-col items-start gap-2 rounded-lg border p-3"
                >
                  <div aria-live="polite">
                    <p className="text-sm font-medium">
                      {capabilityLabels[capability.status]}
                    </p>
                    {capability.status !== "unverified" && (
                      <ul className="mt-1 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                        <li>已确认结构化输出</li>
                        <li>
                          {capability.tools ? "支持工具调用" : "工具调用待确认"}
                        </li>
                        <li>
                          {capability.max_input_images > 0
                            ? `支持参考图（最多 ${capability.max_input_images} 张）`
                            : "参考图能力待确认"}
                        </li>
                      </ul>
                    )}
                  </div>
                  {capability.status === "partial" && (
                    <p className="text-xs text-muted-foreground">
                      {canVerify ? "可更换模型或再验证一次。" : "可更换模型。"}
                    </p>
                  )}
                  {capability.status === "unverified" &&
                    keyReady &&
                    !canVerify && (
                      <p className="text-xs text-muted-foreground">
                        可选择已确认支持智能创作的模型。
                      </p>
                    )}
                  {canVerify && (
                    <>
                      <p className="text-xs text-muted-foreground">
                        检查结构化输出、工具调用和参考图。
                      </p>
                      <p
                        id="service-capability-check-hint"
                        className="text-xs text-muted-foreground"
                      >
                        验证只发送合成测试内容，供应商可能收费。
                      </p>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        aria-busy={pendingAction === "verify"}
                        aria-describedby="service-capability-check-hint"
                        onClick={verifyCapabilities}
                      >
                        {pendingAction === "verify" && (
                          <Spinner aria-hidden="true" />
                        )}
                        {pendingAction === "verify"
                          ? "验证中…"
                          : "同意并验证模型"}
                      </Button>
                    </>
                  )}
                </section>
              )}
              {!config.can_store_keys && (
                <Notice tone="info">
                  {config.key_storage.message}
                  。请联系管理员恢复密钥存储后再保存个人服务。
                </Notice>
              )}
              {hasStoredKey &&
                draft.key_status &&
                !["READY", "MISSING"].includes(draft.key_status) && (
                  <Notice tone="error">
                    已存凭证暂时不可读取，请联系管理员检查主密钥，或重新填写 API
                    Key 后保存。
                  </Notice>
                )}
              <p className="text-xs leading-relaxed text-muted-foreground">
                检测连接会用当前 API Key 向所选服务发送只读请求。
                {kind === "text" &&
                  "新任务会将故事素材发送到所选服务，按供应商规则计费。"}
              </p>
              {connection && connection !== "checking" && (
                <Notice
                  tone={
                    connection.status === "model_found"
                      ? "success"
                      : connection.status === "failed"
                        ? "error"
                        : "info"
                  }
                >
                  <span className="font-medium">
                    {connectionLabels[connection.status]}
                  </span>
                  <span>{connection.message}</span>
                </Notice>
              )}
              {error && (
                <Notice tone="error" role="alert">
                  {error}
                </Notice>
              )}
              {saved && (
                <Notice tone="success" role="status">
                  <span className="flex items-start gap-2">
                    <Check className="mt-0.5 size-4 shrink-0" />
                    {saved}
                  </span>
                </Notice>
              )}
              {confirmClose && (
                <Notice
                  ref={confirmation}
                  tabIndex={-1}
                  tone="info"
                  role="alert"
                >
                  有尚未保存的服务配置，要放弃并关闭吗？
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => setConfirmClose(false)}
                    >
                      继续编辑配置
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      onClick={onDismiss}
                    >
                      放弃配置并关闭
                    </Button>
                  </div>
                </Notice>
              )}
              <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <ShieldCheck className="size-4" />
                  账号隔离 · 密钥不回传浏览器
                </span>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy}
                    onClick={dismiss}
                  >
                    关闭
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy || checking || !canCheck}
                    aria-busy={checking}
                    onClick={checkConnection}
                  >
                    {checking ? (
                      <Spinner aria-hidden="true" />
                    ) : (
                      <PlugZap aria-hidden="true" />
                    )}
                    {checking ? "检测中…" : "检测连接"}
                  </Button>
                  <Button disabled={busy || !draft.dirty}>
                    {pendingAction === "save" && <Spinner aria-hidden="true" />}
                    {pendingAction === "save" ? "保存中…" : "保存此服务"}
                  </Button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </Dialog>
  );
}
