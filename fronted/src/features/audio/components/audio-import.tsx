"use client";

import { useCallback, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import useRenderMutation, {
  pendingCreationKey,
} from "@/features/rendering/hooks/use-render-mutation";
import { audioApi, uploadRecording } from "../api";
import type { AudioAsset, AudioFile } from "../types";
import useAudioPage from "../use-audio-page";
import {
  AudioFeedback,
  AudioPagination,
  emptyRights,
  RightsFields,
} from "./audio-fields";

const fileId = (row: AudioFile) => row.id;
export default function AudioImport({
  projectId,
  snapshotId,
  lineId,
  scope,
  asset,
  writable,
  onImported,
  unavailable,
}: {
  projectId: string;
  snapshotId: string;
  lineId: string;
  scope: string;
  asset: AudioAsset | null;
  writable: boolean;
  onImported: () => void;
  unavailable: (cause: unknown) => void;
}) {
  const readFiles = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      audioApi.files(projectId, offset, signal),
    [projectId],
  );
  const files = useAudioPage(scope, readFiles, fileId, unavailable);
  const [file, setFile] = useState<File | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [existingId, setExistingId] = useState("");
  const [uploaded, setUploaded] = useState<AudioFile | null>(null);
  const [rights, setRights] = useState(emptyRights);
  const mutation = useRenderMutation(`${scope}:import`, writable, unavailable);
  async function save() {
    if (
      (!file && !existingId) ||
      !rights.confirmed ||
      !rights.source.trim() ||
      !rights.rights.trim() ||
      asset?.status === "LOCKED"
    )
      return;
    await mutation.mutate(
      async (signal) => {
        let sourceId = existingId || uploaded?.id;
        if (!sourceId && file) {
          const result = await uploadRecording(
            projectId,
            file,
            {
              source_note: rights.source.trim(),
              rights_note: rights.rights.trim(),
              rights_confirmed: true,
            },
            signal,
          );
          if (signal.aborted) throw new DOMException("Aborted", "AbortError");
          setUploaded(result);
          sourceId = result.id;
        }
        if (!sourceId) throw new Error("请先选择录音。");
        const pending = pendingCreationKey(
          `${scope}:import:${sourceId}:${asset?.id ?? "new"}`,
        );
        const result = await audioApi.import(
          snapshotId,
          lineId,
          {
            creation_key: pending.key,
            source_file_id: sourceId,
            ...(asset
              ? { target_asset_id: asset.id, expected_revision: asset.revision }
              : {}),
          },
          signal,
        );
        pending.clear();
        return result;
      },
      () => {
        onImported();
      },
      "录音已检查并登记为新候选，原音频保留；请试听后审核和选用。",
    );
  }
  return (
    <details className="min-w-0 rounded-lg border p-3">
      <summary className="cursor-pointer text-sm font-medium">
        导入或替换本句录音
      </summary>
      <form
        className="mt-4 flex min-w-0 flex-col gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="audio-import-file">上传外部录音</FieldLabel>
            <Input
              key={fileInputKey}
              id="audio-import-file"
              type="file"
              accept=".wav,.mp3,.ogg,audio/wav,audio/mpeg,audio/ogg"
              disabled={!writable || mutation.busy}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setExistingId("");
                setUploaded(null);
                setRights(emptyRights);
              }}
            />
            <FieldDescription>
              WAV、MP3 或
              OGG。直传需要私有对象存储；上传后仍由服务端检查实际格式、时长及静音情况。
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="audio-existing-file">
              或使用已上传的录音
            </FieldLabel>
            <ChoiceSelect
              id="audio-existing-file"
              value={existingId}
              placeholder="选择项目内录音"
              disabled={!writable || mutation.busy}
              options={files.rows
                .filter((row) => row.media_type.startsWith("audio/"))
                .map((row) => ({ value: row.id, label: row.original_name }))}
              onValueChange={(value) => {
                setExistingId(value);
                setFileInputKey((key) => key + 1);
                setFile(null);
                setUploaded(null);
                const row = files.rows.find((item) => item.id === value);
                setRights({
                  source: row?.source_note ?? "",
                  rights: row?.rights_note ?? "",
                  confirmed: false,
                });
              }}
            />
          </Field>
        </FieldGroup>
        <AudioPagination list={files} label="已上传文件" />
        <RightsFields
          value={rights}
          onChange={setRights}
          disabled={!writable || mutation.busy}
          readOnlySource={!!existingId || !!uploaded}
        />
        {(existingId || uploaded) && (
          <p className="text-sm text-muted-foreground">
            沿用这份文件已保存的来源与授权说明；请确认用于本句的使用权。
          </p>
        )}
        {uploaded && (
          <p className="text-sm text-muted-foreground">
            录音已上传，重试登记会复用这份文件。
          </p>
        )}
        <Button
          type="submit"
          disabled={
            !writable ||
            mutation.busy ||
            asset?.status === "LOCKED" ||
            (!file && !existingId) ||
            !rights.confirmed ||
            !rights.source.trim() ||
            !rights.rights.trim()
          }
        >
          {mutation.busy ? "正在检查并导入…" : "确认授权并导入本句录音"}
        </Button>
        <AudioFeedback {...mutation} />
      </form>
    </details>
  );
}
