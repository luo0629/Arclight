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
  base_url: string;
  model: string;
  has_key: boolean;
  revision: number;
  key_status?: string;
};
type Draft = Profile & { api_key: string; clear_key: boolean; dirty: boolean };
type Configuration = {
  services: Profile[];
  suppliers: {
    id: string;
    name: string;
    base_url: string;
    model: string;
    response_format: ResponseFormat;
  }[];
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
    void api<Configuration>("/me/model-services")
      .then((result) => {
        if (!live) return;
        const next = initialDrafts();
        for (const profile of result.services)
          next[profile.kind] = { ...empty(profile.kind), ...profile };
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
  const presets: ServicePreset[] =
    kind === "text"
      ? (config?.suppliers.map((supplier) => ({
          ...supplier,
          kind: "text" as const,
          group: ["deepseek", "zhipu"].includes(supplier.id)
            ? ("domestic" as const)
            : ("international" as const),
          description: supplier.model,
          models: [supplier.model],
        })) ?? [])
      : [];
  const preset = presets.find((row) => row.id === draft.supplier);
  const stored = config?.services.find((row) => row.kind === kind);
  const hasStoredKey =
    !!stored?.has_key &&
    stored.supplier === draft.supplier &&
    endpoint(stored.base_url) === endpoint(draft.base_url);

  function change(patch: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [kind]: { ...current[kind], ...patch, dirty: true },
    }));
    setSaved("");
    setError("");
    setConfirmClose(false);
  }
  function selectProvider(id: string) {
    if (id === "server") {
      change({ mode: "server" });
      return;
    }
    const next = presets.find((row) => row.id === id);
    if (id === draft.supplier) {
      change({ mode: kind === "text" ? "custom" : "server" });
      return;
    }
    const previous = stored?.supplier === id ? stored : null;
    change({
      supplier: id,
      supplier_name: next?.name ?? previous?.supplier_name ?? "",
      mode: kind === "text" ? "custom" : "server",
      base_url: next?.base_url ?? previous?.base_url ?? "",
      model: previous?.model || next?.models[0] || "",
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
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const profile = await api<Profile>(
        "/me/model-services/" + kind,
        json("PUT", {
          mode: draft.mode,
          supplier: draft.supplier,
          supplier_name: draft.supplier_name,
          response_format: draft.response_format,
          base_url: draft.base_url,
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
          (kind === "text"
            ? draft.mode === "custom"
              ? "新建的文字任务将使用此服务。"
              : "文字任务使用服务器默认服务。"
            : "生成链路尚未开放，本次只保存预设。"),
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
      className="flex max-h-[90dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-5xl"
    >
      <header className="flex shrink-0 items-start justify-between gap-4 border-b px-5 py-4 sm:px-6">
        <div className="flex items-start gap-3">
          <Settings2 className="mt-1 size-5 text-primary" aria-hidden="true" />
          <div>
            <h2 id="service-settings-title" className="text-lg">
              模型与服务
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              选择服务商，填入密钥。配置仅属于你的账号。
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
          <aside className="shrink-0 border-b bg-muted/50 p-3 md:w-44 md:border-r md:border-b-0 md:p-4">
            <ToggleGroup
              type="single"
              value={kind}
              onValueChange={(value) => {
                if (!value) return;
                setKind(value as Kind);
                setError("");
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
                  className="h-10 w-full justify-start gap-2 rounded-lg px-3 text-sm data-[state=on]:bg-accent/60"
                >
                  <service.icon className="size-4 shrink-0" />
                  {service.label}
                  {drafts[service.kind].dirty ? (
                    <span
                      className="ml-auto size-1.5 rounded-full bg-primary"
                      aria-label="未保存"
                    />
                  ) : null}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <p className="mt-5 hidden text-xs leading-relaxed text-muted-foreground md:block">
              每类服务单独保存，
              <br />
              创作任务使用发起者的配置。
            </p>
          </aside>
          <div className="min-w-0 flex-1 space-y-6 p-4 sm:p-6 md:max-h-[calc(90dvh-87px)] md:overflow-y-auto">
            <div className="rounded-lg border bg-sky/50 px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
              {kind === "text"
                ? "选择预设将自动填入服务地址与推荐模型，只需填写对应密钥即可保存使用。"
                : "填写服务地址与模型。仅保存预设 · 尚未接入生成，不会调用供应商。"}
            </div>
            <ServiceProviders
              kind={kind}
              presets={presets}
              value={
                kind === "text" && draft.mode === "server"
                  ? "server"
                  : draft.supplier
              }
              disabled={busy}
              onSelect={selectProvider}
            />
            <form
              onSubmit={save}
              className="flex min-w-0 flex-col gap-4 border-t pt-5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {preset?.name ?? "自定义服务"}配置
                </h3>
                <span className="text-xs text-muted-foreground">
                  {kind !== "text"
                    ? "仅保存预设"
                    : draft.mode === "server"
                      ? "个人预设未启用"
                      : "用于新文字任务"}
                </span>
              </div>
              {kind === "text" && !preset && (
                <FormLabel>
                  文字任务服务
                  <ChoiceSelect
                    aria-label="文字任务服务"
                    value={draft.mode}
                    disabled={busy}
                    onValueChange={(mode) =>
                      change({ mode: mode as Profile["mode"] })
                    }
                    options={[
                      { value: "server", label: "使用服务器默认服务" },
                      { value: "custom", label: "使用我的服务" },
                    ]}
                  />
                </FormLabel>
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
                    change({ api_key: event.target.value, clear_key: false })
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
                      change({ clear_key: !draft.clear_key, api_key: "" })
                    }
                  >
                    {draft.clear_key ? "保留原密钥" : "清除已存密钥"}
                  </Button>
                )}
              </div>
              <div className="grid min-w-0 gap-4 sm:grid-cols-2">
                <FormLabel>
                  服务地址
                  <Input
                    aria-label="服务地址"
                    type="url"
                    placeholder="https://provider.example/v1"
                    maxLength={500}
                    value={draft.base_url}
                    readOnly={!!preset}
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
                    {preset
                      ? "已按所选服务商填写；其他地址请选择自定义。"
                      : "填写服务的 API 基础地址，使用公网 HTTPS。"}
                  </span>
                </FormLabel>
                <FormLabel>
                  模型名称
                  <Input
                    aria-label="模型名称"
                    placeholder="供应商提供的模型 ID"
                    maxLength={160}
                    value={draft.model}
                    disabled={busy}
                    onChange={(event) => change({ model: event.target.value })}
                  />
                  <span className="text-xs font-normal text-muted-foreground">
                    可按账号权限修改为其他模型 ID。
                  </span>
                </FormLabel>
              </div>
              {preset && preset.models.length > 1 && (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  推荐模型
                  {preset.models.map((model) => (
                    <Button
                      key={model}
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-7 max-w-full text-xs"
                      disabled={busy}
                      onClick={() => change({ model })}
                    >
                      <span className="truncate">{model}</span>
                    </Button>
                  ))}
                </div>
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
              {!config.can_store_keys && (
                <Notice tone="info">
                  {config.key_storage.message}
                  。可先保存地址与模型，请联系管理员恢复密钥存储后再填写密钥。
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
