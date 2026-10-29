"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Notice, FormLabel } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import type { AssetVersion } from "@/types/api";

type Source = {
  source_asset_version_id: string;
  asset_name: string;
  version_no: number;
  dependency_type: string;
  stale: boolean;
  status: string;
  current_version_id: string | null;
};

export default function RebuildImage({
  endpoint,
  version,
  disabled,
  onReconfirm,
  onImport,
}: {
  endpoint: string;
  version: AssetVersion;
  disabled: boolean;
  onReconfirm: (manifestId: string) => Promise<unknown>;
  onImport: (manifestId: string) => void;
}) {
  const [inputs, setInputs] = useState<Source[] | null>(null);
  const [replacements, setReplacements] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [frozen, setFrozen] = useState(false);
  const pending = useRef<{ body: unknown; id?: string } | null>(null);
  const submitting = useRef(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    const controller = new AbortController();
    void api<{ inputs: Source[] }>(endpoint + "/rebuild-inputs", {
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted) return;
        setInputs(result.inputs);
        setReplacements(
          Object.fromEntries(
            result.inputs.map((row) => [
              row.source_asset_version_id,
              row.current_version_id ?? row.source_asset_version_id,
            ]),
          ),
        );
        setError("");
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      });
    return () => {
      live.current = false;
      controller.abort();
    };
  }, [endpoint, attempt]);
  async function prepare(reconfirm: boolean) {
    if (!inputs || disabled || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    pending.current ??= {
      body: {
        creation_key: crypto.randomUUID(),
        expected_revision: version.revision,
        replacements,
      },
    };
    setFrozen(true);
    try {
      if (!pending.current.id) {
        const result = await api<{ id: string }>(
          endpoint + "/rebuild-manifest",
          json("POST", pending.current.body),
        );
        pending.current.id = result.id;
      }
      if (!live.current) return;
      if (reconfirm) await onReconfirm(pending.current.id);
      else onImport(pending.current.id);
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <section
      aria-label="确认重建输入"
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-4"
    >
      <h4>按需重建此图片</h4>
      <p>
        逐项确认精确输入。未变更项沿用原版本；替换项默认显示当前选用，可填写同一资产的其他有效批准版本。
      </p>
      {error && (
        <Notice tone="error">
          {error}
          {!frozen && (
            <Button variant="outline" onClick={() => setAttempt((x) => x + 1)}>
              重试读取重建输入
            </Button>
          )}
        </Notice>
      )}
      {inputs?.map((item, index) => (
        <FormLabel key={item.source_asset_version_id + item.dependency_type}>
          {item.asset_name} · 原 v{item.version_no} · {item.dependency_type}
          {item.stale ? " · 已失效" : ""}
          <Input
            aria-label={`重建输入版本 ${index + 1}`}
            value={replacements[item.source_asset_version_id] ?? ""}
            disabled={disabled || busy || frozen}
            onChange={(event) =>
              setReplacements((current) => ({
                ...current,
                [item.source_asset_version_id]: event.target.value,
              }))
            }
          />
        </FormLabel>
      ))}
      <p className="text-sm">
        沿用图片会另建候选并重新审核；原图字节与来源记录保留。替换图片从新输入清单导入，不会自动调用媒体生成服务。
      </p>
      {frozen && <p>重试沿用已确认输入；若要修改，请重新打开此版本。</p>}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={disabled || busy || !inputs}
          onClick={() => void prepare(true)}
        >
          按新输入另存并重新确认此图
        </Button>
        <Button
          variant="outline"
          disabled={disabled || busy || !inputs}
          onClick={() => void prepare(false)}
        >
          按新输入导入替换图片
        </Button>
      </div>
    </section>
  );
}
