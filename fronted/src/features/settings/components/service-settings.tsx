"use client";

import { useEffect, useState } from "react";
import {
  Type,
  Image,
  Video,
  AudioLines,
  ShieldCheck,
  X,
  Check,
} from "lucide-react";
import Dialog from "@/components/shared/app-dialog";
import { api, json, message } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormLabel, Notice, LoadingState } from "@/components/ui-kit";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { ChoiceSelect } from "@/components/choice-select";
import { Spinner } from "@/components/ui/spinner";

type Kind = "text" | "image" | "video" | "speech";
type Profile = {
  kind: Kind;
  mode: "server" | "custom";
  base_url: string;
  model: string;
  has_key: boolean;
  revision: number;
  supplier: string;
  supplier_name: string;
  response_format: "json_schema" | "json_object";
  key_status?: string;
};
type Draft = Profile & {
  api_key: string;
  clear_key: boolean;
  dirty: boolean;
  saved_supplier: string;
  saved_base_url: string;
};
type Supplier = Pick<Profile, "base_url" | "model" | "response_format"> & {
  id: string;
  name: string;
};
type Configuration = {
  services: Profile[];
  can_store_keys: boolean;
  text_runtime: string;
  suppliers: Supplier[];
  key_storage: { code: string; message: string };
};
const services = [
  {
    kind: "text" as const,
    label: "文字模型",
    icon: Type,
    description: "为编剧与导演选择文字服务。",
  },
  {
    kind: "image" as const,
    label: "图片模型",
    icon: Image,
    description: "保存未来角色参考图、场景和分镜图片的服务预设。",
  },
  {
    kind: "video" as const,
    label: "视频模型",
    icon: Video,
    description: "保存未来镜头视频生成的服务预设。",
  },
  {
    kind: "speech" as const,
    label: "语音服务",
    icon: AudioLines,
    description: "保存未来配音服务的模型与地址。",
  },
];
const empty = (kind: Kind): Draft => ({
  kind,
  mode: "server",
  base_url: "",
  model: "",
  has_key: false,
  revision: 0,
  supplier: "custom",
  supplier_name: "",
  response_format: "json_schema",
  api_key: "",
  clear_key: false,
  dirty: false,
  saved_supplier: "custom",
  saved_base_url: "",
});
const toDraft = (profile: Profile): Draft => ({
  ...empty(profile.kind),
  ...profile,
  saved_supplier: profile.supplier,
  saved_base_url: profile.base_url,
});
export default function ServiceSettings({
  onDismiss,
}: {
  onDismiss: () => void;
}) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [drafts, setDrafts] = useState<Record<Kind, Draft>>({
    text: empty("text"),
    image: empty("image"),
    video: empty("video"),
    speech: empty("speech"),
  });
  const [kind, setKind] = useState<Kind>("text");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [confirmClose, setConfirmClose] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setError("");
    void api<Configuration>("/me/model-services")
      .then((result) => {
        if (!live) return;
        const next = {
          text: empty("text"),
          image: empty("image"),
          video: empty("video"),
          speech: empty("speech"),
        };
        for (const profile of result.services)
          next[profile.kind] = toDraft(profile);
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
  const destinationChanged =
    draft.has_key &&
    (draft.supplier !== draft.saved_supplier ||
      draft.base_url !== draft.saved_base_url);
  function change(patch: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [kind]: { ...current[kind], ...patch, dirty: true },
    }));
    setSaved("");
    setError("");
  }
  function selectSupplier(supplier: string) {
    const preset = config?.suppliers.find((row) => row.id === supplier);
    change({
      mode: "custom",
      supplier,
      supplier_name: preset?.name ?? "",
      ...(preset
        ? {
            base_url: preset.base_url,
            model: preset.model,
            response_format: preset.response_format,
          }
        : {}),
      api_key: "",
      clear_key: draft.has_key,
    });
  }
  function dismiss() {
    if (Object.values(drafts).some((row) => row.dirty)) setConfirmClose(true);
    else onDismiss();
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSaved("");
    try {
      const profile = await api<Profile>(
        "/me/model-services/" + kind,
        json("PUT", {
          mode: draft.mode,
          base_url: draft.base_url,
          model: draft.model,
          supplier: draft.supplier,
          supplier_name: draft.supplier_name,
          response_format: draft.response_format,
          api_key: draft.api_key || null,
          clear_key: draft.clear_key,
          expected_revision: draft.revision,
        }),
      );
      setDrafts((current) => ({
        ...current,
        [kind]: toDraft(profile),
      }));
      setSaved(
        "已保存到当前账号。" +
          (kind === "text"
            ? "仅对新建的文字任务生效，现有任务不变。"
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
      className="sm:max-w-3xl"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 id="service-settings-title">模型与服务</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            把创作工具准备好，灵感来时就能出发。
          </p>
        </div>
        <Button
          variant="ghost"
          size="icon"
          aria-label="关闭模型配置"
          disabled={busy}
          onClick={dismiss}
        >
          <X />
        </Button>
      </div>
      {!config ? (
        <>
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
        </>
      ) : (
        <>
          <ToggleGroup
            type="single"
            value={kind}
            onValueChange={(value) => {
              if (value) {
                setKind(value as Kind);
                setError("");
                setSaved("");
              }
            }}
            aria-label="服务类型"
            className="grid grid-cols-2 gap-2 rounded-2xl bg-muted p-2 sm:grid-cols-4"
            disabled={busy}
          >
            {services.map((service) => (
              <ToggleGroupItem
                key={service.kind}
                value={service.kind}
                className="gap-2 rounded-xl py-5 data-[state=on]:bg-card"
              >
                <service.icon className="size-4" />
                {service.label}
                {drafts[service.kind].dirty && (
                  <span
                    className="size-1.5 rounded-full bg-primary"
                    aria-label="未保存"
                  />
                )}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <form onSubmit={save} className="flex min-w-0 flex-col gap-5">
            <div className="rounded-2xl border bg-background/60 p-4 text-sm">
              <p>{services.find((row) => row.kind === kind)?.description}</p>
              {kind !== "text" ? (
                <p className="mt-1 text-muted-foreground">
                  仅保存预设 · 尚未接入生成，不会调用供应商。
                </p>
              ) : (
                <p className="mt-1 text-muted-foreground">
                  服务器默认：
                  {config.text_runtime === "mock"
                    ? "Mock（不调用外部模型）"
                    : config.text_runtime}
                  。保存设置不会发起生成或连接测试。
                </p>
              )}
            </div>
            {kind === "text" && (
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
                    {
                      value: "custom",
                      label: "启用我的兼容服务（可能产生费用）",
                    },
                  ]}
                />
              </FormLabel>
            )}
            {kind === "text" && (
              <FormLabel>
                模型供应商
                <ChoiceSelect
                  aria-label="模型供应商"
                  value={draft.supplier}
                  disabled={busy}
                  onValueChange={selectSupplier}
                  options={[
                    ...config.suppliers.map((row) => ({
                      value: row.id,
                      label: row.name,
                    })),
                    { value: "custom", label: "自定义供应商" },
                  ]}
                />
              </FormLabel>
            )}
            {draft.supplier === "custom" && (
              <FormLabel>
                供应商名称（选填）
                <Input
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
            <div className="grid min-w-0 gap-5 sm:grid-cols-2">
              <FormLabel>
                服务地址
                <Input
                  type="url"
                  placeholder="https://provider.example/v1"
                  maxLength={500}
                  value={draft.base_url}
                  disabled={busy}
                  readOnly={draft.supplier !== "custom"}
                  onChange={(event) =>
                    change({
                      base_url: event.target.value,
                      api_key: "",
                      clear_key: draft.has_key,
                    })
                  }
                />
              </FormLabel>
              <FormLabel>
                模型名称
                <Input
                  placeholder={
                    kind === "text" ? "供应商提供的模型 ID" : "待接入的模型 ID"
                  }
                  maxLength={160}
                  value={draft.model}
                  disabled={busy}
                  onChange={(event) => change({ model: event.target.value })}
                />
              </FormLabel>
            </div>
            {kind === "text" && draft.supplier === "custom" && (
              <FormLabel>
                结构化输出格式
                <ChoiceSelect
                  aria-label="结构化输出格式"
                  value={draft.response_format}
                  disabled={busy}
                  onValueChange={(value) =>
                    change({
                      response_format: value as Profile["response_format"],
                    })
                  }
                  options={[
                    { value: "json_schema", label: "JSON Schema" },
                    { value: "json_object", label: "JSON 对象" },
                  ]}
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
                  draft.has_key && !draft.clear_key && !destinationChanged
                    ? "已保存密钥；留空保持不变"
                    : "输入密钥（不会回显）"
                }
                onChange={(event) =>
                  change({ api_key: event.target.value, clear_key: false })
                }
              />
            </FormLabel>
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>
                {destinationChanged
                  ? "供应商或地址已更改，请重新填写 API Key"
                  : draft.clear_key
                    ? "保存后将清除密钥"
                    : draft.has_key
                      ? "密钥已加密保存"
                      : "尚未保存密钥"}
              </span>
              {draft.has_key && !destinationChanged && (
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
            {!config.can_store_keys && (
              <Notice tone="info">
                {config.key_storage.message}
                。可先保存地址与模型，请联系管理员恢复密钥存储后再填写密钥。
              </Notice>
            )}
            {kind === "text" && draft.mode === "custom" && (
              <Notice tone="info">
                启用后，新文字任务会把故事素材发送到此服务，并可能产生费用。支持
                Chat Completions /{" "}
                {draft.response_format === "json_schema"
                  ? "JSON Schema"
                  : "JSON 对象"}
                ；自定义地址支持公网 HTTPS 服务，无需申请域名白名单。
              </Notice>
            )}
            {draft.has_key &&
              draft.key_status &&
              !["READY", "MISSING"].includes(draft.key_status) && (
                <Notice tone="error">
                  已存凭证暂时不可读取，请联系管理员检查主密钥，或重新填写 API
                  Key 后保存。
                </Notice>
              )}
            {error && (
              <Notice tone="error" role="alert">
                {error}
              </Notice>
            )}
            {saved && (
              <Notice tone="success" role="status">
                <Check className="inline size-4" /> {saved}
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
        </>
      )}
      {confirmClose && (
        <Notice tone="info" role="alert">
          有尚未保存的服务配置，要放弃并关闭吗？
          <div className="mt-2 flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setConfirmClose(false)}>
              继续编辑配置
            </Button>
            <Button variant="destructive" onClick={onDismiss}>
              放弃配置并关闭
            </Button>
          </div>
        </Notice>
      )}
    </Dialog>
  );
}
