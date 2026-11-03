"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ScrollArea } from "@/components/ui/scroll-area";
import { BookOpenText, Sparkles } from "lucide-react";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice, PagePanel, StatusBadge } from "@/components/ui-kit";
import { api, json, message } from "@/lib/api/client";
import { workflowErrorMessage } from "@/lib/workflow-errors";
import type { Run } from "@/types/api";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import CharacterMatching from "@/features/story/components/character-matching";

type Source = { id: string; import_no: number; original_name: string };
type Analysis = {
  id: string;
  version_no: number;
  run_id: string;
  source_id: string | null;
  source_name: string;
  story_sha256: string;
  source_sha256: string | null;
  created_at: string;
};
type Detail = Analysis & {
  source_text: string;
  content: {
    summary: string;
    conflicts: string[];
    key_events: {
      title: string;
      description: string;
      character_names: string[];
    }[];
    characters: {
      name: string;
      aliases: string[];
      role: string;
      motivation: string;
    }[];
  };
};
const active = (run: Run | null) =>
  !!run && ["QUEUED", "RUNNING"].includes(run.status);

export default function StoryAnalysis({
  episodeId,
  writable,
  onTasks,
  onQueued,
}: {
  episodeId: string;
  writable: boolean;
  onTasks: () => void;
  onQueued: () => Promise<void>;
}) {
  const prefix = `/episodes/${episodeId}`;
  const list = usePagedList<Analysis>(prefix + "/story-analyses");
  const sources = usePagedList<Source>(prefix + "/story-sources");
  const [mode, setMode] = useState("manual");
  const [story, setStory] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Analysis | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [loading, setLoading] = useState(false);
  const live = useRef(true);
  const saving = useRef(false);
  const submissions = useRef(0);
  const sequence = useRef(0);
  const key = useRef({ fingerprint: "", value: "" });
  const preview = useRef<HTMLElement>(null);
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
  useEffect(() => {
    void sources.reload().catch(() => {});
  }, [sources.reload]);
  useEffect(() => {
    let cancelled = false;
    const initialSubmission = submissions.current;
    void api<Run[]>(prefix + "/runs?limit=100&offset=0")
      .then((rows) => {
        if (!cancelled && submissions.current === initialSubmission)
          setRun(
            rows.find((row) => row.workflow_type === "story_analysis") ?? null,
          );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [prefix]);
  useEffect(() => {
    if (!run || !active(run)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const updated = await api<Run>(`/runs/${run!.id}`);
        if (cancelled) return;
        setRun(updated);
        setError("");
        if (!active(updated)) {
          if (updated.status === "SUCCEEDED") {
            setNotice("故事提炼已完成，请从版本记录查看结果。");
            await list.reload();
          }
          return;
        }
      } catch (error) {
        if (!cancelled)
          setError("任务已提交；状态或目录刷新失败：" + message(error));
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 1500);
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [run?.id, run?.status, list.reload]);
  async function show(row: Analysis) {
    const ticket = ++sequence.current;
    setSelected(row);
    setDetail(null);
    setDetailError("");
    setLoading(true);
    try {
      const result = await api<Detail>(prefix + "/story-analyses/" + row.id);
      if (live.current && ticket === sequence.current) {
        setDetail(result);
        preview.current?.focus();
      }
    } catch (error) {
      if (live.current && ticket === sequence.current)
        setDetailError(message(error));
    } finally {
      if (live.current && ticket === sequence.current) setLoading(false);
    }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!writable || saving.current || active(run)) return;
    saving.current = true;
    submissions.current++;
    setBusy(true);
    setError("");
    setNotice("");
    const body = mode === "manual" ? { story } : { source_id: sourceId };
    const fingerprint = JSON.stringify(body);
    if (key.current.fingerprint !== fingerprint)
      key.current = { fingerprint, value: crypto.randomUUID() };
    let accepted: { run_id: string; status: string };
    try {
      accepted = await api(prefix + "/story-analysis-runs", {
        ...json("POST", body),
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key.current.value,
        },
      });
    } catch (error) {
      if (live.current) {
        setError(message(error));
        setBusy(false);
      }
      saving.current = false;
      return;
    }
    if (!live.current) return;
    setNotice(
      "提炼任务已提交，输入已保存。可在任务与审核中取消排队任务或重试失败任务。",
    );
    setRun({
      episode_id: episodeId,
      id: accepted.run_id,
      status: accepted.status,
      workflow_type: "story_analysis",
      revision: 0,
      pending_review: null,
      error_code: null,
      created_at: new Date().toISOString(),
    });
    key.current = { fingerprint: "", value: "" };
    setBusy(false);
    saving.current = false;
    try {
      await Promise.all([onQueued(), list.reload()]);
    } catch {
      if (live.current)
        setNotice(
          "提炼任务已提交；工作区或结果目录刷新失败，请刷新查看，无需再次提交。",
        );
    }
  }
  return (
    <div className="studio-analysis">
      <PagePanel
        className="studio-page studio-analysis-compose"
        eyebrow="读懂故事，再开始创作"
        title="故事提炼"
        description="提取摘要、主要冲突、关键事件与角色表。每次结果独立保存，不会自动生成剧本或角色资产。"
      >
        {!writable && <Notice>当前为只读模式，可查看历史提炼结果。</Notice>}
        <details className="studio-analysis-input" open>
          <summary>输入故事与提炼选项</summary>
          <form onSubmit={submit} className="grid min-w-0 gap-4">
            <FieldGroup className="studio-analysis-fields">
              <Field>
                <FieldLabel htmlFor="analysis-mode">故事输入方式</FieldLabel>
                <ChoiceSelect
                  id="analysis-mode"
                  aria-label="故事输入方式"
                  value={mode}
                  onValueChange={setMode}
                  disabled={!writable || busy}
                  options={[
                    { value: "manual", label: "手动输入" },
                    { value: "source", label: "已导入来源" },
                  ]}
                />
              </Field>
              {mode === "manual" ? (
                <Field>
                  <FieldLabel htmlFor="analysis-story">待提炼故事</FieldLabel>
                  <Textarea
                    id="analysis-story"
                    rows={7}
                    value={story}
                    onChange={(e) => setStory(e.target.value)}
                    disabled={!writable || busy}
                    required
                    maxLength={20000}
                    placeholder="粘贴需要理解的故事原文…"
                  />
                  <p className="text-sm text-muted-foreground">
                    {story.length} / 20000 字；服务配置可能有更低限制。
                  </p>
                </Field>
              ) : (
                <Field>
                  <FieldLabel htmlFor="analysis-source">
                    选择故事来源
                  </FieldLabel>
                  <ChoiceSelect
                    id="analysis-source"
                    aria-label="选择故事来源"
                    value={sourceId}
                    onValueChange={setSourceId}
                    disabled={!writable || busy}
                    placeholder="请选择已导入来源"
                    options={sources.rows.map((row) => ({
                      value: row.id,
                      label: `#${row.import_no} ${row.original_name}`,
                    }))}
                  />
                  <p className="text-sm text-muted-foreground">
                    按所选版本的完整原文提炼；超过服务限制时会拒绝，不会截断。请先在“故事来源”导入文件。
                  </p>
                  <ListPagination
                    {...sources}
                    count={sources.rows.length}
                    label="可选来源"
                  />
                </Field>
              )}
            </FieldGroup>
            <div className="flex flex-wrap gap-2">
              <Button
                type="submit"
                disabled={
                  !writable ||
                  busy ||
                  active(run) ||
                  (mode === "manual" ? !story.trim() : !sourceId)
                }
              >
                <Sparkles className="size-4" />
                {busy ? "正在提交…" : "开始提炼"}
              </Button>
              <Button type="button" variant="outline" onClick={onTasks}>
                查看任务与审核
              </Button>
            </div>
          </form>
        </details>
        {notice && <Notice role="status">{notice}</Notice>}
        {error && (
          <Notice tone="error" role="alert">
            {error}
          </Notice>
        )}
        {run && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span>最近提炼任务</span>
            <StatusBadge status={run.status} />
          </div>
        )}
        {run?.error_code && (
          <Notice tone="error" role="alert" className="mt-3">
            {workflowErrorMessage(run.error_code)} 可到任务与审核主动重试。
          </Notice>
        )}
      </PagePanel>
      <PagePanel
        className="studio-page studio-analysis-index"
        title="提炼版本记录"
        description="选择版本查看结构化结果及当时的原文快照。"
      >
        <ListPagination {...list} count={list.rows.length} label="提炼版本" />
        {list.loaded && list.rows.length === 0 && (
          <p className="text-muted-foreground">还没有提炼结果。</p>
        )}
        <ScrollArea className="studio-index-scroll">
          <div className="studio-index-items">
            {list.rows.map((row) => (
              <Button
                key={row.id}
                variant={selected?.id === row.id ? "secondary" : "outline"}
                aria-pressed={selected?.id === row.id}
                className="h-auto justify-start whitespace-normal break-words text-left"
                onClick={() => void show(row)}
              >
                提炼 v{row.version_no} · {row.source_name} ·{" "}
                {new Date(row.created_at).toLocaleString("zh-CN")}
              </Button>
            ))}
          </div>
        </ScrollArea>
      </PagePanel>
      {selected ? (
        <section
          ref={preview}
          tabIndex={-1}
          aria-label="提炼结果详情"
          className="studio-detail studio-analysis-result grid min-w-0 gap-4 break-words"
        >
          <h3 className="text-lg font-semibold">
            提炼 v{selected.version_no} · {selected.source_name}
          </h3>
          {loading && <p role="status">正在加载提炼结果…</p>}
          {detailError && (
            <>
              <Notice tone="error">{detailError}</Notice>
              <Button variant="outline" onClick={() => void show(selected)}>
                重试详情
              </Button>
            </>
          )}
          {detail && (
            <>
              <h4 className="font-semibold">摘要</h4>
              <p className="whitespace-pre-wrap">{detail.content.summary}</p>
              <h4 className="font-semibold">主要冲突</h4>
              {detail.content.conflicts.length ? (
                <ul className="list-inside list-disc">
                  {detail.content.conflicts.map((text, i) => (
                    <li key={i}>{text}</li>
                  ))}
                </ul>
              ) : (
                <p>未提取到明确冲突。</p>
              )}
              <h4 className="font-semibold">关键事件</h4>
              <ol className="grid list-inside list-decimal gap-3">
                {detail.content.key_events.map((item, i) => (
                  <li key={i}>
                    <strong>{item.title}</strong>
                    <p className="whitespace-pre-wrap">{item.description}</p>
                    {item.character_names.length > 0 && (
                      <p>相关角色：{item.character_names.join("、")}</p>
                    )}
                  </li>
                ))}
              </ol>
              <h4 className="font-semibold">角色表</h4>
              <CharacterMatching
                episodeId={episodeId}
                sourceType="story-analyses"
                versionId={detail.id}
                writable={writable}
                busy={busy}
              />
              {!detail.content.characters.length && <p>未提取到明确角色。</p>}
              {detail.content.characters.map((item) => (
                <article key={item.name} className="studio-analysis-character">
                  <strong>{item.name}</strong>
                  {item.aliases.length > 0 && (
                    <p>别名：{item.aliases.join("、")}</p>
                  )}
                  <p>身份：{item.role}</p>
                  <p>动机：{item.motivation}</p>
                </article>
              ))}
              <details className="studio-disclosure">
                <summary className="cursor-pointer">
                  查看原文快照与来源校验
                </summary>
                <p className="break-all text-xs text-muted-foreground">
                  文本 SHA-256：{detail.story_sha256}
                </p>
                {detail.source_sha256 && (
                  <p className="break-all text-xs text-muted-foreground">
                    文件 SHA-256：{detail.source_sha256}
                  </p>
                )}
                <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-3 font-sans text-sm">
                  {detail.source_text}
                </pre>
              </details>
            </>
          )}
        </section>
      ) : (
        <section className="studio-analysis-empty" aria-label="提炼结果详情">
          <BookOpenText className="size-8" strokeWidth={1.2} />
          <h3>选择一个提炼版本</h3>
          <p>
            在左侧查看版本，或先输入故事开始提炼。摘要、冲突、事件与角色会在这里展示。
          </p>
        </section>
      )}
    </div>
  );
}
