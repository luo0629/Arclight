import "client-only";

import { api, json } from "@/lib/api/client";

export type ServiceKind = "text" | "image" | "video" | "speech";

export type ServicePreset = {
  id: string;
  kind: ServiceKind;
  name: string;
  group: "domestic" | "international";
  description: string;
  base_url: string;
  protocol: string;
};

export type CapabilityCheck = {
  status: "ready" | "partial" | "unverified";
  tools: boolean;
  max_input_images: number;
  can_check: boolean;
};

export type ServiceProfile = {
  kind: ServiceKind;
  supplier: string;
  supplier_name: string;
  protocol: string;
  base_url: string;
  model: string;
  has_key: boolean;
  revision: number;
  key_status?: string;
  capability_check?: CapabilityCheck;
};

export type ConnectionResult = {
  status: "model_found" | "reachable" | "failed";
  message: string;
};

type ModelPreset = {
  supplier: string;
  model: string;
  name: string;
};
type SpeechModelPreset = ModelPreset & { description: string };
type VisualModelPreset = ModelPreset & { kind: "image" | "video" };

export type ServiceConfiguration = {
  services: ServiceProfile[];
  speech_models?: SpeechModelPreset[];
  text_models?: ModelPreset[];
  visual_models: VisualModelPreset[];
  suppliers: ServicePreset[];
  protocols: Record<ServiceKind, string[]>;
  can_store_keys: boolean;
  key_storage: { code: string; message: string };
};

type ConnectionInput = {
  supplier: string;
  protocol?: string;
  base_url?: string;
  model: string;
  api_key: string | null;
  expected_revision: number;
};

type SaveInput = ConnectionInput & {
  supplier_name: string;
  clear_key: boolean;
};

const modelServicesPath = "/me/model-services";

export function readServiceConfiguration() {
  return api<ServiceConfiguration>(modelServicesPath);
}

export function checkServiceConnection(
  kind: ServiceKind,
  input: ConnectionInput,
  signal: AbortSignal,
) {
  return api<ConnectionResult>(`${modelServicesPath}/${kind}/connection`, {
    ...json("POST", input),
    signal,
    cache: "no-store",
  });
}

export function saveServiceConfiguration(kind: ServiceKind, input: SaveInput) {
  return api<ServiceProfile>(
    `${modelServicesPath}/${kind}`,
    json("PUT", input),
  );
}

export function verifyTextCapabilities(revision: number) {
  return api<ServiceProfile>(
    `${modelServicesPath}/text/capabilities`,
    json("POST", { expected_revision: revision, confirm_charges: true }),
  );
}
