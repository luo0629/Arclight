import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice, PagePanel, StatusBadge } from "@/components/ui-kit";
import { api, json, message, type Episode, type Run } from "./api";
import { useSessionDraft } from "./Drafts";
import usePagedList from "./usePagedList";
import ListPagination from "./ListPagination";

type Entry = {
  episode_no: number;
  title: string;
  goal: string;
  conflict: string;
  turning_point: string;
  ending_hook: string;
};
type Content = {
  schema_version: "1.0";
  target_episode_count: number;
  episode_duration_ms: number;
  episodes: Entry[];
};
type Outline = {
  id: string;
  episode_id: string;
  version_no: number;
  status: string;
  base_version_id: string | null;
  created_by: string;
  created_at: string;
  confirmed_by: string | null;
};
type Detail = Outline & {
  content: Content;
  source_snapshot: {
    story: string;
    story_sha256: string;
    source_name: string;
    source_id: string | null;
    source_sha256: string | null;
    analysis_id: string | null;
    analysis_version_no: number | null;
  };
};
type Source = { id: string; import_no: number; original_name: string };
type Analysis = { id: string; version_no: number; source_name: string };
const initial = {
  mode: "manual",
  story: "",
  source: "",
  analysis: "",
  count: "",
  seconds: "",
  key: "",
};
const inputValid = (v: unknown): v is typeof initial =>
  !!v &&
  typeof v === "object" &&
  Object.keys(initial).every(
    (k) => typeof (v as Record<string, unknown>)[k] === "string",
  );
const outlineDraftValid = (
  v: unknown,
): v is { content: Content; key: string } => {
  if (!v || typeof v !== "object") return false;
  const { content, key } = v as { content?: Content; key?: unknown };
  return (
    typeof key === "string" &&
    !!content &&
    content.schema_version === "1.0" &&
    Number.isInteger(content.target_episode_count) &&
    Number.isFinite(content.episode_duration_ms) &&
    Array.isArray(content.episodes) &&
    content.episodes.length > 0 &&
    content.episodes.length <= 1000 &&
    content.episodes.every(
      (row) =>
        !!row &&
        Number.isInteger(row.episode_no) &&
        [
          row.title,
          row.goal,
          row.conflict,
          row.turning_point,
          row.ending_hook,
        ].every((text) => typeof text === "string"),
    )
  );
};
const active = (run: Run | null) =>
  !!run && ["QUEUED", "RUNNING", "WAITING_REVIEW"].includes(run.status);

export default function StoryOutline({
  episode,
  writable,
  owner,
  onQueued,
  onTasks,
}: {
  episode: Episode;
  writable: boolean;
  owner: boolean;
  onQueued: () => Promise<void>;
  onTasks: () => void;
}) {
  const prefix = "/episodes/" + episode.id;
  const list = usePagedList<Outline>(prefix + "/story-outlines");
  const sources = usePagedList<Source>(prefix + "/story-sources");
  const analyses = usePagedList<Analysis>(prefix + "/story-analyses");
  const draft = useSessionDraft({
    id: JSON.stringify([episode.id, "outline-input"]),
    episodeId: episode.id,
    label: "分集大纲输入",
    initial,
    validate: inputValid,
  });
  const value = draft.value;
  const [detail, setDetail] = useState<Detail | null>(null);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  const saving = useRef(false);
  const sequence = useRef(0);
  const submitted = useRef(0);
  const writerKey = useRef({ version: "", key: "" });
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
    void analyses.reload().catch(() => {});
  }, [analyses.reload]);
  useEffect(() => {
    let cancelled = false;
    const ticket = submitted.current;
    void api<Run[]>(prefix + "/runs?limit=100&offset=0")
      .then((rows) => {
        if (!cancelled && ticket === submitted.current)
          setRun(
            rows.find((r) => active(r)) ??
              rows.find((r) => r.workflow_type === "story_outline") ??
              null,
          );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [prefix]);
  useEffect(() => {
    if (!active(run)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await api<Run>("/runs/" + run!.id);
        if (cancelled) return;
        setRun(result);
        if (!active(result)) {
          if (
            result.status === "SUCCEEDED" &&
            result.workflow_type === "story_outline"
          ) {
            setNotice("分集大纲已生成，请选择版本查看并编辑。");
            await list.reload();
          }
          return;
        }
      } catch (e) {
        if (!cancelled) setError("任务状态或版本刷新失败：" + message(e));
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 1500);
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [run?.id, run?.status, list.reload]);
  function change(key: keyof typeof initial, next: string) {
    draft.update({ ...value, [key]: next, key: crypto.randomUUID() });
  }
  async function refreshAfterSave() {
    try {
      await Promise.all([list.reload(), onQueued()]);
    } catch {
      if (live.current)
        setNotice("操作已保存；目录刷新失败，请重试刷新，无需再次提交。");
    }
  }
  async function show(id: string) {
    const ticket = ++sequence.current;
    setSelected(id);
    setDetail(null);
    setDetailError("");
    setLoading(true);
    try {
      const result = await api<Detail>(prefix + "/story-outlines/" + id);
      if (live.current && ticket === sequence.current) setDetail(result);
    } catch (e) {
      if (live.current && ticket === sequence.current)
        setDetailError(message(e));
    } finally {
      if (live.current && ticket === sequence.current) setLoading(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving.current || !writable || active(run)) return;
    saving.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    submitted.current++;
    const key = value.key || crypto.randomUUID();
    if (!value.key) draft.update({ ...value, key });
    const body = {
      ...(value.mode === "manual"
        ? { story: value.story }
        : value.mode === "source"
          ? { source_id: value.source }
          : { analysis_id: value.analysis }),
      ...(value.count ? { target_episode_count: Number(value.count) } : {}),
      ...(value.seconds
        ? { episode_duration_ms: Math.round(Number(value.seconds) * 1000) }
        : {}),
    };
    try {
      const accepted = await api<{ run_id: string; status: string }>(
        prefix + "/story-outline-runs",
        {
          ...json("POST", body),
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": key,
          },
        },
      );
      if (!live.current) return;
      setRun({
        id: accepted.run_id,
        status: accepted.status,
        workflow_type: "story_outline",
        revision: 0,
        pending_review: null,
        error_code: null,
        created_at: new Date().toISOString(),
      });
      setNotice("大纲任务已提交，输入已固定。失败后可在任务与审核中显式重试。");
      draft.clear();
      await refreshAfterSave();
    } catch (e) {
      if (live.current) setError(message(e));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function confirm() {
    if (!detail || !owner || !writable || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await api<Detail>(
        prefix + "/story-outlines/" + detail.id + "/confirm",
        json("POST", {}),
      );
      if (!live.current) return;
      setDetail(result);
      setNotice("此精确版本已确认，可用于对应剧集的编剧起稿。");
      await refreshAfterSave();
    } catch (e) {
      if (live.current) setError(message(e));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function derive() {
    if (!detail || !writable || saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    if (writerKey.current.version !== detail.id)
      writerKey.current = { version: detail.id, key: crypto.randomUUID() };
    try {
      const accepted = await api<{ run_id: string; status: string }>(
        prefix + "/script-runs",
        {
          ...json("POST", { outline_version_id: detail.id }),
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": writerKey.current.key,
          },
        },
      );
      if (!live.current) return;
      setRun({
        id: accepted.run_id,
        status: accepted.status,
        workflow_type: "script_storyboard",
        revision: 0,
        pending_review: null,
        error_code: null,
        created_at: new Date().toISOString(),
      });
      setNotice(
        "已从大纲 v" +
          detail.version_no +
          " 的第" +
          episode.episode_no +
          "集启动编剧，请到任务与审核查看。",
      );
      await refreshAfterSave();
    } catch (e) {
      if (live.current) setError(message(e));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  const blocked = busy || active(run);
  return (
    <div className="studio-outline">
      <PagePanel
        className="studio-page"
        title="分集大纲"
        description="把完整故事编排成每一集。编辑另存版本，确认后再选择精确版本开始编剧。"
      >
        {!writable && <Notice>当前为只读模式，可查看项目内的历史大纲。</Notice>}
        <details className="studio-analysis-input" open>
          <summary>故事与分集规划</summary>
          <form onSubmit={submit} className="grid gap-4">
            <Field>
              <FieldLabel htmlFor="outline-mode">大纲输入方式</FieldLabel>
              <ChoiceSelect
                id="outline-mode"
                aria-label="大纲输入方式"
                value={value.mode}
                onValueChange={(v) => change("mode", v)}
                disabled={!writable || blocked}
                options={[
                  { value: "manual", label: "手动输入" },
                  { value: "source", label: "已导入来源" },
                  { value: "analysis", label: "精确提炼版本" },
                ]}
              />
            </Field>
            {value.mode === "manual" ? (
              <Field>
                <FieldLabel htmlFor="outline-story">待规划故事</FieldLabel>
                <Textarea
                  id="outline-story"
                  rows={5}
                  maxLength={20000}
                  required
                  disabled={!writable || blocked}
                  value={value.story}
                  onChange={(e) => change("story", e.target.value)}
                />
              </Field>
            ) : value.mode === "source" ? (
              <Field>
                <FieldLabel htmlFor="outline-source">大纲故事来源</FieldLabel>
                <ChoiceSelect
                  id="outline-source"
                  aria-label="大纲故事来源"
                  value={value.source}
                  onValueChange={(v) => change("source", v)}
                  disabled={!writable || blocked}
                  placeholder="选择已导入的原文"
                  options={sources.rows.map((s) => ({
                    value: s.id,
                    label: "来源 " + s.import_no + "：" + s.original_name,
                  }))}
                />
                <ListPagination
                  {...sources}
                  label="大纲故事来源"
                  count={sources.rows.length}
                />
              </Field>
            ) : (
              <Field>
                <FieldLabel htmlFor="outline-analysis">大纲提炼版本</FieldLabel>
                <ChoiceSelect
                  id="outline-analysis"
                  aria-label="大纲提炼版本"
                  value={value.analysis}
                  onValueChange={(v) => change("analysis", v)}
                  disabled={!writable || blocked}
                  placeholder="选择精确提炼版本"
                  options={analyses.rows.map((s) => ({
                    value: s.id,
                    label: "提炼 v" + s.version_no + "：" + s.source_name,
                  }))}
                />
                <ListPagination
                  {...analyses}
                  label="大纲提炼版本"
                  count={analyses.rows.length}
                />
              </Field>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="outline-count">目标集数</FieldLabel>
                <Input
                  id="outline-count"
                  type="number"
                  min={1}
                  max={1000}
                  step={1}
                  placeholder="使用项目设定"
                  value={value.count}
                  disabled={!writable || blocked}
                  onChange={(e) => change("count", e.target.value)}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="outline-seconds">
                  单集时长（秒）
                </FieldLabel>
                <Input
                  id="outline-seconds"
                  type="number"
                  min={2.5}
                  max={1800}
                  step={0.001}
                  placeholder="使用项目设定"
                  value={value.seconds}
                  disabled={!writable || blocked}
                  onChange={(e) => change("seconds", e.target.value)}
                />
              </Field>
            </div>
            <p className="text-sm text-muted-foreground">
              集数或时长留空时使用提交时的项目设定。手动输入保存在当前账号的本机草稿中。
            </p>
            {draft.recovered && <Notice>已恢复本机的大纲输入草稿。</Notice>}
            <div className="flex flex-wrap gap-2">
              <Button
                type="submit"
                disabled={
                  !writable ||
                  blocked ||
                  !(value.mode === "manual"
                    ? value.story.trim()
                    : value.mode === "source"
                      ? value.source
                      : value.analysis)
                }
              >
                生成分集大纲
              </Button>
              <Button type="button" variant="outline" onClick={onTasks}>
                查看任务与审核
              </Button>
            </div>
          </form>
        </details>
        {error && (
          <Notice tone="error" role="alert">
            {error}
          </Notice>
        )}
        {notice && <Notice role="status">{notice}</Notice>}
        {run && (
          <p className="flex items-center gap-2 text-sm">
            最近任务 <StatusBadge status={run.status} />
          </p>
        )}
      </PagePanel>
      <div className="studio-outline-browser">
        <aside
          className="studio-page grid content-start gap-3"
          aria-label="大纲版本目录"
        >
          <h3 className="font-semibold">项目大纲版本</h3>
          <p className="text-sm text-muted-foreground">
            各剧集共用此目录，确认版本后可分别起稿。
          </p>
          <ListPagination {...list} label="分集大纲" count={list.rows.length} />
          {!list.loading && !list.rows.length && (
            <p>尚无分集大纲，请先生成。</p>
          )}
          {list.rows.map((row) => (
            <Button
              key={row.id}
              variant={selected === row.id ? "secondary" : "ghost"}
              className="h-auto justify-start whitespace-normal text-left"
              disabled={busy}
              onClick={() => void show(row.id)}
            >
              大纲 v{row.version_no} ·{" "}
              {row.status === "APPROVED" ? "已确认" : "待确认"}
            </Button>
          ))}
        </aside>
        <section className="studio-page min-w-0" aria-label="大纲版本详情">
          {loading && <p role="status">正在读取大纲…</p>}
          {detailError && (
            <>
              <Notice tone="error">{detailError}</Notice>
              <Button variant="outline" onClick={() => void show(selected)}>
                重试大纲详情
              </Button>
            </>
          )}
          {!selected && (
            <p className="text-muted-foreground">
              选择一个版本，查看每集的目标、冲突、转折与结尾钩子。
            </p>
          )}
          {detail && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <h3 className="font-semibold">大纲 v{detail.version_no}</h3>
                <StatusBadge
                  status={
                    detail.status === "APPROVED"
                      ? "APPROVED"
                      : "READY_FOR_REVIEW"
                  }
                />
              </div>
              <p className="my-3 text-sm text-muted-foreground">
                {detail.content.target_episode_count} 集，每集{" "}
                {detail.content.episode_duration_ms / 1000} 秒 ·{" "}
                {new Date(detail.created_at).toLocaleString("zh-CN")}
                {detail.base_version_id && " · 从历史大纲另存"}
              </p>
              <details className="studio-disclosure mb-4">
                <summary>查看大纲来源与原文快照</summary>
                <p>来源：{detail.source_snapshot.source_name}</p>
                {detail.source_snapshot.analysis_id && (
                  <p className="break-all">
                    提炼 v{detail.source_snapshot.analysis_version_no}：
                    {detail.source_snapshot.analysis_id}
                  </p>
                )}
                <p className="break-all text-xs">
                  文本 SHA-256：{detail.source_snapshot.story_sha256}
                </p>
                {detail.source_snapshot.source_sha256 && (
                  <p className="break-all text-xs">
                    文件 SHA-256：{detail.source_snapshot.source_sha256}
                  </p>
                )}
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words font-sans text-sm">
                  {detail.source_snapshot.story}
                </pre>
              </details>
              <div className="mb-5 flex flex-wrap gap-2">
                {detail.status === "DRAFT" && (
                  <Button
                    disabled={!owner || !writable || blocked}
                    onClick={() => void confirm()}
                  >
                    确认此大纲版本
                  </Button>
                )}
                {detail.status === "APPROVED" && (
                  <Button
                    disabled={
                      !writable ||
                      blocked ||
                      detail.content.episode_duration_ms !==
                        episode.target_duration_ms ||
                      !detail.content.episodes.some(
                        (e) => e.episode_no === episode.episode_no,
                      )
                    }
                    onClick={() => void derive()}
                  >
                    从此大纲编写第{episode.episode_no}集剧本
                  </Button>
                )}
              </div>
              {detail.status === "DRAFT" && !owner && (
                <p className="mb-4 text-sm text-muted-foreground">
                  由项目 Owner 确认后才可用于编剧。
                </p>
              )}
              {detail.status === "APPROVED" &&
                (detail.content.episode_duration_ms !==
                  episode.target_duration_ms ||
                  !detail.content.episodes.some(
                    (e) => e.episode_no === episode.episode_no,
                  )) && (
                  <Notice>
                    当前剧集的集号或时长与此大纲不匹配，请调整剧集或另存大纲。
                  </Notice>
                )}
              <OutlineEditor
                key={detail.id}
                detail={detail}
                episodeId={episode.id}
                writable={writable}
                blocked={blocked}
                onSaved={async (result) => {
                  if (!live.current) return;
                  sequence.current++;
                  setSelected(result.id);
                  setDetail(result);
                  setNotice(
                    "已另存为大纲 v" +
                      result.version_no +
                      "，新版本需要重新确认。",
                  );
                  await refreshAfterSave();
                }}
              />
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function OutlineEditor({
  detail,
  episodeId,
  writable,
  blocked,
  onSaved,
}: {
  detail: Detail;
  episodeId: string;
  writable: boolean;
  blocked: boolean;
  onSaved: (detail: Detail) => Promise<void>;
}) {
  const draft = useSessionDraft({
    id: JSON.stringify([episodeId, "outline", detail.id]),
    episodeId,
    label: "大纲 v" + detail.version_no,
    initial: { content: detail.content, key: "" },
    validate: outlineDraftValid,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const saving = useRef(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const content = draft.value.content;
  function update(next: Content) {
    draft.update({ content: next, key: crypto.randomUUID() });
  }
  function entry(index: number, key: keyof Entry, value: string) {
    update({
      ...content,
      episodes: content.episodes.map((row, i) =>
        i === index ? { ...row, [key]: value } : row,
      ),
    });
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving.current || !writable || blocked) return;
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await api<Detail>(
        "/episodes/" + episodeId + "/story-outlines",
        {
          ...json("POST", { base_version_id: detail.id, content }),
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": draft.value.key,
          },
        },
      );
      draft.clear();
      if (live.current) await onSaved(result);
    } catch (e) {
      if (live.current) setError(message(e));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="grid gap-5" aria-label="分集大纲编辑">
      <p className="text-sm text-muted-foreground">
        修改会另存一个待确认版本，原版本和已启动的编剧任务保留。
        {draft.recovered && "已恢复此精确版本的本机草稿。"}
      </p>
      {draft.invalid && (
        <Notice tone="error" role="alert">
          此大纲版本的本机草稿已损坏，已加载服务器版本，请重新编辑后保存。
        </Notice>
      )}
      <Field>
        <FieldLabel htmlFor="outline-edit-duration">
          大纲单集时长（秒）
        </FieldLabel>
        <Input
          id="outline-edit-duration"
          type="number"
          min={2.5}
          max={1800}
          step={0.001}
          required
          value={content.episode_duration_ms / 1000}
          disabled={!writable || blocked || busy}
          onChange={(e) =>
            update({
              ...content,
              episode_duration_ms: Math.round(Number(e.target.value) * 1000),
            })
          }
        />
      </Field>
      {content.episodes.map((row, i) => (
        <fieldset
          key={row.episode_no}
          className="studio-outline-entry grid min-w-0 gap-3"
          disabled={!writable || blocked || busy}
        >
          <legend className="px-2 font-semibold">第{row.episode_no}集</legend>
          {(
            [
              ["title", "标题"],
              ["goal", "目标"],
              ["conflict", "冲突"],
              ["turning_point", "转折"],
              ["ending_hook", "结尾钩子"],
            ] as const
          ).map(([key, label]) => (
            <Field key={key}>
              <FieldLabel htmlFor={"outline-" + i + "-" + key}>
                {"第" + row.episode_no + "集" + label}
              </FieldLabel>
              {key === "title" ? (
                <Input
                  id={"outline-" + i + "-" + key}
                  value={row[key]}
                  maxLength={160}
                  required
                  onChange={(e) => entry(i, key, e.target.value)}
                />
              ) : (
                <Textarea
                  id={"outline-" + i + "-" + key}
                  value={row[key]}
                  maxLength={2000}
                  required
                  rows={2}
                  onChange={(e) => entry(i, key, e.target.value)}
                />
              )}
            </Field>
          ))}
        </fieldset>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={
            !writable || blocked || busy || content.episodes.length >= 1000
          }
          onClick={() =>
            update({
              ...content,
              target_episode_count: content.episodes.length + 1,
              episodes: [
                ...content.episodes,
                {
                  episode_no: content.episodes.length + 1,
                  title: "",
                  goal: "",
                  conflict: "",
                  turning_point: "",
                  ending_hook: "",
                },
              ],
            })
          }
        >
          追加一集
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={
            !writable || blocked || busy || content.episodes.length <= 1
          }
          onClick={() =>
            update({
              ...content,
              target_episode_count: content.episodes.length - 1,
              episodes: content.episodes.slice(0, -1),
            })
          }
        >
          移除末集
        </Button>
        <Button
          type="submit"
          disabled={!writable || blocked || busy || !draft.dirty}
        >
          另存大纲新版本
        </Button>
      </div>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
    </form>
  );
}
