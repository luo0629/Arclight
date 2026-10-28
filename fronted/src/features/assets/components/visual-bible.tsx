"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { api, message } from "@/lib/api/client";

const fields = [
  ["art_style", "画风描述", 10000],
  ["line_style", "线条风格", 2000],
  ["render_style", "渲染风格", 2000],
  ["palette", "主要配色", 2000],
  ["lighting", "光照原则", 2000],
  ["composition", "构图偏好", 2000],
  ["prohibited", "禁止元素", 4000],
  ["negative_prompt", "视觉负面提示词", 4000],
] as const;
type FieldName = (typeof fields)[number][0];
type Values = Record<FieldName, string>;
type Version = {
  id: string | null;
  version_no: number;
  fields: Values;
  locked_fields: FieldName[];
  created_by: string | null;
  created_at: string | null;
  change_kind: string;
  legacy_snapshot: Record<string, unknown>;
};

export default function VisualBible({
  projectId,
  writable,
  owner,
}: {
  projectId: string;
  writable: boolean;
  owner: boolean;
}) {
  const path = `/projects/${projectId}/visual-bible`;
  const [current, setCurrent] = useState<Version | null>(null);
  const [draft, setDraft] = useState<Values | null>(null);
  const [edited, setEdited] = useState<Set<FieldName>>(new Set());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState(false);
  const live = useRef(true);
  const operation = useRef(false);
  const pending = useRef<{ path: string; body: string } | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const dirty =
    current !== null &&
    draft !== null &&
    [...edited].some((key) => current.fields[key] !== draft[key]);
  async function load(reset: boolean) {
    setError("");
    try {
      const row = await api<Version>(path);
      if (live.current) {
        setCurrent(row);
        setDraft((previous) =>
          reset || !previous
            ? row.fields
            : {
                ...row.fields,
                ...Object.fromEntries(
                  [...edited].map((key) => [key, previous[key]]),
                ),
              },
        );
        if (reset) setEdited(new Set());
      }
    } catch (error) {
      if (live.current) setError(message(error));
    }
  }
  useEffect(() => {
    live.current = true;
    void load(true);
    return () => {
      live.current = false;
    };
    // Component is keyed by project ID.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  useEffect(() => {
    function leave(event: BeforeUnloadEvent) {
      if (dirty || uncertain) event.preventDefault();
    }
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty, uncertain]);
  async function send(endpoint: string, body: object) {
    if (operation.current || !writable) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    pending.current ??= { path: endpoint, body: JSON.stringify(body) };
    try {
      const saved = await api<Version>(pending.current.path, {
        method: "POST",
        body: pending.current.body,
      });
      if (live.current) {
        setCurrent(saved);
        setDraft(saved.fields);
        setEdited(new Set());
        setNotice(`已保存视觉规范 v${saved.version_no}`);
        pending.current = null;
        setUncertain(false);
        setHistory(false);
      }
    } catch (error) {
      if (live.current) {
        setError(message(error));
        if (error instanceof TypeError) setUncertain(true);
        else {
          pending.current = null;
          setUncertain(false);
        }
      }
    } finally {
      operation.current = false;
      if (live.current) setBusy(false);
    }
  }
  function save(event: FormEvent) {
    event.preventDefault();
    if (!current || !draft) return;
    const changed = Object.fromEntries(
      fields
        .filter(
          ([key]) => edited.has(key) && current.fields[key] !== draft[key],
        )
        .map(([key]) => [key, draft[key]]),
    );
    void send(path + "/versions", {
      expected_version_no: current.version_no,
      creation_key: crypto.randomUUID(),
      fields: changed,
    });
  }
  return (
    <section
      aria-label="项目视觉规范"
      className="flex min-w-0 flex-col gap-4 border-b pb-5"
    >
      <h2 className="text-lg font-semibold">Visual Bible · 项目视觉规范</h2>
      <p className="text-sm text-muted-foreground">
        逐字段维护并另存不可变版本；Owner
        可独立锁定字段，锁定值只能先显式解锁再修改。旧任意配置保留在只读来源中，后续任务冻结精确规范版本。
      </p>
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
      {!current || !draft ? (
        <Button variant="outline" onClick={() => void load(true)}>
          重新加载视觉规范
        </Button>
      ) : (
        <>
          <p>
            当前视觉规范 v{current.version_no}
            {current.version_no === 0 && " · 尚未建立正式版本"}
          </p>
          <form onSubmit={save} className="flex min-w-0 flex-col gap-4">
            {fields.map(([key, label, max]) => (
              <Field key={key}>
                <FieldLabel htmlFor={`visual-${key}`}>
                  {label}
                  {current.locked_fields.includes(key) && " · 已锁定"}
                </FieldLabel>
                <Textarea
                  id={`visual-${key}`}
                  maxLength={max}
                  value={draft[key]}
                  disabled={
                    !writable ||
                    busy ||
                    uncertain ||
                    current.locked_fields.includes(key)
                  }
                  onChange={(e) => {
                    setDraft({ ...draft, [key]: e.target.value });
                    setEdited((previous) => new Set([...previous, key]));
                  }}
                />
                {owner && (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={!writable || busy || dirty || uncertain}
                    onClick={() =>
                      void send(path + "/locks", {
                        expected_version_no: current.version_no,
                        creation_key: crypto.randomUUID(),
                        locked_fields: current.locked_fields.includes(key)
                          ? current.locked_fields.filter((k) => k !== key)
                          : [...current.locked_fields, key],
                      })
                    }
                  >
                    {current.locked_fields.includes(key) ? "解锁" : "锁定"}
                    {label}
                  </Button>
                )}
              </Field>
            ))}
            <div className="flex flex-wrap gap-2">
              {writable && (
                <Button disabled={busy || (!dirty && !uncertain)}>
                  {uncertain ? "重试同一视觉规范请求" : "另存视觉规范新版本"}
                </Button>
              )}
              <Button
                type="button"
                variant="outline"
                disabled={busy || uncertain}
                onClick={() => void load(false)}
              >
                刷新规范并保留草稿
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy || uncertain}
                onClick={() => {
                  setDraft(current.fields);
                  setEdited(new Set());
                  setError("");
                }}
              >
                还原当前规范
              </Button>
            </div>
          </form>
          <Button variant="outline" onClick={() => setHistory(!history)}>
            {history ? "收起视觉规范历史" : "查看视觉规范历史"}
          </Button>
          {history && <History path={path} />}
          <details>
            <summary>原始旧配置（只读）</summary>
            <pre className="whitespace-pre-wrap break-all text-xs">
              {JSON.stringify(current.legacy_snapshot, null, 2)}
            </pre>
          </details>
        </>
      )}
    </section>
  );
}

function History({ path }: { path: string }) {
  const list = usePagedList<Version & { id: string }>(path + "/versions");
  const [selected, setSelected] = useState<Version | null>(null);
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  return (
    <section aria-label="视觉规范历史" className="flex min-w-0 flex-col gap-3">
      {list.rows.map((row) => (
        <Button key={row.id} variant="outline" onClick={() => setSelected(row)}>
          查看视觉规范 v{row.version_no}
        </Button>
      ))}
      <ListPagination {...list} count={list.rows.length} label="视觉规范版本" />
      {selected && (
        <article
          aria-label="精确视觉规范版本"
          className="flex min-w-0 flex-col gap-2 break-all text-sm"
        >
          <h3>
            视觉规范 v{selected.version_no} · {selected.id}
          </h3>
          <p>
            {selected.change_kind === "LOCK" ? "字段锁定变更" : "字段编辑"} ·{" "}
            {selected.created_by} · {selected.created_at}
          </p>
          {fields.map(([key, label]) => (
            <p key={key} className="whitespace-pre-wrap">
              {label}：{selected.fields[key] || "未填写"}
              {selected.locked_fields.includes(key) && " · 当时已锁定"}
            </p>
          ))}
        </article>
      )}
    </section>
  );
}
