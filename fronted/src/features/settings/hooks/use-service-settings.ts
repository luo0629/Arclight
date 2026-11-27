import { useEffect, useRef, useState } from "react";
import { message } from "@/lib/api/client";
import {
  checkServiceConnection,
  readServiceConfiguration,
  saveServiceConfiguration,
  verifyTextCapabilities,
  type ConnectionResult,
  type ServiceConfiguration,
  type ServiceKind,
  type ServiceProfile,
} from "../service-settings-api";

export type ServiceDraft = ServiceProfile & {
  api_key: string;
  clear_key: boolean;
  dirty: boolean;
};

const empty = (kind: ServiceKind): ServiceDraft => ({
  kind,
  supplier: "custom",
  supplier_name: "",
  protocol: "",
  base_url: "",
  model: "",
  has_key: false,
  revision: 0,
  api_key: "",
  clear_key: false,
  dirty: false,
});

const initialDrafts = (): Record<ServiceKind, ServiceDraft> => ({
  text: empty("text"),
  image: empty("image"),
  video: empty("video"),
  speech: empty("speech"),
});

const endpoint = (url: string) => url.trim().replace(/\/+$/, "");

export default function useServiceSettings() {
  const [config, setConfig] = useState<ServiceConfiguration | null>(null);
  const [drafts, setDrafts] = useState(initialDrafts);
  const [kind, setKind] = useState<ServiceKind>("text");
  const [pendingAction, setPendingAction] = useState<"save" | "verify" | null>(
    null,
  );
  const busy = pendingAction !== null;
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [connection, setConnection] = useState<
    ConnectionResult | "checking" | null
  >(null);
  const probe = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      probe.current?.abort();
      probe.current = null;
    },
    [],
  );
  useEffect(() => {
    let live = true;
    setError("");
    void readServiceConfiguration()
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
              official?.protocol ||
              result.protocols[profile.kind][0],
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
  const speechModels =
    kind === "speech"
      ? (config?.speech_models ?? []).filter(
          (row) => row.supplier === draft.supplier,
        )
      : [];
  const speechModel = speechModels.find((row) => row.model === draft.model);
  const modelSuggestions = (
    kind === "text"
      ? (config?.text_models ?? [])
      : (config?.visual_models ?? []).filter((row) => row.kind === kind)
  ).filter((row) => row.supplier === preset?.id);
  const stored = config?.services.find((row) => row.kind === kind);
  const hasStoredKey =
    !!stored?.has_key &&
    stored.supplier === draft.supplier &&
    endpoint(stored.base_url) === endpoint(draft.base_url) &&
    stored.protocol === draft.protocol;
  const capability =
    kind === "text" && !draft.dirty ? stored?.capability_check : undefined;
  const keyReady = hasStoredKey && stored?.key_status === "READY";
  const canVerify = !!capability?.can_check && keyReady;
  const checking = connection === "checking";
  const canCheck =
    !!draft.model.trim() &&
    (preset || !!draft.base_url.trim()) &&
    !draft.clear_key &&
    (!!draft.api_key.trim() || hasStoredKey);
  const draftInput = {
    supplier: draft.supplier,
    protocol: draft.protocol || config?.protocols[kind][0],
    base_url: preset ? undefined : draft.base_url,
    model: draft.model,
    api_key: draft.api_key || null,
    expected_revision: draft.revision,
  };

  function retryLoad() {
    setAttempt((current) => current + 1);
  }

  function resetConnection() {
    probe.current?.abort();
    probe.current = null;
    setConnection(null);
  }

  function clearActionFeedback() {
    resetConnection();
    setError("");
    setSaved("");
  }

  function changeDraft(patch: Partial<ServiceDraft>) {
    setDrafts((current) => ({
      ...current,
      [kind]: { ...current[kind], ...patch, dirty: true },
    }));
    clearActionFeedback();
  }

  function selectKind(value: ServiceKind) {
    setKind(value);
    clearActionFeedback();
  }

  function selectProvider(id: string) {
    const next = presets.find((row) => row.id === id);
    const previous = stored?.supplier === id ? stored : null;
    changeDraft({
      supplier: id,
      supplier_name: next?.name ?? previous?.supplier_name ?? "",
      protocol:
        next?.protocol ??
        previous?.protocol ??
        config?.protocols[kind][0] ??
        "",
      base_url: next?.base_url ?? previous?.base_url ?? "",
      model: previous?.model ?? "",
      api_key: "",
      clear_key: false,
    });
  }

  async function checkConnection() {
    if (!canCheck || busy || probe.current) return;
    const request = new AbortController();
    probe.current = request;
    setConnection("checking");
    try {
      const result = await checkServiceConnection(
        kind,
        draftInput,
        request.signal,
      );
      if (probe.current === request) setConnection(result);
    } catch (err) {
      if (probe.current === request)
        setConnection({ status: "failed", message: message(err) });
    } finally {
      if (probe.current === request) probe.current = null;
    }
  }

  function applyProfile(profile: ServiceProfile) {
    setDrafts((current) => ({
      ...current,
      [profile.kind]: { ...empty(profile.kind), ...profile },
    }));
    setConfig((current) =>
      current
        ? {
            ...current,
            services: [
              ...current.services.filter((row) => row.kind !== profile.kind),
              profile,
            ],
          }
        : current,
    );
  }

  async function verifyCapabilities() {
    if (!canVerify || !stored || busy) return;
    clearActionFeedback();
    setPendingAction("verify");
    try {
      applyProfile(await verifyTextCapabilities(stored.revision));
    } catch (err) {
      setError(message(err));
    } finally {
      setPendingAction(null);
    }
  }

  async function saveDraft() {
    clearActionFeedback();
    setPendingAction("save");
    try {
      const profile = await saveServiceConfiguration(kind, {
        ...draftInput,
        supplier_name: draft.supplier_name,
        clear_key: draft.clear_key,
      });
      applyProfile(profile);
      setSaved(
        draft.clear_key
          ? "已清除 API Key。重新配置此服务后才能用于新任务。"
          : "已保存到当前账号。新建的对应任务将使用此配置，已有任务保留原配置。",
      );
    } catch (err) {
      setError(message(err));
    } finally {
      setPendingAction(null);
    }
  }

  return {
    config,
    drafts,
    kind,
    draft,
    presets,
    preset,
    speechModels,
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
  };
}
