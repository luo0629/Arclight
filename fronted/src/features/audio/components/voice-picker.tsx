"use client";

import { useCallback, useEffect, useState } from "react";
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
import { audioApi } from "../api";
import type { AudioVoice } from "../types";
import useAudioPage from "../use-audio-page";
import { AudioFeedback, AudioPagination } from "./audio-fields";

const voiceId = (row: AudioVoice) => row.id;
export default function VoicePicker({
  projectId,
  speaker,
  scope,
  writable,
  selected,
  onSelected,
  unavailable,
}: {
  projectId: string;
  speaker: string;
  scope: string;
  writable: boolean;
  selected: AudioVoice | null;
  onSelected: (voice: AudioVoice) => void;
  unavailable: (cause: unknown) => void;
}) {
  const fetchVoices = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.voices(projectId, speaker, offset, signal),
    [projectId, speaker],
  );
  const voices = useAudioPage(scope, fetchVoices, voiceId, unavailable);
  const fetchConfiguration = useCallback(
    (signal: AbortSignal) => audioApi.speech(projectId, signal),
    [projectId],
  );
  const configuration = useRenderDetail(scope, fetchConfiguration, unavailable);
  const [voiceName, setVoiceName] = useState("");
  const [speed, setSpeed] = useState("");
  const [instructions, setInstructions] = useState("");
  const [language, setLanguage] = useState("");
  const supported = configuration.data?.supported_parameters ?? [];
  const parameters: Record<string, string | number> = {
    voice: voiceName.trim(),
  };
  if (supported.includes("speed") && speed) parameters.speed = Number(speed);
  if (supported.includes("instructions") && instructions.trim())
    parameters.instructions = instructions.trim();
  if (supported.includes("language_type") && language.trim())
    parameters.language_type = language.trim();
  const creationScope = `${scope}:voice:${JSON.stringify(parameters)}`;
  const mutation = useRenderMutation(creationScope, writable, unavailable);
  useEffect(() => {
    if (!selected && voices.rows[0]) onSelected(voices.rows[0]);
  }, [selected, voices.rows, onSelected]);
  const versionNo = Math.max(0, ...voices.rows.map((row) => row.version_no));
  async function save() {
    if (
      !voiceName.trim() ||
      !configuration.data?.configured ||
      voices.loading ||
      voices.error
    )
      return;
    const pending = pendingCreationKey(creationScope);
    await mutation.mutate(
      (signal) =>
        audioApi.createVoice(
          projectId,
          {
            creation_key: pending.key,
            speaker,
            expected_version_no: versionNo,
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
    <section aria-label="本句音色" className="flex min-w-0 flex-col gap-3">
      <Field>
        <FieldLabel htmlFor="audio-voice-version">
          {speaker}的音色版本
        </FieldLabel>
        <ChoiceSelect
          id="audio-voice-version"
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
      <AudioPagination list={voices} label="音色版本" />
      {selected && (
        <p className="text-xs text-muted-foreground">
          本次使用保存时的 {selected.model} 配置与音色 v{selected.version_no}。
        </p>
      )}
      {writable && (
        <details className="min-w-0 rounded-lg border p-3">
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
            {configuration.error && (
              <Notice tone="error">{configuration.error}</Notice>
            )}
            {configuration.data && (
              <Notice>
                {configuration.data.message}
                {configuration.data.model
                  ? ` · ${configuration.data.model}`
                  : ""}
              </Notice>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={configuration.loading || mutation.busy}
              onClick={() => void configuration.load()}
            >
              重新读取语音服务
            </Button>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="audio-voice-name">
                  供应商音色名称或 ID
                </FieldLabel>
                <Input
                  id="audio-voice-name"
                  value={voiceName}
                  required
                  maxLength={160}
                  disabled={mutation.busy}
                  onChange={(event) => setVoiceName(event.target.value)}
                />
                <FieldDescription>
                  填写所选模型支持的音色。保存不会调用供应商生成配音。
                </FieldDescription>
              </Field>
              {supported.includes("speed") && (
                <Field>
                  <FieldLabel htmlFor="audio-voice-speed">
                    默认语速（可选）
                  </FieldLabel>
                  <Input
                    id="audio-voice-speed"
                    type="number"
                    min={0.25}
                    max={4}
                    step={0.05}
                    value={speed}
                    disabled={mutation.busy}
                    onChange={(event) => setSpeed(event.target.value)}
                  />
                </Field>
              )}
              {supported.includes("instructions") && (
                <Field>
                  <FieldLabel htmlFor="audio-voice-emotion">
                    语气与情绪说明（可选）
                  </FieldLabel>
                  <Textarea
                    id="audio-voice-emotion"
                    maxLength={2000}
                    value={instructions}
                    disabled={mutation.busy}
                    onChange={(event) => setInstructions(event.target.value)}
                  />
                </Field>
              )}
              {supported.includes("language_type") && (
                <Field>
                  <FieldLabel htmlFor="audio-voice-language">
                    发音语言（可选）
                  </FieldLabel>
                  <Input
                    id="audio-voice-language"
                    maxLength={160}
                    value={language}
                    disabled={mutation.busy}
                    onChange={(event) => setLanguage(event.target.value)}
                  />
                </Field>
              )}
            </FieldGroup>
            <Button
              type="submit"
              disabled={
                mutation.busy ||
                voices.loading ||
                !!voices.error ||
                !configuration.data?.configured ||
                !voiceName.trim()
              }
            >
              {mutation.busy ? "正在保存音色…" : "保存新音色版本"}
            </Button>
            <AudioFeedback {...mutation} />
          </form>
        </details>
      )}
    </section>
  );
}
