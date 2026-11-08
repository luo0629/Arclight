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
} from "lucide-react";
import Dialog from "@/components/shared/app-dialog";
import ServiceProviders, {
  type ServiceKind as Kind,
  type ServicePreset,
  type ResponseFormat,
} from "./service-providers";
import { api, json, message } from "@/lib/api/client";
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

type Profile = {
  kind: Kind;
  mode: "server" | "custom";
  supplier: string;
  supplier_name: string;
  response_format: ResponseFormat;
  protocol: string;
  base_url: string;
  model: string;
  has_key: boolean;
  revision: number;
  key_status?: string;
};
type Draft = Profile & { api_key: string; clear_key: boolean; dirty: boolean };
type Configuration = {
  services: Profile[];
  suppliers: ServicePreset[];
  protocols: Record<Kind, string[]>;
  can_store_keys: boolean;
  text_runtime: string;
  key_storage: { code: string; message: string };
};
const services = [
  { kind: "text" as const, label: "文字模型", icon: Type },
  { kind: "image" as const, label: "图片模型", icon: Image },
  { kind: "video" as const, label: "视频模型", icon: Video },
  { kind: "speech" as const, label: "语音服务", icon: AudioLines },
];
const empty = (kind: Kind): Draft => ({
  kind,
  mode: "server",
  supplier: "custom",
  supplier_name: "",
  response_format: "json_schema",
  protocol: "",
  base_url: "",
  model: "",
  has_key: false,
  revision: 0,
  api_key: "",
  clear_key: false,
  dirty: false,
});
const initialDrafts = (): Record<Kind, Draft> => ({
  text: empty("text"),
  image: empty("image"),
  video: empty("video"),
  speech: empty("speech"),
});
const endpoint = (url: string) => url.trim().replace(/\/+$/, "");
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
  siliconflow_tts: "硅基流动语音",
  openai_tts: "OpenAI 语音",
};

export default function ServiceSettings({
  onDismiss,
}: {
  onDismiss: () => void;
}) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [drafts, setDrafts] = useState(initialDrafts);
  const [kind, setKind] = useState<Kind>("text");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [confirmClose, setConfirmClose] = useState(false);
  const confirmation = useRef<HTMLDivElement>(null);
  const modelInput = useRef<HTMLInputElement>(null);
  const [modelError, setModelError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (confirmClose) {
      confirmation.current?.scrollIntoView({ block: "nearest" });
      confirmation.current?.focus();
    }
  }, [confirmClose]);
  useEffect(() => {
    let live = true;
    setError("");
    void api<Configuration>("/me/model-services?catalog_version=2")
      .then((result) => {
        if (!live) return;
        const next = initialDrafts();
        for (const profile of result.services) {
          const official = result.suppliers.find(
            (item) =>
              item.kind === profile.kind && item.id === profile.supplier,
          );
          const changedEndpoint =
            official &&
            endpoint(profile.base_url) !== endpoint(official.base_url);
          next[profile.kind] = {
            ...empty(profile.kind),
            ...profile,
            ...(changedEndpoint ? { supplier: "custom" } : {}),
            protocol:
              profile.protocol ||
              (profile.kind === "text"
                ? "openai_chat"
                : official?.protocol || result.protocols[profile.kind][0]),
          };
        }
        setDrafts(next);
        setConfig(result);
      })
      .catch((err) => {
        if (live) setError(message(err));
      });
    return () => {
      live = false;
    };
  }, [attempt]);
  const draft = drafts[kind];
  const presets =
    config?.suppliers.filter((supplier) => supplier.kind === kind) ?? [];
  const preset = presets.find((row) => row.id === draft.supplier);
  const stored = config?.services.find((row) => row.kind === kind);
  const hasStoredKey =
    !!stored?.has_key &&
    stored.supplier === draft.supplier &&
    endpoint(stored.base_url) === endpoint(draft.base_url) &&
    (!stored.protocol || stored.protocol === draft.protocol);

  function change(patch: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [kind]: { ...current[kind], ...patch, dirty: true },
    }));
    setSaved("");
    setError("");
    setModelError("");
    setConfirmClose(false);
  }
  function selectProvider(id: string) {
    if (id === "server") {
      change({ mode: "server" });
      return;
    }
    const next = presets.find((row) => row.id === id);
    if (id === draft.supplier) {
      change({ mode: "custom" });
      return;
    }
    const previous = stored?.supplier === id ? stored : null;
    change({
      supplier: id,
      supplier_name: next?.name ?? previous?.supplier_name ?? "",
      mode: "custom",
      protocol:
        next?.protocol ??
        previous?.protocol ??
        config?.protocols[kind][0] ??
        "",
      base_url: next?.base_url ?? previous?.base_url ?? "",
      model: previous?.model ?? "",
      response_format:
        next?.response_format ?? previous?.response_format ?? "json_schema",
      api_key: "",
      clear_key: false,
    });
  }
  function dismiss() {
    if (Object.values(drafts).some((row) => row.dirty)) setConfirmClose(true);
    else onDismiss();
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !draft.dirty) return;
    if (draft.mode === "custom" && !draft.model.trim()) {
      setModelError("请填写供应商提供的模型 ID。");
      modelInput.current?.focus();
      return;
    }
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const profile = await api<Profile>(
        "/me/model-services/" + kind,
        json("PUT", {
          contract_version: 2,
          mode: draft.mode,
          supplier: draft.supplier,
          supplier_name: draft.supplier_name,
          response_format: draft.response_format,
          protocol: draft.protocol,
          base_url: preset ? undefined : draft.base_url,
          model: draft.model,
          api_key: draft.api_key || null,
          clear_key: draft.clear_key,
          expected_revision: draft.revision,
        }),
      );
      setDrafts((current) => ({
        ...current,
        [kind]: { ...empty(kind), ...profile },
      }));
      setConfig((current) =>
        current
          ? {
              ...current,
              services: [
                ...current.services.filter((row) => row.kind !== kind),
                profile,
              ],
            }
          : current,
      );
      setSaved(
        "已保存到当前账号。" +
          (draft.mode === "custom"
            ? "新建的对应任务将使用此配置，已有任务保留原配置。"
            : "新任务使用服务器默认服务。"),
      );
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
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
              选择服务商，填写 API Key 和模型 ID。配置仅属于你的账号。
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
              <Button
                variant="outline"
                onClick={() => setAttempt((n) => n + 1)}
              >
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
                setKind(value as Kind);
                setError("");
                setModelError("");
                setSaved("");
                setConfirmClose(false);
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
                指定服务商只需填写 API Key 和模型 ID；选择「自定义」还需填写
                Base URL。
              </AlertDescription>
            </Alert>
            <ServiceProviders
              kind={kind}
              presets={presets}
              value={draft.mode === "server" ? "server" : draft.supplier}
              disabled={busy}
              onSelect={selectProvider}
            />
            <form
              onSubmit={save}
              className="flex min-w-0 flex-col gap-4 border-t pt-5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {draft.mode === "server"
                    ? "服务器默认服务"
                    : (preset?.name ?? "自定义服务") + "配置"}
                </h3>
                <span className="text-xs text-muted-foreground">
                  {draft.mode === "server" ? "个人配置未启用" : "用于新任务"}
                </span>
              </div>
              {draft.clear_key && (
                <Notice tone="info">
                  保存后清除个人密钥，新任务使用服务器默认服务。
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => change({ clear_key: false, mode: "custom" })}
                  >
                    保留原密钥
                  </Button>
                </Notice>
              )}
              {draft.mode === "custom" && (
                <>
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
                      required={!hasStoredKey || draft.clear_key}
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
                    {hasStoredKey && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() =>
                          change({
                            clear_key: true,
                            api_key: "",
                            mode: "server",
                          })
                        }
                      >
                        {draft.clear_key ? "保留原密钥" : "清除已存密钥"}
                      </Button>
                    )}
                  </div>
                  <FieldGroup className="grid min-w-0 gap-4 sm:grid-cols-2">
                    {!preset && (
                      <FormLabel>
                        Base URL
                        <Input
                          aria-label="Base URL"
                          required
                          type="url"
                          placeholder="https://provider.example/v1"
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
                        <span className="text-xs font-normal text-muted-foreground">
                          填写服务的 API 基础地址，使用公网 HTTPS。
                        </span>
                      </FormLabel>
                    )}
                    <Field data-invalid={!!modelError} data-disabled={busy}>
                      <FieldLabel htmlFor="service-model">
                        模型 ID
                        {(preset || draft.mode === "custom") && (
                          <span className="text-xs font-normal text-muted-foreground">
                            （必填）
                          </span>
                        )}
                      </FieldLabel>
                      <Input
                        id="service-model"
                        ref={modelInput}
                        aria-label="模型 ID"
                        required
                        aria-required={!!preset || draft.mode === "custom"}
                        aria-invalid={!!modelError}
                        aria-describedby={
                          modelError
                            ? "service-model-error"
                            : "service-model-hint"
                        }
                        placeholder="供应商提供的模型 ID"
                        maxLength={160}
                        value={draft.model}
                        disabled={busy}
                        onChange={(event) =>
                          change({ model: event.target.value })
                        }
                      />
                      <FieldDescription id="service-model-hint">
                        请手动填写当前服务商支持的模型 ID。
                      </FieldDescription>
                      {modelError && (
                        <FieldError id="service-model-error">
                          {modelError}
                        </FieldError>
                      )}
                    </Field>
                  </FieldGroup>
                  {preset && (
                    <p className="text-xs text-muted-foreground">
                      使用 {preset.name} 官方接口，服务地址由系统配置。
                    </p>
                  )}
                  {!preset && (
                    <FormLabel>
                      API 协议
                      <ChoiceSelect
                        aria-label="API 协议"
                        value={draft.protocol || config.protocols[kind][0]}
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
                  {kind === "text" && !preset && (
                    <FormLabel>
                      结构化输出格式
                      <ChoiceSelect
                        aria-label="结构化输出格式"
                        value={draft.response_format}
                        disabled={busy}
                        onValueChange={(value) =>
                          change({ response_format: value as ResponseFormat })
                        }
                        options={[
                          { value: "json_schema", label: "JSON Schema" },
                          {
                            value: "json_object",
                            label: "JSON 对象",
                          },
                        ]}
                      />
                    </FormLabel>
                  )}
                </>
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
                保存配置不会测试连接或产生模型费用。
                {kind === "text" &&
                  (draft.mode === "custom"
                    ? "新任务会将故事素材发送到所选服务，按供应商规则计费。"
                    : `当前使用服务器默认：${config.text_runtime === "mock" ? "Mock（不调用外部模型）" : config.text_runtime}。`)}
              </p>
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
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy}
                    onClick={dismiss}
                  >
                    关闭
                  </Button>
                  <Button disabled={busy || !draft.dirty}>
                    {busy && <Spinner aria-hidden="true" />}
                    {busy ? "保存中…" : "保存此服务"}
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
