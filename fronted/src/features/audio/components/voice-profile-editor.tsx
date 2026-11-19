"use client";

import { useCallback, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Notice } from "@/components/ui-kit";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "@/features/rendering/hooks/use-render-mutation";
import { audioApi } from "../api";
import type { AudioVoice, VoiceProfile, VoiceProfilePlan } from "../types";
import useAudioPage from "../use-audio-page";
import { AudioFeedback, AudioPagination } from "./audio-fields";

const voiceId = (row: AudioVoice) => row.id;
type ChangeDraft = {
  key: string;
  start: string;
  end: string;
  voice: string;
  reason: string;
};

export default function VoiceProfileEditor({
  projectId,
  characterId,
  profile,
  selected,
  onSaved,
  unavailable,
}: {
  projectId: string;
  characterId: string;
  profile: VoiceProfile | null;
  selected: AudioVoice | null;
  onSaved: () => void;
  unavailable: (cause: unknown) => void;
}) {
  const scope = `${projectId}:voice-profile:${characterId}`;
  const fetchVoices = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.characterVoices(projectId, characterId, offset, signal),
    [projectId, characterId],
  );
  const voices = useAudioPage(scope, fetchVoices, voiceId, unavailable);
  const fetchLedger = useCallback(
    (signal: AbortSignal) => audioApi.voiceLedger(projectId, signal),
    [projectId],
  );
  const ledger = useRenderDetail(scope, fetchLedger, unavailable);
  const approvedLedger = ledger.data?.approved_version_id;
  const pinnedLedger = profile?.plan.ledger_version_id;
  const [voice, setVoice] = useState(
    profile?.plan.default_voice_version_id ?? "",
  );
  const [ledgerId, setLedgerId] = useState("");
  const [changes, setChanges] = useState<ChangeDraft[]>(() =>
    (profile?.plan.changes ?? []).map((change) => ({
      key: crypto.randomUUID(),
      start: String(change.story_start),
      end: change.story_end === null ? "" : String(change.story_end),
      voice: change.voice_version_id,
      reason: change.reason,
    })),
  );
  const currentLedger = ledgerId || pinnedLedger || approvedLedger || "";
  const defaultVoice =
    voice ||
    (selected?.character_asset_id === characterId ? selected.id : "") ||
    voices.rows[0]?.id ||
    "";
  const options = voices.rows.map((row) => ({
    value: row.id,
    label: `v${row.version_no} · ${String(row.parameters.voice ?? row.model)}`,
  }));
  const referencedVoices = new Set(
    [defaultVoice, ...changes.map((change) => change.voice)].filter(Boolean),
  );
  for (const id of referencedVoices) {
    if (!options.some((entry) => entry.value === id))
      options.push({ value: id, label: `已保存的音色 · ${id.slice(0, 8)}` });
  }
  const plan: VoiceProfilePlan = {
    ledger_version_id: currentLedger,
    default_voice_version_id: defaultVoice,
    changes: changes.map((change) => ({
      story_start: Number(change.start),
      story_end: change.end ? Number(change.end) : null,
      voice_version_id: change.voice,
      reason: change.reason.trim(),
    })),
  };
  const mutation = useRenderMutation(scope, true, unavailable);
  const revision = profile?.version_no ?? 0;
  function updateChange(key: string, patch: Partial<ChangeDraft>) {
    setChanges((rows) =>
      rows.map((row) => (row.key === key ? { ...row, ...patch } : row)),
    );
  }
  async function save() {
    const pending = pendingCreationKey(
      `${scope}:${revision}:${JSON.stringify(plan)}`,
    );
    await mutation.mutate(
      (signal) =>
        audioApi.saveVoiceProfile(
          projectId,
          characterId,
          {
            ...plan,
            creation_key: pending.key,
            expected_version_no: revision,
          },
          signal,
        ),
      () => {
        pending.clear();
        onSaved();
      },
      "声音档案已保存为新版本。",
    );
  }
  return (
    <form
      aria-label="跨集声音档案"
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-3"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <Field>
        <FieldLabel htmlFor="series-default-voice">跨集默认音色</FieldLabel>
        <ChoiceSelect
          id="series-default-voice"
          value={defaultVoice}
          placeholder="选择角色音色"
          options={options}
          onValueChange={setVoice}
        />
      </Field>
      <AudioPagination list={voices} label="角色音色" />
      {!voices.loading && options.length === 0 && (
        <Notice>先在本句音色面板保存一个角色音色。</Notice>
      )}
      <Field>
        <FieldLabel htmlFor="series-voice-ledger">剧情时点来源</FieldLabel>
        <ChoiceSelect
          id="series-voice-ledger"
          value={currentLedger}
          placeholder="需要已批准的故事账本"
          options={[
            ...(pinnedLedger && pinnedLedger !== approvedLedger
              ? [{ value: pinnedLedger, label: "档案已固定的故事账本" }]
              : []),
            ...(approvedLedger
              ? [{ value: approvedLedger, label: "当前已批准故事账本" }]
              : []),
          ]}
          onValueChange={setLedgerId}
        />
      </Field>
      {changes.map((change, index) => (
        <fieldset
          key={change.key}
          className="grid min-w-0 gap-2 rounded border p-3 sm:grid-cols-2"
        >
          <legend className="px-1 text-sm font-medium">
            剧情变声 {index + 1}
          </legend>
          <Field>
            <FieldLabel htmlFor={`voice-start-${change.key}`}>
              故事开始时点
            </FieldLabel>
            <Input
              id={`voice-start-${change.key}`}
              type="number"
              required
              step={1}
              value={change.start}
              onChange={(event) =>
                updateChange(change.key, { start: event.target.value })
              }
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`voice-end-${change.key}`}>
              恢复默认音色时点
            </FieldLabel>
            <Input
              id={`voice-end-${change.key}`}
              type="number"
              step={1}
              value={change.end}
              onChange={(event) =>
                updateChange(change.key, { end: event.target.value })
              }
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`voice-change-${change.key}`}>
              变声音色
            </FieldLabel>
            <ChoiceSelect
              id={`voice-change-${change.key}`}
              value={change.voice}
              placeholder="选择变声音色"
              options={options}
              onValueChange={(value) =>
                updateChange(change.key, { voice: value })
              }
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`voice-reason-${change.key}`}>
              剧情原因
            </FieldLabel>
            <Input
              id={`voice-reason-${change.key}`}
              required
              maxLength={2000}
              value={change.reason}
              onChange={(event) =>
                updateChange(change.key, { reason: event.target.value })
              }
            />
          </Field>
          <Button
            type="button"
            variant="ghost"
            className="justify-self-start"
            onClick={() =>
              setChanges((rows) => rows.filter((row) => row.key !== change.key))
            }
          >
            移除此变声
          </Button>
        </fieldset>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={changes.length >= 64 || mutation.busy}
          onClick={() =>
            setChanges((rows) => [
              ...rows,
              {
                key: crypto.randomUUID(),
                start: "",
                end: "",
                voice: "",
                reason: "",
              },
            ])
          }
        >
          添加剧情变声
        </Button>
        <Button
          type="submit"
          disabled={
            !defaultVoice ||
            !currentLedger ||
            ledger.loading ||
            voices.loading ||
            mutation.busy ||
            changes.some((change) => !change.voice)
          }
        >
          保存声音档案
        </Button>
      </div>
      <AudioFeedback error={voices.error || ledger.error || mutation.error} />
    </form>
  );
}
