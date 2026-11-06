"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { Notice, FormLabel } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import type { ContentVersion } from "@/types/api";
import CreationOptionsFields, {
  creationPayload,
  initialCreationOptions,
  validCreationOptions,
} from "@/features/editor/components/creation-options";

export default function RebuildStoryboard({
  episodeId,
  versionId,
  disabled,
  reload,
  showTasks,
}: {
  episodeId: string;
  versionId: string;
  disabled: boolean;
  reload: () => Promise<void>;
  showTasks: () => void;
}) {
  const [scripts, setScripts] = useState<ContentVersion[]>([]);
  const [source, setSource] = useState("");
  const [creation, setCreation] = useState(initialCreationOptions);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const key = useRef({ source: "", value: "" });
  const submitting = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void (async () => {
      const result: ContentVersion[] = [];
      for (let offset = 0; ; offset += 100) {
        const rows = await api<ContentVersion[]>(
          `/episodes/${episodeId}/scripts?limit=100&offset=${offset}`,
          { signal: controller.signal },
        );
        result.push(...rows.filter((row) => row.status === "APPROVED"));
        if (rows.length < 100) break;
      }
      if (!controller.signal.aborted) setScripts(result);
    })().catch((error) => {
      if (!controller.signal.aborted) setError(message(error));
    });
    return () => controller.abort();
  }, [episodeId, attempt]);
  async function rebuild() {
    if (
      !source ||
      disabled ||
      submitting.current ||
      !validCreationOptions(creation)
    )
      return;
    submitting.current = true;
    setBusy(true);
    setError("");
    const body = {
      script_version_id: source,
      rebuild_from_version_id: versionId,
      ...creationPayload(creation),
    };
    const signature = JSON.stringify(body);
    if (key.current.source !== signature)
      key.current = { source: signature, value: crypto.randomUUID() };
    try {
      await api(`/episodes/${episodeId}/storyboard-runs`, {
        ...json("POST", body),
        headers: { "Idempotency-Key": key.current.value },
      });
      await reload();
      showTasks();
    } catch (error) {
      setError(message(error));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <section
      aria-label="按需重生成分镜"
      className="flex flex-col gap-3 rounded-lg border p-4"
    >
      <h3>重新生成此失效分镜</h3>
      <p>
        只创建新的待审核分镜，原版本和未受影响产物保留。请选择本次使用的精确批准剧本。
      </p>
      {error && (
        <Notice tone="error">
          {error}
          <Button variant="outline" onClick={() => setAttempt((x) => x + 1)}>
            重新读取批准剧本
          </Button>
        </Notice>
      )}
      <FormLabel>
        重生成来源剧本
        <NativeSelect
          value={source}
          disabled={disabled || busy}
          onChange={(event) => setSource(event.target.value)}
        >
          <NativeSelectOption value="">请选择精确版本</NativeSelectOption>
          {scripts.map((row) => (
            <NativeSelectOption key={row.id} value={row.id}>
              剧本 v{row.version_no} · {row.id}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FormLabel>
      <CreationOptionsFields
        directorOnly
        allowReview={false}
        value={creation}
        onChange={setCreation}
        disabled={disabled || busy}
      />
      <Button
        disabled={
          !source || disabled || busy || !validCreationOptions(creation)
        }
        onClick={() => void rebuild()}
      >
        从所选来源重新生成分镜
      </Button>
    </section>
  );
}
