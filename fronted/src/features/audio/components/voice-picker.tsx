"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "@/features/rendering/hooks/use-render-mutation";
import { runErrorMessage } from "@/lib/run-errors";
import { audioApi } from "../api";
import type { AudioVoice } from "../types";
import useAudioPage from "../use-audio-page";
import { AudioFeedback, AudioPagination } from "./audio-fields";

const voiceId = (row: AudioVoice) => row.id;

const catalogStatusMessage = {
  NOT_CONFIGURED: "请先配置语音服务",
  UNAVAILABLE:
    "当前配音模型没有可用的音色目录。请在「模型服务配置」中更换模型。",
  QUERY_FAILED: "音色目录查询失败。请检查配音服务后重新读取。",
};
export default function VoicePicker({
  projectId,
  speaker,
  characterId,
  profiled = false,
  saveOnly = false,
  scope,
  writable,
  selected = null,
  onSelected,
  unavailable,
}: {
  projectId: string;
  speaker: string;
  characterId: string | null;
  profiled?: boolean;
  saveOnly?: boolean;
  scope: string;
  writable: boolean;
  selected?: AudioVoice | null;
  onSelected: (voice: AudioVoice) => void;
  unavailable: (cause: unknown) => void;
}) {
  const fieldId = useId();
  const selectable = !profiled && !saveOnly;
  const fetchVoices = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.voices(projectId, speaker, offset, signal),
    [projectId, speaker],
  );
  const voices = useAudioPage(scope, fetchVoices, voiceId, unavailable);
  const [formOpen, setFormOpen] = useState(false);
  const fetchCatalog = useCallback(
    (signal: AbortSignal) => audioApi.voiceCatalog(projectId, signal),
    [projectId],
  );
  const catalog = useRenderDetail(
    scope,
    fetchCatalog,
    unavailable,
    writable && formOpen,
  );
  const [choice, setChoice] = useState<{
    version: string;
    voice: string;
  } | null>(null);
  const [speed, setSpeed] = useState("");
  const [instructions, setInstructions] = useState("");
  const [language, setLanguage] = useState("");
  const data = catalog.data;
  const catalogVersion = data?.configuration_version;
  const options = data?.status === "READY" ? data.options : [];
  const selectedOption =
    choice?.version === catalogVersion
      ? options.find((option) => option.id === choice?.voice)
      : undefined;
  const supported = data?.supported_parameters ?? [];
  const speedRange = data?.parameter_ranges.speed;
  const parameters: Record<string, string | number> = {
    voice: selectedOption?.id ?? "",
  };
  if (speedRange && speed) parameters.speed = Number(speed);
  if (supported.includes("instructions") && instructions.trim())
    parameters.instructions = instructions.trim();
  if (supported.includes("language_type") && language.trim())
    parameters.language_type = language.trim();
  const creationScope = `${scope}:voice:${catalogVersion ?? ""}:${JSON.stringify(parameters)}`;
  const mutation = useRenderMutation(creationScope, writable, unavailable);
  const canSave =
    writable &&
    !mutation.busy &&
    !voices.loading &&
    !voices.error &&
    !catalog.loading &&
    !!selectedOption &&
    !!catalogVersion;
  useEffect(() => {
    if (!saveOnly && !selected && voices.rows[0]) onSelected(voices.rows[0]);
  }, [saveOnly, selected, voices.rows, onSelected]);
  const versionNo = Math.max(0, ...voices.rows.map((row) => row.version_no));
  async function save() {
    if (!canSave || !catalogVersion) return;
    const pending = pendingCreationKey(creationScope);
    await mutation.mutate(
      (signal) =>
        audioApi.createVoice(
          projectId,
          {
            creation_key: pending.key,
            speaker,
            ...(characterId ? { character_asset_id: characterId } : {}),
            expected_version_no: versionNo,
            model_config_version: catalogVersion,
            parameters,
            output_spec: {},
          },
          signal,
        ),
      (row) => {
        pending.clear();
        voices.prepend(row);
        onSelected(row);
      },
      "音色已保存为新版本，已有配音与旧音色保持原样。",
    );
  }
  return (
    <section
      aria-label={saveOnly ? `${speaker}的音色` : "本句音色"}
      className="flex min-w-0 flex-col gap-3"
    >
      {selectable && (
        <Field>
          <FieldLabel htmlFor={`${fieldId}-version`}>
            {speaker}的音色版本
          </FieldLabel>
          <ChoiceSelect
            id={`${fieldId}-version`}
            value={selected?.id ?? ""}
            placeholder="选择已保存音色"
            disabled={!writable || voices.loading || mutation.busy}
            options={voices.rows.map((row) => ({
              value: row.id,
              label: `${row.speaker} · v${row.version_no} · ${String(row.parameters.voice ?? "未标记音色")} · ${row.model}`,
            }))}
            onValueChange={(value) => {
              const row = voices.rows.find((voice) => voice.id === value);
              if (row) onSelected(row);
            }}
          />
        </Field>
      )}
      {selectable && <AudioPagination list={voices} label="音色版本" />}
      {selectable && selected && (
        <p className="text-xs text-muted-foreground">
          {selected.model} · 音色 v{selected.version_no}
        </p>
      )}
      {writable && (
        <details
          className="min-w-0 rounded-lg border p-3"
          onToggle={(event) => setFormOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer text-sm font-medium">
            为{speaker}保存新音色
          </summary>
          <form
            className="mt-4 flex min-w-0 flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            {saveOnly && voices.error && (
              <Notice tone="error">
                {voices.error}
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => void voices.load()}
                >
                  重新读取音色版本
                </Button>
              </Notice>
            )}
            {catalog.loading && <p role="status">正在读取音色目录…</p>}
            {catalog.error && <Notice tone="error">{catalog.error}</Notice>}
            {data?.status === "READY" && options.length === 0 && (
              <Notice>当前配音模型的音色目录为空。</Notice>
            )}
            {data && data.status !== "READY" && (
              <Notice tone={data.status === "QUERY_FAILED" ? "error" : "info"}>
                {catalogStatusMessage[data.status]}
                {data.status === "QUERY_FAILED" && data.error_code && (
                  <p>{runErrorMessage(data.error_code)}</p>
                )}
              </Notice>
            )}
            {data?.model && (
              <p className="text-sm text-muted-foreground">
                {[data.supplier, data.model].filter(Boolean).join(" · ")}
              </p>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="self-start"
              disabled={catalog.loading || mutation.busy}
              onClick={() => void catalog.load()}
            >
              重新读取音色目录
            </Button>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor={`${fieldId}-name`}>供应商音色</FieldLabel>
                <ChoiceSelect
                  id={`${fieldId}-name`}
                  aria-describedby={
                    selectedOption?.description
                      ? `${fieldId}-description`
                      : undefined
                  }
                  value={selectedOption?.id ?? ""}
                  placeholder="选择当前模型支持的音色"
                  disabled={catalog.loading || mutation.busy || !options.length}
                  options={options.map((option) => ({
                    value: option.id,
                    label: option.label,
                  }))}
                  onValueChange={(value) => {
                    if (catalogVersion)
                      setChoice({ version: catalogVersion, voice: value });
                  }}
                />
                {selectedOption?.description && (
                  <FieldDescription id={`${fieldId}-description`}>
                    {selectedOption.description}
                  </FieldDescription>
                )}
              </Field>
              {speedRange && (
                <Field>
                  <FieldLabel htmlFor={`${fieldId}-speed`}>
                    默认语速（可选）
                  </FieldLabel>
                  <Input
                    id={`${fieldId}-speed`}
                    type="number"
                    min={speedRange.minimum}
                    max={speedRange.maximum}
                    step="any"
                    value={speed}
                    disabled={mutation.busy}
                    onChange={(event) => setSpeed(event.target.value)}
                  />
                </Field>
              )}
              {supported.includes("instructions") && (
                <Field>
                  <FieldLabel htmlFor={`${fieldId}-emotion`}>
                    语气与情绪说明（可选）
                  </FieldLabel>
                  <Textarea
                    id={`${fieldId}-emotion`}
                    maxLength={2000}
                    value={instructions}
                    disabled={mutation.busy}
                    onChange={(event) => setInstructions(event.target.value)}
                  />
                </Field>
              )}
              {supported.includes("language_type") && (
                <Field>
                  <FieldLabel htmlFor={`${fieldId}-language`}>
                    发音语言（可选）
                  </FieldLabel>
                  <Input
                    id={`${fieldId}-language`}
                    maxLength={160}
                    value={language}
                    disabled={mutation.busy}
                    onChange={(event) => setLanguage(event.target.value)}
                  />
                </Field>
              )}
            </FieldGroup>
            <Button type="submit" disabled={!canSave}>
              {mutation.busy ? "正在保存音色…" : "保存新音色版本"}
            </Button>
            <AudioFeedback {...mutation} />
          </form>
        </details>
      )}
    </section>
  );
}
