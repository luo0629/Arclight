"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Notice } from "@/components/ui-kit";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { useAuth } from "@/features/auth/auth-context";
import { api, json, message } from "@/lib/api/client";
import {
  artStatus,
  type ArtRecord,
} from "@/features/assets/types/art-proposals";
import ArtProposalEditor from "./art-proposal-editor";
import ArtProposalDetail from "./art-proposal-detail";
import ArtGeneration from "./art-generation";

export default function ArtProposals({
  projectId,
  writable,
  owner,
}: {
  projectId: string;
  writable: boolean;
  owner: boolean;
}) {
  const path = `/projects/${projectId}/art-proposals`;
  const list = usePagedList<ArtRecord>(path + "?include_archived=true");
  const { user } = useAuth();
  const storageKey = `arclight:art-selection:${user.id}:${projectId}`;
  const [selected, setSelected] = useState<ArtRecord | null>(null);
  const [editor, setEditor] = useState<{ base?: ArtRecord } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  const operation = useRef(false);
  const sequence = useRef(0);
  function select(row: ArtRecord) {
    setSelected(row);
    try {
      sessionStorage.setItem(storageKey, row.id);
    } catch {
      /* Selection is optional; draft recovery has its own notice. */
    }
  }
  useEffect(() => {
    live.current = true;
    void list.reload().catch(() => {});
    try {
      const id = sessionStorage.getItem(storageKey);
      if (id)
        void api<ArtRecord>(`${path}/${id}`)
          .then((row) => {
            if (live.current) setSelected(row);
          })
          .catch(() => {});
    } catch {
      /* Optional navigation memory. */
    }
    return () => {
      live.current = false;
      ++sequence.current;
    };
  }, [path, storageKey, list.reload]);
  async function load(id: string, version?: string) {
    const request = ++sequence.current;
    setError("");
    try {
      const row = await api<ArtRecord>(
        `${path}/${id}${version ? `/versions/${version}` : ""}`,
      );
      if (live.current && request === sequence.current) select(row);
      return row;
    } catch (error) {
      if (live.current && request === sequence.current)
        setError(message(error));
    }
  }
  async function saved(row: ArtRecord) {
    if (!live.current) return;
    ++sequence.current;
    select(row);
    setEditor(null);
    setNotice("美术提案已保存，请核对逐项变更后提交审核。");
    try {
      await list.reload();
    } catch {
      /* Keep the exact successful receipt. */
    }
  }
  async function action(suffix: string, body: object) {
    if (!selected || operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    ++sequence.current;
    try {
      if (suffix === "resume") {
        await api(
          `/human-reviews/${selected.review?.id}/resume`,
          json("POST", body),
        );
        const row = await api<ArtRecord>(`${path}/${selected.id}`);
        if (live.current) select(row);
      } else {
        const row = await api<ArtRecord>(
          `${path}/${selected.id}${suffix ? `/${suffix}` : ""}`,
          json(suffix ? "POST" : "PATCH", body),
        );
        if (live.current) select(row);
      }
      if (live.current)
        setNotice(
          suffix === "submit" ? "已提交人工审核。" : "提案状态已更新。",
        );
      try {
        await list.reload();
      } catch {
        /* A list refresh must not replay the mutation. */
      }
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      operation.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <section aria-label="美术提案" className="flex min-w-0 flex-col gap-4 py-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold">美术提案</h2>
          <p className="text-sm text-muted-foreground">
            比较角色、场景、道具与规范方案，人工审核后生成正式版本。
          </p>
        </div>
        {writable && (
          <Button onClick={() => setEditor({})}>新建美术提案</Button>
        )}
      </header>
      <ArtGeneration
        key={projectId}
        projectId={projectId}
        writable={writable}
        onView={(delivery) => {
          void load(delivery.proposal_id, delivery.version_id);
          void list.reload().catch(() => {});
        }}
      />
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      {notice && (
        <Notice tone="success" role="status">
          {notice}
        </Notice>
      )}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(12rem,1fr)_minmax(0,3fr)]">
        <nav aria-label="美术提案列表" className="flex min-w-0 flex-col gap-2">
          {list.rows.map((row) => (
            <Button
              key={row.id}
              variant={selected?.id === row.id ? "secondary" : "ghost"}
              className="h-auto justify-start whitespace-normal py-3 text-left"
              disabled={busy}
              onClick={() => void load(row.id)}
            >
              <span className="min-w-0 break-words">
                {row.version.proposal_json.visual_goal}
                <span className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
                  <span>v{row.version.version_no}</span>
                  <Badge variant="outline">{artStatus(row)}</Badge>
                </span>
              </span>
            </Button>
          ))}
          {list.loaded && !list.rows.length && (
            <p className="text-sm text-muted-foreground">
              还没有美术提案。从视觉目标开始，先提交一个人工方案。
            </p>
          )}
          <ListPagination
            {...list}
            count={list.rows.length}
            label="美术提案"
            reload={async () => {
              const rows = await list.reload();
              if (selected) await load(selected.id);
              return rows;
            }}
          />
        </nav>
        {selected ? (
          <ArtProposalDetail
            key={`${selected.version.id}:${selected.review?.id}:${selected.review?.status}:${selected.archived}`}
            row={selected}
            owner={owner}
            writable={writable}
            busy={busy}
            onAction={(suffix, body) => void action(suffix, body)}
            onEdit={() => {
              void load(selected.id).then((row) => {
                if (row && live.current) setEditor({ base: row });
              });
            }}
            onVersion={(id) => void load(selected.id, id)}
          />
        ) : (
          <p className="text-sm text-muted-foreground">
            选择提案，查看精确来源、方案与正式变更。
          </p>
        )}
      </div>
      {editor && (
        <ArtProposalEditor
          key={editor.base?.id ?? "new"}
          projectId={projectId}
          base={editor.base}
          onSaved={(row) => void saved(row)}
          onClose={() => setEditor(null)}
        />
      )}
    </section>
  );
}
