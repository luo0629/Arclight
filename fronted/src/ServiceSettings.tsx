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
import Dialog from "./Dialog";
import { api, json, message } from "./api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormLabel, Notice, LoadingState } from "@/components/ui-kit";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";

type Kind = "text" | "image" | "video" | "speech";
type Profile = {
  kind: Kind;
  mode: "server" | "custom";
  base_url: string;
  model: string;
  has_key: boolean;
  revision: number;
};
type Draft = Profile & { api_key: string; clear_key: boolean; dirty: boolean };
type Configuration = {
  services: Profile[];
  can_store_keys: boolean;
  text_runtime: string;
  allowed_hosts: string[];
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
  api_key: "",
  clear_key: false,
  dirty: false,
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
  function change(patch: Partial<Draft>) {
    setDrafts((current) => ({
      ...current,
      [kind]: { ...current[kind], ...patch, dirty: true },
    }));
    setSaved("");
    setError("");
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
          api_key: draft.api_key || null,
          clear_key: draft.clear_key,
          expected_revision: draft.revision,
        }),
      );
      setDrafts((current) => ({
        ...current,
        [kind]: { ...empty(kind), ...profile },
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
                <NativeSelect
                  value={draft.mode}
                  disabled={busy}
                  onChange={(event) =>
                    change({ mode: event.target.value as "server" | "custom" })
                  }
                >
                  <NativeSelectOption value="server">
                    使用服务器默认服务
                  </NativeSelectOption>
                  <NativeSelectOption value="custom">
                    启用我的兼容服务（可能产生费用）
                  </NativeSelectOption>
                </NativeSelect>
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
                  onChange={(event) => change({ base_url: event.target.value })}
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
            <FormLabel>
              API Key
              <Input
                type="password"
                autoComplete="new-password"
                maxLength={2000}
                value={draft.api_key}
                disabled={busy || !config.can_store_keys}
                placeholder={
                  draft.has_key && !draft.clear_key
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
                {draft.clear_key
                  ? "保存后将清除密钥"
                  : draft.has_key
                    ? "密钥已加密保存"
                    : "尚未保存密钥"}
              </span>
              {draft.has_key && (
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
                服务器未启用密钥加密存储。可先保存地址与模型；请管理员设置
                ARCLIGHT_SERVICE_ENCRYPTION_KEY 后再填写密钥。
              </Notice>
            )}
            {kind === "text" && draft.mode === "custom" && (
              <Notice tone="info">
                启用后，新文字任务会把故事素材发送到此服务，并可能产生费用。支持
                Chat Completions / JSON Schema；允许的域名：
                {config.allowed_hosts.join("、") || "暂未配置"}。
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
