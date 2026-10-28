"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import { Notice } from "@/components/ui-kit";
import Dialog from "@/components/shared/app-dialog";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { api, apiBlob, json, message } from "@/lib/api/client";
import type { AssetVersion, VisualAsset } from "@/types/api";

const labels: Record<string, string> = {
  DRAFT: "候选草稿",
  READY_FOR_REVIEW: "待人工审核",
  APPROVED: "已批准",
  LOCKED: "已锁定",
  REJECTED: "已拒绝",
  STALE: "已失效",
  ARCHIVED: "已归档",
};
const label = (status: string) => labels[status] ?? status;

export default function AssetVersions({
  asset,
  path,
  writable,
  owner,
  onSaved,
}: {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  owner: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
}) {
  const endpoint = path + "/" + asset.id + "/versions";
  const list = usePagedList<AssetVersion>(endpoint);
  const [selected, setSelected] = useState<AssetVersion | null>(null);
  const [importing, setImporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [comment, setComment] = useState("");
  const live = useRef(true);
  const sequence = useRef(0);
  const submitting = useRef(false);
  const editable = writable && !["LOCKED", "ARCHIVED"].includes(asset.status);
  useEffect(() => {
    live.current = true;
    void list.reload().catch(() => {});
    return () => {
      live.current = false;
    };
  }, [list.reload]);
  async function choose(row: AssetVersion) {
    const current = ++sequence.current;
    setError("");
    try {
      const latest = await api<AssetVersion>(endpoint + "/" + row.id);
      if (live.current && current === sequence.current) {
        setSelected(latest);
        list.update(latest);
        setComment("");
      }
    } catch (error) {
      if (live.current && current === sequence.current)
        setError(message(error));
    }
  }
  async function change(status: string) {
    if (!selected || !editable || submitting.current) return;
    const current = ++sequence.current;
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api<AssetVersion>(
        endpoint + "/" + selected.id + "/state",
        json("PATCH", {
          expected_revision: selected.revision,
          status,
          comment,
        }),
      );
      if (live.current && current === sequence.current) {
        setSelected(saved);
        list.update(saved);
        setComment("");
        setNotice("版本状态已保存，历史图片和引用保持原样");
      }
    } catch (error) {
      if (live.current && current === sequence.current)
        setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  const restoreKey = useRef<string | null>(null);
  async function selectCurrent() {
    if (!selected || !editable || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const saved = await api<VisualAsset>(
        endpoint + "/" + selected.id + "/select",
        json("POST", { expected_revision: asset.revision }),
      );
      if (live.current) {
        await onSaved(saved);
        setNotice("已选用此精确版本");
      }
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function restoreNew() {
    if (!selected || !editable || !comment.trim() || submitting.current) return;
    restoreKey.current ??= crypto.randomUUID();
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const saved = await api<AssetVersion>(
        endpoint + "/" + selected.id + "/restore-as-new",
        json("POST", {
          expected_revision: asset.revision,
          creation_key: restoreKey.current,
          comment,
        }),
      );
      restoreKey.current = null;
      await created(saved);
      if (live.current) setComment("");
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function created(version: AssetVersion) {
    if (!live.current) return;
    ++sequence.current;
    setSelected(version);
    setImporting(false);
    setNotice("新候选版本已保存，尚未选用");
    // A successful POST remains successful if either following GET fails.
    try {
      await onSaved(await api<VisualAsset>(path + "/" + asset.id));
    } catch {
      if (live.current) setNotice("新候选版本已保存，请刷新资产修订");
    }
    try {
      await list.reload();
      await list.ensure(version.id);
    } catch {
      /* Retry only the list GET. The exact receipt remains visible. */
    }
  }
  return (
    <section
      className="flex min-w-0 flex-col gap-3 rounded-xl border p-4"
      aria-label="资产版本"
    >
      <h3 className="font-semibold">不可变图片版本</h3>
      <p className="text-sm text-muted-foreground">
        人工导入原图，每次另建候选版本。批准后可人工选用；历史图片可另存为新候选并重新审核。支持PNG/JPEG，最大1
        MiB、400万像素、最长边4096。
      </p>
      <p className="break-all text-sm" role="status">
        当前选用：{asset.current_version_id ?? "尚未选用"}
      </p>
      {notice && <Notice role="status">{notice}</Notice>}
      {error && (
        <Notice role="alert" tone="error">
          {error}
        </Notice>
      )}
      <Button disabled={!editable || busy} onClick={() => setImporting(true)}>
        导入新图片版本
      </Button>
      <div className="flex flex-wrap gap-2">
        {list.rows.map((row) => (
          <Button
            key={row.id}
            variant={selected?.id === row.id ? "default" : "outline"}
            disabled={busy}
            onClick={() => void choose(row)}
          >
            查看资产版本 v{row.version_no} · {label(row.status)}
            {row.stale && row.status !== "STALE" ? " · 已失效" : ""}
          </Button>
        ))}
      </div>
      <ListPagination {...list} count={list.rows.length} label="资产版本" />
      {selected && (
        <div
          className="flex min-w-0 flex-col gap-3"
          aria-label="精确资产版本详情"
        >
          <h4>
            版本 v{selected.version_no} · {label(selected.status)} · 状态修订{" "}
            {selected.revision}
          </h4>
          {selected.restored_from_version_id && (
            <p className="break-all text-sm">
              恢复来源版本：{selected.restored_from_version_id} ·{" "}
              {selected.restoration_note}
            </p>
          )}
          {selected.stale && (
            <Notice>此版本已失效，仅供历史查阅，不能用于新引用。</Notice>
          )}
          <dl className="grid gap-2 break-all text-sm">
            <div>
              <dt>版本 ID</dt>
              <dd>{selected.id}</dd>
            </div>
            <div>
              <dt>创建者 / 创建时间（UTC）</dt>
              <dd>
                {selected.created_by} / {selected.created_at}
              </dd>
            </div>
            <div>
              <dt>SHA-256</dt>
              <dd>{selected.checksum}</dd>
            </div>
            {selected.media && (
              <>
                <div>
                  <dt>原文件</dt>
                  <dd>
                    {selected.media.original_name} · {selected.media.width}×
                    {selected.media.height} · {selected.media.byte_size} 字节
                  </dd>
                </div>
                <div>
                  <dt>图片来源</dt>
                  <dd>{selected.media.source_note}</dd>
                </div>
                <div>
                  <dt>使用权说明</dt>
                  <dd>{selected.media.rights_note}</dd>
                </div>
              </>
            )}
            {selected.asset_snapshot && (
              <div>
                <dt>创建时资产名称 / 资料修订</dt>
                <dd>
                  {selected.asset_snapshot.name} /{" "}
                  {selected.asset_snapshot.revision}
                </dd>
              </div>
            )}
            {selected.archived_from_status && (
              <div>
                <dt>归档前状态</dt>
                <dd>{label(selected.archived_from_status)}</dd>
              </div>
            )}
          </dl>
          {selected.media ? (
            <VersionImage
              key={selected.id}
              path={endpoint + "/" + selected.id + "/image"}
            />
          ) : (
            <p>历史对象尚未接入图片读取；原版本保留。</p>
          )}
          {editable && selected.status !== "LOCKED" && (
            <>
              <Field>
                <FieldLabel htmlFor="asset-version-comment">
                  版本状态说明
                </FieldLabel>
                <Textarea
                  id="asset-version-comment"
                  maxLength={1000}
                  value={comment}
                  disabled={busy}
                  onChange={(event) => setComment(event.target.value)}
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                {selected.status === "APPROVED" && !selected.stale && (
                  <Button
                    disabled={busy || asset.current_version_id === selected.id}
                    onClick={() => void selectCurrent()}
                  >
                    选用此版本
                  </Button>
                )}
                {selected.media && (
                  <Button
                    variant="outline"
                    disabled={busy || !comment.trim()}
                    onClick={() => void restoreNew()}
                  >
                    从此版本另存新候选
                  </Button>
                )}
                {selected.status === "DRAFT" && (
                  <Button
                    disabled={busy}
                    onClick={() => void change("READY_FOR_REVIEW")}
                  >
                    提交版本审核
                  </Button>
                )}
                {owner && selected.status === "READY_FOR_REVIEW" && (
                  <>
                    <Button
                      disabled={busy}
                      onClick={() => void change("APPROVED")}
                    >
                      批准此版本
                    </Button>
                    <Button
                      variant="outline"
                      disabled={busy || !comment.trim()}
                      onClick={() => void change("REJECTED")}
                    >
                      拒绝此版本
                    </Button>
                  </>
                )}
                {owner && selected.status === "APPROVED" && (
                  <Button
                    variant="outline"
                    disabled={busy || !comment.trim()}
                    onClick={() => void change("STALE")}
                  >
                    标记版本失效
                  </Button>
                )}
                {selected.status === "ARCHIVED" ? (
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => void change("RESTORE")}
                  >
                    恢复归档前状态
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    disabled={busy || asset.current_version_id === selected.id}
                    onClick={() => void change("ARCHIVED")}
                  >
                    归档此版本
                  </Button>
                )}
              </div>
            </>
          )}
        </div>
      )}
      {importing && (
        <ImportDialog
          asset={asset}
          endpoint={endpoint}
          writable={editable}
          onSaved={created}
          onDismiss={() => setImporting(false)}
        />
      )}
    </section>
  );
}

function VersionImage({ path }: { path: string }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setUrl("");
    setError("");
    void apiBlob(path, controller.signal)
      .then((blob) => {
        if (!controller.signal.aborted) {
          objectUrl = URL.createObjectURL(blob);
          setUrl(objectUrl);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(message(error));
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, attempt]);
  return (
    <div>
      {url && (
        <img
          src={url}
          alt="精确资产版本原图"
          className="max-h-80 max-w-full rounded-lg object-contain"
        />
      )}
      {error && (
        <Notice role="alert" tone="error">
          {error}
          <Button
            variant="outline"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重试读取图片
          </Button>
        </Notice>
      )}
    </div>
  );
}

function ImportDialog({
  asset,
  endpoint,
  writable,
  onSaved,
  onDismiss,
}: {
  asset: VisualAsset;
  endpoint: string;
  writable: boolean;
  onSaved: (version: AssetVersion) => Promise<void>;
  onDismiss: () => void;
}) {
  const [revision] = useState(asset.revision);
  const [creationKey] = useState(() => crypto.randomUUID());
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [rights, setRights] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [discard, setDiscard] = useState(false);
  const submitting = useRef(false);
  const frozenPayload = useRef<string | null>(null);
  const dirty = !!file || !!source || !!rights || confirmed;
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
    if (!file || !writable || submitting.current) return;
    if (
      file.size > 1024 * 1024 ||
      !["image/png", "image/jpeg"].includes(file.type)
    ) {
      setError("请选择不超过1 MiB的PNG/JPEG图片");
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      if (!frozenPayload.current) {
        const encoded = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result).split(",")[1]);
          reader.onerror = () => reject(new Error("无法读取本机图片"));
          reader.readAsDataURL(file);
        });
        frozenPayload.current = JSON.stringify({
          creation_key: creationKey,
          expected_revision: revision,
          original_name: file.name,
          media_type: file.type,
          content_base64: encoded,
          source_note: source,
          rights_note: rights,
          rights_confirmed: confirmed,
        });
      }
      const version = await api<AssetVersion>(endpoint, {
        method: "POST",
        body: frozenPayload.current,
      });
      await onSaved(version);
    } catch (error) {
      setError(message(error));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  // Keep an uncertain POST byte-for-byte replayable. Editing starts a new import after closing.
  const frozen = frozenPayload.current !== null;
  return (
    <Dialog labelledBy="asset-import-title" busy={busy} onDismiss={dismiss}>
      <h2 id="asset-import-title">导入不可变图片版本</h2>
      {discard ? (
        <>
          <p>放弃未保存的图片导入？</p>
          <Button onClick={() => setDiscard(false)}>继续编辑</Button>
          <Button onClick={onDismiss}>放弃导入并关闭</Button>
        </>
      ) : (
        <form onSubmit={save} className="flex min-w-0 flex-col gap-4">
          {error && (
            <Notice role="alert" tone="error">
              {error}
            </Notice>
          )}
          {frozen && (
            <p className="text-sm">
              提交后重试沿用同一导入标识和图片，避免重复版本。若需修改，请关闭后重新导入。
            </p>
          )}
          <fieldset
            disabled={busy || !writable || frozen}
            className="flex flex-col gap-3"
          >
            <Field>
              <FieldLabel htmlFor="asset-image-file">版本图片</FieldLabel>
              <Input
                id="asset-image-file"
                type="file"
                required
                accept="image/png,image/jpeg"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="asset-image-source">图片来源说明</FieldLabel>
              <Textarea
                id="asset-image-source"
                required
                maxLength={1000}
                value={source}
                onChange={(event) => setSource(event.target.value)}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="asset-image-rights">
                图片使用权说明
              </FieldLabel>
              <Textarea
                id="asset-image-rights"
                required
                maxLength={1000}
                value={rights}
                onChange={(event) => setRights(event.target.value)}
              />
            </Field>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                required
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              我确认拥有此图片的使用权并同意保存到项目私有目录
            </label>
          </fieldset>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              disabled={
                busy ||
                !writable ||
                !file ||
                !confirmed ||
                !source.trim() ||
                !rights.trim()
              }
            >
              {busy ? "正在保存…" : "保存新候选版本"}
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
