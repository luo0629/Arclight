import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ChoiceSelect } from "@/components/choice-select";
import { FormLabel, Notice, PagePanel } from "@/components/ui-kit";
import { api, json, message } from "./api";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";

type Source = {
  id: string;
  episode_id: string;
  import_no: number;
  original_name: string;
  source_type: string;
  media_type: string;
  encoding: string;
  byte_size: number;
  sha256: string;
  source_note: string;
  rights_note: string;
  created_by: string;
  created_at: string;
};
type Detail = Source & { text: string };
const maxBytes = 256 * 1024;

export default function StorySources({
  episodeId,
  writable,
}: {
  episodeId: string;
  writable: boolean;
}) {
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
  return (
    <PagePanel
      eyebrow="保留故事的来处"
      title="故事来源"
      description="导入这一集的原始故事，保留原文件与来源说明。每次导入独立保存，历史内容不会被覆盖。"
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
      {writable && (
        <form
          onSubmit={upload}
          aria-label="导入故事文件"
          aria-busy={busy}
          className="flex min-w-0 flex-col gap-4 rounded-xl border p-4"
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
                { value: "yes", label: "我确认拥有导入和使用此故事的权利" },
              ]}
            />
          </FormLabel>
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
              confirmed !== "yes"
            }
          >
            {busy ? "正在导入…" : "保存故事来源"}
          </Button>
        </form>
      )}
      <div className="grid min-w-0 gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside
          aria-label="故事来源目录"
          className="flex min-w-0 flex-col gap-3"
        >
          {list.loaded && !list.rows.length && <p>尚未导入故事文件</p>}
          {list.rows.map((row) => (
            <Button
              key={row.id}
              variant={selected?.id === row.id ? "secondary" : "outline"}
              disabled={busy}
              className="h-auto justify-start whitespace-normal break-all text-left"
              onClick={() => void show(row)}
            >
              导入 {row.import_no} · {row.original_name}
            </Button>
          ))}
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
          className="flex min-w-0 flex-col gap-3 rounded-xl border p-4 focus-visible:outline-2"
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
                授权（已确认使用权）：{detail.rights_note}
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
            </>
          )}
        </section>
      </div>
    </PagePanel>
  );
}
