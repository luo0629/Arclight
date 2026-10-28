"use client";

import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type FormEvent,
} from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import Dialog from "@/components/shared/app-dialog";
import { api, json, message } from "@/lib/api/client";
import type { VisualAsset } from "@/types/api";

// Each mount keeps the exact revision seen when editing began.
export default function AssetProfileDialog({
  asset,
  path,
  endpoint,
  title,
  profile,
  initial,
  writable,
  onSaved,
  onDismiss,
  children,
  pendingDirty = false,
}: {
  asset: VisualAsset;
  path: string;
  endpoint: string;
  title: string;
  profile: unknown;
  initial: unknown;
  writable: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
  onDismiss: () => void;
  children: ReactNode;
  pendingDirty?: boolean;
}) {
  const [revision] = useState(asset.revision);
  const [base] = useState(JSON.stringify(initial));
  const dirty = JSON.stringify(profile) !== base || pendingDirty;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [discard, setDiscard] = useState(false);
  const submitting = useRef(false);
  useEffect(() => {
    function leave(event: BeforeUnloadEvent) {
      if (dirty || busy) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty, busy]);
  function dismiss() {
    if (!busy) {
      if (dirty) setDiscard(true);
      else onDismiss();
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!writable || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const row = await api<VisualAsset>(
        path + "/" + asset.id + "/" + endpoint,
        json("PUT", { expected_revision: revision, profile }),
      );
      await onSaved(row);
      onDismiss();
    } catch (error) {
      setError(message(error));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog labelledBy="asset-profile-title" busy={busy} onDismiss={dismiss}>
      <h2 id="asset-profile-title">{title}</h2>
      {discard ? (
        <>
          <p>放弃未保存的设定？</p>
          <Button onClick={() => setDiscard(false)}>继续编辑</Button>
          <Button onClick={onDismiss}>放弃修改并关闭</Button>
        </>
      ) : (
        <form onSubmit={save} className="flex min-w-0 flex-col gap-4">
          {error && (
            <Notice role="alert" tone="error">
              {error}
            </Notice>
          )}
          <fieldset disabled={busy || !writable}>{children}</fieldset>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy || !writable || !dirty}>
              {busy ? "正在保存…" : "保存设定"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={dismiss}
            >
              取消
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}
