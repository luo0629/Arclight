"use client";

import { FileText, Upload } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetDescription,
  SheetTrigger,
} from "@/components/ui/sheet";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ChoiceSelect } from "@/components/choice-select";
import { FormLabel, Notice, PagePanel } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import type {
  Source,
  SourceDetail as Detail,
} from "@/features/story/types/source";
import SourceRevisionEditor from "@/features/story/components/source-revision-editor";
import SourceHistory from "@/features/story/components/source-history";
const maxBytes = 256 * 1024;

export default function StorySources({
  episodeId,
  writable,
  onQueued,
  onTasks,
}: {
  episodeId: string;
  writable: boolean;
  onQueued: () => Promise<void>;
  onTasks: () => void;
}) {
  const [importOpen, setImportOpen] = useState(false);
  const [fileBase, setFileBase] = useState<Detail | null>(null);
  const [changeNote, setChangeNote] = useState("");
  const path = `/episodes/${episodeId}/story-sources`;
  const list = usePagedList<Source>(path);
  const [file, setFile] = useState<File | null>(null);
  const [encoding, setEncoding] = useState("utf-8");
  const [origin, setOrigin] = useState("");
  const [rights, setRights] = useState("");
  const [confirmed, setConfirmed] = useState("no");
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Source | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const preview = useRef<HTMLElement>(null);
  const sequence = useRef(0);
  const live = useRef(true);
  const requestKey = useRef({ fingerprint: "", key: "" });
  const runKey = useRef({ sourceId: "", key: "" });
  const launching = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      sequence.current++;
    };
  }, []);
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  async function show(row: Source) {
    const ticket = ++sequence.current;
    setSelected(row);
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    try {
      const result = await api<Detail>(path + "/" + row.id);
      if (live.current && ticket === sequence.current) {
        setDetail(result);
        preview.current?.focus();
      }
    } catch (error) {
      if (live.current && ticket === sequence.current)
        setDetailError(message(error));
    } finally {
      if (live.current && ticket === sequence.current) setDetailLoading(false);
    }
  }
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!writable || saving.current || !file || confirmed !== "yes") return;
    saving.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    let saved: Detail;
    try {
      if (!/\.(txt|md|markdown)$/i.test(file.name))
        throw new Error("仅支持 TXT、MD 或 Markdown 文件");
      if (!file.size) throw new Error("故事文件不能为空");
      if (file.size > maxBytes) throw new Error("故事文件不能超过 256 KiB");
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192)
        binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      const body = {
        original_name: file.name,
        media_type: file.type,
        encoding,
        content_base64: btoa(binary),
        source_note: origin.trim(),
        rights_note: rights.trim(),
        rights_confirmed: true,
        base_source_id: fileBase?.id,
        change_note: fileBase ? changeNote.trim() : undefined,
      };
      const fingerprint = JSON.stringify(body);
      if (requestKey.current.fingerprint !== fingerprint)
        requestKey.current = { fingerprint, key: crypto.randomUUID() };
      saved = await api<Detail>(
        path,
        json("POST", { ...body, import_key: requestKey.current.key }),
      );
    } catch (error) {
      if (live.current) setError(message(error));
      saving.current = false;
      if (live.current) setBusy(false);
      return;
    }
    if (!live.current) return;
    ++sequence.current;
    setSelected(saved);
    setDetail(saved);
    setDetailLoading(false);
    setDetailError("");
    setNotice("故事来源已保存，未启动生成。");
    setImportOpen(false);
    setFile(null);
    if (fileInput.current) fileInput.current.value = "";
    setConfirmed("no");
    requestKey.current = { fingerprint: "", key: "" };
    preview.current?.focus();
    try {
      await list.reload();
    } catch (error) {
      if (live.current)
        setNotice("故事来源已保存；目录刷新失败，请重试加载，无需再次导入。");
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function savedText(saved: Detail) {
    ++sequence.current;
    setSelected(saved);
    setDetail(saved);
    setDetailError("");
    setDetailLoading(false);
    setNotice("故事来源版本已保存，历史内容与任务输入保持原样。");
    try {
      await list.reload();
    } catch {
      if (live.current)
        setNotice(
          "来源版本已保存；目录刷新失败，请重新加载目录，无需再次保存。",
        );
    }
  }
  async function writeFromSource() {
    if (!detail || !writable || launching.current) return;
    launching.current = true;
    setBusy(true);
    setError("");
    if (runKey.current.sourceId !== detail.id)
      runKey.current = { sourceId: detail.id, key: crypto.randomUUID() };
    try {
      await api(`/episodes/${episodeId}/script-runs`, {
        ...json("POST", { source_id: detail.id }),
        headers: { "Idempotency-Key": runKey.current.key },
      });
      if (!live.current) return;
      setNotice("已从此精确来源版本创建编剧任务。");
      runKey.current = { sourceId: "", key: "" };
      try {
        await onQueued();
      } catch {
        setNotice("编剧任务已创建，工作区刷新失败；请进入任务与审核重新加载。");
      }
      onTasks();
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      launching.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <PagePanel
      className="studio-page story-sources"
      eyebrow="保留故事的来处"
      title="故事来源"
      description="保留这一集的原始文本、文件与修订记录。指定来源版本派生，历史内容和任务输入不会被覆盖。"
    >
      <p className="text-sm text-muted-foreground">
        支持 TXT、MD、Markdown，单文件最多 256 KiB。默认 UTF-8（含
        BOM）；旧版中文文本可显式选择 GB18030。Markdown
        按纯文本预览，导入不会调用模型或启动任务。
      </p>
      {!writable && (
        <Notice tone="info">当前为只读模式，可查看已保存来源。</Notice>
      )}
      {notice && (
        <Notice role="status" tone="success">
          {notice}
        </Notice>
      )}
      {error && !importOpen && <Notice tone="error">{error}</Notice>}
      {writable && (
        <div className="flex flex-wrap gap-2">
          <SourceRevisionEditor episodeId={episodeId} onSaved={savedText} />
        </div>
      )}
      {writable && (
        <Sheet
          open={importOpen}
          onOpenChange={(open) => {
            if (!saving.current) setImportOpen(open);
          }}
        >
          <SheetTrigger asChild>
            <Button
              className="self-start"
              onClick={() => {
                setFileBase(null);
                setChangeNote("");
              }}
            >
              <Upload className="size-4" />
              导入故事文件
            </Button>
          </SheetTrigger>
          <SheetContent
            className="studio-import-sheet"
            onCloseAutoFocus={(event) => {
              if (detail) {
                event.preventDefault();
                preview.current?.focus();
              }
            }}
            onInteractOutside={(event) => {
              if (saving.current) event.preventDefault();
            }}
            onEscapeKeyDown={(event) => {
              if (saving.current) event.preventDefault();
            }}
          >
            <SheetTitle>
              {fileBase ? "以文件保存来源修订" : "导入故事文件"}
            </SheetTitle>
            <SheetDescription>
              保留原稿与授权记录，不自动启动生成。支持 TXT / Markdown，最大 256
              KiB。
            </SheetDescription>
            <ScrollArea className="studio-import-scroll">
              <form
                onSubmit={upload}
                aria-label="导入故事文件"
                aria-busy={busy}
                className="flex min-w-0 flex-col gap-5"
              >
                <FormLabel>
                  故事文件
                  <Input
                    ref={fileInput}
                    type="file"
                    accept=".txt,.md,.markdown,text/plain,text/markdown"
                    disabled={busy}
                    onChange={(event) => {
                      setFile(event.target.files?.[0] ?? null);
                      setError("");
                    }}
                    required
                  />
                </FormLabel>
                {file && (
                  <p className="text-xs text-muted-foreground">
                    已选择：{file.name} · {Math.ceil(file.size / 1024)} KiB
                  </p>
                )}
                <FormLabel>
                  文件编码
                  <ChoiceSelect
                    value={encoding}
                    onValueChange={setEncoding}
                    disabled={busy}
                    aria-label="文件编码"
                    options={[
                      { value: "utf-8", label: "UTF-8（含 BOM）" },
                      { value: "gb18030", label: "GB18030（旧版中文文本）" },
                    ]}
                  />
                </FormLabel>
                <FormLabel>
                  来源说明
                  <Input
                    value={origin}
                    onChange={(event) => setOrigin(event.target.value)}
                    disabled={busy}
                    maxLength={1000}
                    required
                    placeholder="例如：本人原创初稿，或来源链接与作者"
                  />
                </FormLabel>
                <FormLabel>
                  授权说明
                  <Textarea
                    aria-label="授权说明"
                    value={rights}
                    onChange={(event) => setRights(event.target.value)}
                    disabled={busy}
                    maxLength={1000}
                    required
                    placeholder="说明原创权利或已获得的使用许可"
                  />
                </FormLabel>
                <FormLabel>
                  使用权确认
                  <ChoiceSelect
                    value={confirmed}
                    onValueChange={setConfirmed}
                    disabled={busy}
                    aria-label="使用权确认"
                    options={[
                      { value: "no", label: "请选择确认" },
                      {
                        value: "yes",
                        label: "我确认拥有导入和使用此故事的权利",
                      },
                    ]}
                  />
                </FormLabel>
                {fileBase && (
                  <FormLabel>
                    文件修订说明
                    <Input
                      value={changeNote}
                      onChange={(event) => setChangeNote(event.target.value)}
                      disabled={busy}
                      required
                      maxLength={1000}
                    />
                  </FormLabel>
                )}
                {error && (
                  <Notice tone="error" role="alert">
                    {error}
                  </Notice>
                )}
                <Button
                  type="submit"
                  disabled={
                    busy ||
                    !file ||
                    !origin.trim() ||
                    !rights.trim() ||
                    (!!fileBase && !changeNote.trim()) ||
                    confirmed !== "yes"
                  }
                >
                  {busy ? "正在导入…" : "保存故事来源"}
                </Button>
              </form>
            </ScrollArea>
          </SheetContent>
        </Sheet>
      )}
      <div className="studio-split">
        <aside aria-label="故事来源目录" className="studio-index">
          <div className="studio-index-heading">
            <FileText className="size-4" />
            <h3>原始故事</h3>
            <span>{list.rows.length}</span>
          </div>
          {list.loaded && !list.rows.length && <p>尚未导入故事文件</p>}
          <ScrollArea className="studio-index-scroll">
            <div className="studio-index-items">
              {list.rows.map((row) => (
                <Button
                  key={row.id}
                  variant={selected?.id === row.id ? "secondary" : "ghost"}
                  aria-pressed={selected?.id === row.id}
                  disabled={busy}
                  className="h-auto justify-start whitespace-normal break-all text-left"
                  onClick={() => void show(row)}
                >
                  导入 {row.import_no} · {row.original_name}
                  {row.provenance.version_no > 1
                    ? ` · 修订 v${row.provenance.version_no}`
                    : ""}
                </Button>
              ))}
            </div>
          </ScrollArea>
          <fieldset disabled={busy}>
            <ListPagination
              {...list}
              label="故事来源"
              count={list.rows.length}
            />
          </fieldset>
        </aside>
        <section
          ref={preview}
          tabIndex={-1}
          aria-label="故事来源预览"
          aria-busy={detailLoading}
          className="studio-detail source-preview flex min-w-0 flex-col gap-3 focus-visible:outline-2"
        >
          {!selected && <p>选择一个来源查看文本与授权记录。</p>}
          {detailLoading && <p role="status">正在读取故事来源…</p>}
          {detailError && (
            <Notice tone="error" role="alert">
              {detailError}
              <Button
                variant="outline"
                onClick={() => selected && void show(selected)}
              >
                重试读取来源
              </Button>
            </Notice>
          )}
          {detail && (
            <>
              <h3 className="break-all">{detail.original_name}</h3>
              <p className="text-sm break-all">
                导入 {detail.import_no} · {detail.encoding} · {detail.byte_size}{" "}
                字节 · {new Date(detail.created_at).toLocaleString()}
              </p>
              <p className="text-sm break-all">来源：{detail.source_note}</p>
              <p className="text-sm break-all">
                授权（
                {detail.provenance.rights_status === "CONFIRMED"
                  ? "已确认使用权"
                  : "未单独声明"}
                ）：{detail.rights_note}
              </p>
              <p className="text-sm break-all">导入者：{detail.created_by}</p>
              <p className="text-xs break-all text-muted-foreground">
                SHA-256：{detail.sha256}
              </p>
              <pre
                className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]"
                aria-label="原始故事文本"
              >
                {detail.text}
              </pre>
              <p className="text-sm">
                来源版本 v{detail.provenance.version_no} ·{" "}
                {detail.provenance.change_note}
              </p>
              {detail.provenance.rights_status === "UNDECLARED" && (
                <Notice>
                  此文本随创作任务保存，未补录授权声明。确认权利后可另存来源修订；历史声明保持原样。
                </Notice>
              )}
              <details className="text-xs text-muted-foreground">
                <summary>查看精确来源标识</summary>
                <p className="mt-2 break-all">来源：{detail.id}</p>
                <p className="break-all">
                  根来源：{detail.provenance.root_source_id}
                </p>
                {detail.provenance.parent_source_id && (
                  <p className="break-all">
                    上一版本：{detail.provenance.parent_source_id}
                  </p>
                )}
                <p className="break-all">
                  原稿引用：{detail.provenance.object_ref.key}
                </p>
                <p>原稿保存在应用内，读取仍需项目权限。</p>
              </details>
              {writable && (
                <div className="flex flex-wrap gap-2">
                  <SourceRevisionEditor
                    key={detail.id}
                    episodeId={episodeId}
                    base={detail}
                    onSaved={savedText}
                  />
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() => {
                      setFileBase(detail);
                      setChangeNote("");
                      setOrigin(detail.source_note);
                      setRights(
                        detail.provenance.rights_status === "CONFIRMED"
                          ? detail.rights_note
                          : "",
                      );
                      setConfirmed("no");
                      setError("");
                      setImportOpen(true);
                    }}
                  >
                    使用文件修订
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() => void writeFromSource()}
                  >
                    从此版本编剧
                  </Button>
                </div>
              )}
              <SourceHistory
                key={detail.id + "-history"}
                episodeId={episodeId}
                source={detail}
                onOpen={(row) => void show(row)}
              />
            </>
          )}
        </section>
      </div>
    </PagePanel>
  );
}
