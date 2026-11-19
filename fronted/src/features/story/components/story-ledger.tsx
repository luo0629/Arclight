"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { Notice, FormLabel } from "@/components/ui-kit";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import { useDraftStore } from "@/providers/draft-provider";
import { ApiError, api, json, message } from "@/lib/api/client";
import type { StoryEntry } from "@/types/api";
import ImpactButton from "@/features/dependencies/components/impact-button";
import StoryLedgerEditor from "./story-ledger-editor";
import {
  emptyLedger,
  isLedgerContent,
  ledgerKinds,
  ledgerStatuses,
  sceneLabel,
  type LedgerApproval,
  type LedgerContent,
  type LedgerHead,
  type LedgerPage,
  type LedgerQuery,
  type LedgerResult,
  type LedgerSource,
  type LedgerVersion,
  type LedgerVersionSummary,
} from "../story-ledger";

type Draft = {
  expected_revision: number;
  base_version_id: string | null;
  content: LedgerContent;
  key: string;
};
type ApprovalAttempt = {
  version_id: string;
  expected_revision: number;
  impact_digest?: string;
  key: string;
};
function validDraft(value: unknown): value is Draft {
  if (!value || typeof value !== "object") return false;
  const draft = value as Draft;
  return (
    Number.isInteger(draft.expected_revision) &&
    typeof draft.key === "string" &&
    (draft.base_version_id === null ||
      typeof draft.base_version_id === "string") &&
    isLedgerContent(draft.content)
  );
}
function validApproval(value: unknown): value is ApprovalAttempt {
  if (!value || typeof value !== "object") return false;
  const row = value as ApprovalAttempt;
  return (
    typeof row.version_id === "string" &&
    typeof row.key === "string" &&
    Number.isInteger(row.expected_revision) &&
    (row.impact_digest === undefined ||
      /^[a-f0-9]{64}$/.test(row.impact_digest))
  );
}

// These endpoints return an explicit total rather than the shared bare-array contract.
function useLedgerPages<T extends { id?: string; script_version_id?: string }>(
  path: string,
) {
  const [rows, setRows] = useState<T[]>([]);
  const [total, setTotal] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [batchSize, setBatchSize] = useState(20);
  const size = useRef(batchSize);
  const loadedRows = useRef<T[]>([]);
  const rawOffset = useRef(0);
  const controller = useRef<AbortController | null>(null);
  size.current = batchSize;
  const load = useCallback(
    async (append: boolean) => {
      if (append && controller.current) return;
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      setLoading(true);
      setError("");
      const offset = append ? rawOffset.current : 0;
      try {
        const page = await api<LedgerPage<T>>(
          `${path}?limit=${size.current}&offset=${offset}`,
          { signal: abort.signal },
        );
        if (abort.signal.aborted) return;
        rawOffset.current = offset + page.items.length;
        const unique = new Map(
          (append ? loadedRows.current : []).map((row) => [
            row.id ?? row.script_version_id,
            row,
          ]),
        );
        page.items.forEach((row) =>
          unique.set(row.id ?? row.script_version_id, row),
        );
        const next = [...unique.values()];
        loadedRows.current = next;
        setRows(next);
        setTotal(page.total);
        setLoaded(true);
      } catch (error) {
        if (!abort.signal.aborted) setError(message(error));
      } finally {
        if (!abort.signal.aborted) {
          controller.current = null;
          setLoading(false);
        }
      }
    },
    [path],
  );
  const reload = useCallback(() => load(false), [load]);
  const more = useCallback(() => load(true), [load]);
  useEffect(() => {
    void reload();
    return () => controller.current?.abort();
  }, [reload]);
  return {
    rows,
    total,
    loaded,
    loading,
    error,
    batchSize,
    setBatchSize,
    hasMore: rawOffset.current < total,
    reload,
    more,
  };
}

export default function StoryLedger({
  projectId,
  owner,
  writable,
}: {
  projectId: string;
  owner: boolean;
  writable: boolean;
}) {
  const path = `/projects/${projectId}/story-ledger`;
  const versions = useLedgerPages<LedgerVersionSummary>(path + "/versions");
  const sources = useLedgerPages<LedgerSource>(path + "/sources");
  const entities = usePagedList<StoryEntry>(
    `/projects/${projectId}/story-entries`,
  );
  const store = useDraftStore();
  const draftId = `story-ledger:${projectId}`;
  const approvalId = `story-ledger-approval:${projectId}`;
  const [draft, setDraft] = useState<Draft | null>(() =>
    validDraft(store.drafts[draftId]?.value)
      ? store.drafts[draftId].value
      : null,
  );
  const [recovered, setRecovered] = useState(() => !!store.drafts[draftId]);
  const [head, setHead] = useState<LedgerHead | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [version, setVersion] = useState<LedgerVersion | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState(false);
  const [discardInvalid, setDiscardInvalid] = useState(false);
  const [headLoading, setHeadLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const active = useRef(true);
  const submitting = useRef(false);
  const headController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const approvalAttempt = useRef<ApprovalAttempt | null>(
    validApproval(store.drafts[approvalId]?.value)
      ? store.drafts[approvalId].value
      : null,
  );

  const reloadHead = useCallback(async () => {
    headController.current?.abort();
    const abort = new AbortController();
    headController.current = abort;
    setHeadLoading(true);
    setError("");
    try {
      const next = await api<LedgerHead>(path, { signal: abort.signal });
      if (!abort.signal.aborted && active.current) {
        setHead(next);
        setSelectedId((current) => current || next.latest_version_id || "");
      }
    } catch (error) {
      if (!abort.signal.aborted && active.current) setError(message(error));
    } finally {
      if (!abort.signal.aborted && active.current) setHeadLoading(false);
    }
  }, [path]);

  useEffect(() => {
    active.current = true;
    void reloadHead();
    void entities.reload().catch(() => {});
    return () => {
      active.current = false;
      headController.current?.abort();
      detailController.current?.abort();
    };
  }, [reloadHead, entities.reload]);
  useEffect(() => {
    detailController.current?.abort();
    setVersion(null);
    if (!selectedId) {
      setDetailLoading(false);
      return;
    }
    const abort = new AbortController();
    detailController.current = abort;
    setDetailLoading(true);
    void api<LedgerVersion>(path + "/versions/" + selectedId, {
      signal: abort.signal,
    })
      .then((row) => {
        if (
          !abort.signal.aborted &&
          active.current &&
          row.id === selectedId &&
          row.project_id === projectId
        )
          setVersion(row);
      })
      .catch((error) => {
        if (!abort.signal.aborted && active.current) setError(message(error));
      })
      .finally(() => {
        if (!abort.signal.aborted && active.current) setDetailLoading(false);
      });
    return () => abort.abort();
  }, [path, projectId, selectedId]);

  useEffect(() => {
    const attempt = approvalAttempt.current;
    if (version?.approval && attempt?.version_id === version.id) {
      approvalAttempt.current = null;
      store.write(approvalId, undefined, attempt);
    }
  }, [version, approvalId, store]);

  function writeDraft(next: Draft) {
    setDraft(next);
    const episodeId =
      next.content.scene_times[0]?.episode_id ??
      store.drafts[draftId]?.episodeId;
    if (episodeId)
      store.write(draftId, { episodeId, label: "剧情账本草稿", value: next });
  }
  function beginDraft() {
    if (
      !head ||
      draft ||
      !writable ||
      (store.drafts[draftId] && !validDraft(store.drafts[draftId].value))
    )
      return;
    const content = version ? structuredClone(version.content) : emptyLedger();
    writeDraft({
      expected_revision: head.revision,
      base_version_id: version?.id ?? null,
      content,
      key: crypto.randomUUID(),
    });
    setRecovered(false);
    setError("");
    setNotice("");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || !writable || submitting.current) return;
    if (!draft.content.scene_times.length) {
      setError("请先添加至少一个精确批准来源的时序映射。");
      return;
    }
    for (const [index, entry] of draft.content.entries.entries()) {
      const prefix = "条目 " + (index + 1) + "：";
      const invalid =
        !entry.state_key.trim() || !entry.value.trim()
          ? "请填写状态名称和具体内容。"
          : entry.kind !== "EVENT" && !entry.subject_id
            ? "请选择主体设定。"
            : entry.story_end !== null && entry.story_end <= entry.story_start
              ? "结束时点须晚于生效时点。"
              : entry.status !== "AUTHOR_PLANNED" &&
                  (!entry.source_script_version_id || !entry.source_scene_id)
                ? "待核实与确认条目须选择精确来源。"
                : !draft.content.scene_times.some(
                      (scene) => scene.id === entry.disclosure_start,
                    )
                  ? "请选择首次披露位置。"
                  : entry.known_by.some(
                        (row) =>
                          !row.character_id ||
                          (row.story_end !== null &&
                            row.story_end <= row.story_start),
                      )
                    ? "请补全知情角色，并检查知情范围。"
                    : "";
      if (invalid) {
        setError(prefix + invalid);
        return;
      }
    }
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const request = draft;
    try {
      const response = await api<{ head: LedgerHead; version: LedgerVersion }>(
        path + "/versions",
        {
          ...json("POST", {
            expected_revision: request.expected_revision,
            base_version_id: request.base_version_id,
            content: request.content,
          }),
          headers: { "Idempotency-Key": request.key },
        },
      );
      if (!active.current) return;
      store.write(draftId, undefined, request);
      setDraft(null);
      setRecovered(false);
      setHead(response.head);
      setSelectedId(response.version.id);
      setVersion(response.version);
      setNotice("账本版本已保存。条目须在精确版本批准后才能作为事实。");
      void versions.reload();
    } catch (error) {
      if (active.current)
        setError(
          message(error) + "。草稿、原修订和重试凭据已保留；请先核对账本状态。",
        );
    } finally {
      submitting.current = false;
      if (active.current) setBusy(false);
    }
  }
  async function approve(impact_digest?: string) {
    if (!version || !head || !owner || !writable || submitting.current)
      return false;
    const request =
      approvalAttempt.current?.version_id === version.id
        ? approvalAttempt.current
        : {
            version_id: version.id,
            expected_revision: head.revision,
            impact_digest,
            key: crypto.randomUUID(),
          };
    approvalAttempt.current = request;
    const episodeId = version.content.scene_times[0]?.episode_id;
    if (episodeId)
      store.write(approvalId, {
        episodeId,
        label: "剧情账本批准待确认",
        value: request,
      });
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const response = await api<{
        head: LedgerHead;
        approval: LedgerApproval;
      }>(path + "/versions/" + request.version_id + "/approve", {
        ...json("POST", {
          expected_revision: request.expected_revision,
          impact_digest: request.impact_digest,
        }),
        headers: { "Idempotency-Key": request.key },
      });
      if (!active.current) return;
      store.write(approvalId, undefined, request);
      approvalAttempt.current = null;
      setHead(response.head);
      setVersion((current) =>
        current?.id === request.version_id
          ? { ...current, approval: response.approval }
          : current,
      );
      setNotice("精确账本版本已批准，历史版本保持不变。");
      void versions.reload();
      void reloadHead();
    } catch (error) {
      if (
        error instanceof ApiError &&
        ["DEPENDENCY_IMPACT_CHANGED", "DEPENDENCY_IMPACT_REQUIRED"].includes(
          error.code,
        )
      ) {
        approvalAttempt.current = null;
        store.write(approvalId, undefined, request);
        if (active.current) setError(message(error));
      } else if (active.current) {
        setError(message(error) + "。批准请求与重试凭据已保留。");
      }
      return false;
    } finally {
      submitting.current = false;
      if (active.current) setBusy(false);
    }
  }
  const entityName = (id: string | null) =>
    entities.rows.find((row) => row.id === id)?.name ?? id ?? "";
  return (
    <section
      aria-label="剧情事件与状态账本"
      className="grid min-w-0 gap-5 border-t pt-6"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="grid min-w-0 gap-2">
          <h2 className="text-lg font-semibold">剧情事件与状态账本</h2>
          <p className="max-w-prose text-sm text-muted-foreground">
            将故事时序、精确来源和披露范围写入不可变版本。作者计划、待核实提议与批准事实分别查询。
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={busy || headLoading}
          onClick={() => {
            void reloadHead();
            void versions.reload();
          }}
        >
          重新载入账本状态
        </Button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {notice && <Notice tone="success">{notice}</Notice>}
      {store.drafts[draftId] && !validDraft(store.drafts[draftId].value) && (
        <Notice tone="error">
          <p>本机账本草稿格式无效。请先保留备份，再放弃旧草稿并重新起草。</p>
          {writable && (
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                if (!discardInvalid) {
                  setDiscardInvalid(true);
                  return;
                }
                store.write(draftId);
                setDiscardInvalid(false);
                setRecovered(false);
              }}
            >
              {discardInvalid ? "确认删除无效账本草稿" : "放弃无效账本草稿"}
            </Button>
          )}
        </Notice>
      )}
      {!writable && <Notice>当前项目只读，可查看历史版本与时点查询。</Notice>}
      {head && (
        <p className="text-sm text-muted-foreground">
          账本修订 {head.revision}；
          {head.approved_version_id ? "已有批准版本" : "尚无批准版本"}
        </p>
      )}
      <div className="grid min-w-0 gap-5 lg:grid-cols-[minmax(12rem,1fr)_minmax(0,3fr)]">
        <aside
          className="grid min-w-0 content-start gap-3"
          aria-label="剧情账本版本目录"
        >
          <h3 className="font-semibold">版本记录</h3>
          {versions.loaded && !versions.rows.length && (
            <p className="text-sm text-muted-foreground">
              尚无账本版本。请先起草并添加时序映射。
            </p>
          )}
          {versions.rows.map((row) => (
            <Button
              key={row.id}
              type="button"
              className="h-auto justify-start whitespace-normal text-left"
              variant={selectedId === row.id ? "secondary" : "ghost"}
              aria-pressed={selectedId === row.id}
              disabled={busy}
              onClick={() => {
                setSelectedId(row.id);
                setError("");
              }}
            >
              版本 {row.version_no}
              {row.id === head?.latest_version_id ? " · 最新" : ""}
              {row.approval ? " · 已批准" : " · 未批准"}
              {row.id === head?.approved_version_id ? " · 当前事实依据" : ""}
            </Button>
          ))}
          <ListPagination
            {...versions}
            label="账本版本"
            count={versions.rows.length}
          />
        </aside>
        <div className="grid min-w-0 content-start gap-4">
          {detailLoading && <p role="status">正在读取精确账本版本…</p>}
          {version && !detailLoading && (
            <section
              aria-label="剧情账本版本详情"
              className="grid min-w-0 gap-3"
            >
              <h3 className="font-semibold">
                账本版本 {version.version_no}
                {version.approval ? "（已批准）" : "（未批准）"}
              </h3>
              <p className="break-all text-xs text-muted-foreground">
                版本：{version.id}
                <br />
                内容校验：{version.content_sha256}
                <br />
                基于版本：{version.base_version_id ?? "初始版本"}
              </p>
              {version.approval && (
                <p className="break-all text-xs text-muted-foreground">
                  批准凭证：{version.approval.id}；批准时间：
                  {version.approval.approved_at}
                </p>
              )}
              <p className="text-sm">
                {version.content.scene_times.length} 项时序映射，
                {version.content.entries.length} 条记录
              </p>
              <details className="studio-disclosure">
                <summary>冻结来源与精确批准依据</summary>
                <pre className="max-h-72 min-w-0 overflow-auto whitespace-pre-wrap break-all text-xs">
                  {JSON.stringify(version.source_manifest, null, 2)}
                </pre>
              </details>
              <details className="studio-disclosure">
                <summary>查看此版本的映射与记录</summary>
                <div className="grid min-w-0 gap-3 py-3">
                  {version.content.scene_times.map((scene) => (
                    <p key={scene.id} className="text-sm">
                      {sceneLabel(scene)}
                    </p>
                  ))}
                  {version.content.entries.map((entry) => (
                    <div
                      key={entry.id}
                      className="grid min-w-0 gap-1 border-t pt-2 text-sm"
                    >
                      <p className="font-medium">
                        {ledgerKinds[entry.kind]} · {entry.state_key} ·{" "}
                        {ledgerStatuses[entry.status]}
                      </p>
                      <p className="break-words">
                        {entityName(entry.subject_id)} {entry.value}
                      </p>
                      <p className="text-muted-foreground">
                        实际范围 [{entry.story_start},{" "}
                        {entry.story_end ?? "持续"})；
                        {version.content.scene_times.find(
                          (scene) => scene.id === entry.disclosure_start,
                        )
                          ? sceneLabel(
                              version.content.scene_times.find(
                                (scene) => scene.id === entry.disclosure_start,
                              )!,
                            )
                          : "缺少披露映射"}
                      </p>
                    </div>
                  ))}
                </div>
              </details>
              {owner &&
                writable &&
                version.id === head?.latest_version_id &&
                !version.approval && (
                  <ImpactButton
                    type="button"
                    className="justify-self-start"
                    disabled={busy || !!draft}
                    endpoint={
                      approvalAttempt.current?.version_id === version.id
                        ? undefined
                        : `${path}/versions/${version.id}/approval-impact`
                    }
                    onConfirm={approve}
                  >
                    {busy ? "处理中…" : "批准此精确版本"}
                  </ImpactButton>
                )}
              {!owner && writable && (
                <p className="text-sm text-muted-foreground">
                  编辑者可起草，批准由项目拥有者完成。
                </p>
              )}
              <LedgerQueryPanel
                key={version.id + ":" + (version.approval?.id ?? "unapproved")}
                path={path}
                version={version}
                entities={entities.rows}
              />
            </section>
          )}
          {writable && !draft && (
            <Button
              type="button"
              className="justify-self-start"
              disabled={
                !head ||
                busy ||
                detailLoading ||
                (!!store.drafts[draftId] &&
                  !validDraft(store.drafts[draftId].value)) ||
                (!!head.latest_version_id && !version)
              }
              onClick={beginDraft}
            >
              {version ? "以此版本起草" : "新建账本草稿"}
            </Button>
          )}
        </div>
      </div>

      <details className="studio-disclosure">
        <summary>加载更多查询角色与主体设定</summary>
        <ListPagination
          {...entities}
          label="账本设定"
          count={entities.rows.length}
        />
      </details>
      {draft && (
        <form
          onSubmit={save}
          aria-label="剧情账本草稿"
          className="grid min-w-0 gap-4 border-t pt-5"
        >
          <h3 className="font-semibold">账本草稿</h3>
          {recovered && (
            <Notice>已恢复本机草稿，原基础版本、修订和重试凭据均保留。</Notice>
          )}
          <p className="break-all text-sm text-muted-foreground">
            基于修订 {draft.expected_revision}；基础版本：
            {draft.base_version_id ?? "初始版本"}。修改会保存为新版本。
          </p>
          {head && head.revision !== draft.expected_revision && (
            <Notice tone="error">
              当前账本已到修订 {head.revision}，草稿仍基于修订{" "}
              {draft.expected_revision}
              。请查看最新版本并比对；不会自动覆盖或切换基础版本。
            </Notice>
          )}
          {store.drafts[draftId] &&
            !validDraft(store.drafts[draftId].value) && (
              <Notice tone="error">
                旧草稿格式无效，请保留备份后重新起草。
              </Notice>
            )}
          <StoryLedgerEditor
            content={draft.content}
            sources={sources.rows}
            entities={entities.rows}
            disabled={busy || !writable}
            onChange={(content) =>
              writeDraft({
                ...draft,
                content,
                key:
                  JSON.stringify(content) === JSON.stringify(draft.content)
                    ? draft.key
                    : crypto.randomUUID(),
              })
            }
          />
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <ListPagination
              {...sources}
              label="批准来源"
              count={sources.rows.length}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            选择器只显示已加载项目，可继续加载来源和设定。首次添加映射后，草稿按项目与精确基础版本保存在本机。
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              disabled={busy || !writable || !draft.content.scene_times.length}
            >
              {busy ? "保存中…" : "保存账本新版本"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => setDiscard(true)}
            >
              放弃账本草稿
            </Button>
          </div>
          {discard && (
            <Notice>
              <p>放弃会删除此项目保留的账本草稿。已保存版本不受影响。</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setDiscard(false)}
                >
                  继续编辑账本
                </Button>
                <Button
                  type="button"
                  onClick={() => {
                    store.write(draftId, undefined, draft);
                    setDraft(null);
                    setRecovered(false);
                    setDiscard(false);
                  }}
                >
                  确认放弃账本草稿
                </Button>
              </div>
            </Notice>
          )}
        </form>
      )}
    </section>
  );
}

function LedgerQueryPanel({
  path,
  version,
  entities,
}: {
  path: string;
  version: LedgerVersion;
  entities: StoryEntry[];
}) {
  const [targetId, setTargetId] = useState("");
  const [perspective, setPerspective] = useState<"AUTHOR" | "CHARACTER">(
    "AUTHOR",
  );
  const [characterId, setCharacterId] = useState("");
  const [planned, setPlanned] = useState(false);
  const [proposed, setProposed] = useState(false);
  const [result, setResult] = useState<LedgerResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [offsets, setOffsets] = useState<number[]>([]);
  const controller = useRef<AbortController | null>(null);
  const submitted = useRef<LedgerQuery | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  function invalidate() {
    controller.current?.abort();
    setBusy(false);
    setResult(null);
    setError("");
    setOffsets([]);
    submitted.current = null;
  }
  async function query(
    event?: FormEvent,
    offset = 0,
    previousOffsets: number[] = [],
  ) {
    event?.preventDefault();
    const target = version.content.scene_times.find(
      (scene) => scene.id === targetId,
    );
    if (!target || (perspective === "CHARACTER" && !characterId)) {
      setError("请选择精确查询位置，角色视角还须选择角色。");
      return;
    }
    const request: LedgerQuery =
      offset && submitted.current
        ? submitted.current
        : {
            episode_id: target.episode_id,
            script_version_id: target.script_version_id,
            scene_id: target.scene_id,
            segment_no: target.segment_no,
            perspective,
            ...(perspective === "CHARACTER"
              ? { character_id: characterId }
              : {}),
            include_planned: perspective === "AUTHOR" && planned,
            include_proposed: perspective === "AUTHOR" && proposed,
          };
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const next = await api<LedgerResult>(path + "/query", {
        ...json("POST", {
          version_id: version.id,
          query: request,
          limit: 20,
          offset,
        }),
        signal: abort.signal,
      });
      if (!abort.signal.aborted && next.version_id === version.id) {
        setResult(next);
        setOffsets(previousOffsets);
        submitted.current = request;
      }
    } catch (error) {
      if (!abort.signal.aborted) setError(message(error));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  return (
    <section
      aria-label="剧情账本时点查询"
      className="grid min-w-0 gap-3 border-t pt-4"
    >
      <h4 className="font-semibold">按时点与视角查询</h4>
      <form
        onSubmit={(event) => void query(event)}
        className="grid min-w-0 gap-3"
      >
        <FormLabel>
          查询位置
          <NativeSelect
            className="w-full"
            aria-label="账本查询位置"
            value={targetId}
            onChange={(event) => {
              invalidate();
              setTargetId(event.target.value);
            }}
            required
          >
            <option value="">选择此版本的精确集、场与片段</option>
            {version.content.scene_times.map((scene) => (
              <option key={scene.id} value={scene.id}>
                {sceneLabel(scene)}
              </option>
            ))}
          </NativeSelect>
        </FormLabel>
        <FormLabel>
          查询视角
          <NativeSelect
            className="w-full"
            aria-label="账本查询视角"
            value={perspective}
            onChange={(event) => {
              invalidate();
              setPerspective(event.target.value as "AUTHOR" | "CHARACTER");
              setPlanned(false);
              setProposed(false);
              setCharacterId("");
            }}
          >
            <option value="AUTHOR">作者视角</option>
            <option value="CHARACTER">角色所知</option>
          </NativeSelect>
        </FormLabel>
        {perspective === "CHARACTER" && (
          <FormLabel>
            视角角色
            <NativeSelect
              className="w-full"
              aria-label="账本视角角色"
              value={characterId}
              onChange={(event) => {
                invalidate();
                setCharacterId(event.target.value);
              }}
              required
            >
              <option value="">选择已加载的角色</option>
              {entities
                .filter((row) => row.kind === "CHARACTER")
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                    {row.archived ? "（历史角色）" : ""}
                  </option>
                ))}
            </NativeSelect>
          </FormLabel>
        )}
        {perspective === "AUTHOR" && (
          <div className="grid gap-2 text-sm">
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={planned}
                onChange={(event) => {
                  invalidate();
                  setPlanned(event.target.checked);
                }}
              />
              单独显示作者计划（可能包含未来计划）
            </label>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={proposed}
                onChange={(event) => {
                  invalidate();
                  setProposed(event.target.checked);
                }}
              />
              单独显示待核实提议
            </label>
          </div>
        )}
        <Button type="submit" className="justify-self-start" disabled={busy}>
          {busy ? "查询中…" : "查询此时点"}
        </Button>
      </form>
      {error && <Notice tone="error">{error}</Notice>}
      {result && (
        <section aria-label="账本受控查询结果" className="grid min-w-0 gap-3">
          <p className="text-sm">
            版本 {result.version_no}；{sceneLabel(result.target)}；
            {result.perspective === "CHARACTER" ? "角色所知" : "作者视角"}
          </p>
          <p className="break-all text-xs text-muted-foreground">
            批准凭证：{result.approval_id ?? "无，当前版本不能提供批准事实"}
            ；内容校验：{result.content_sha256}
          </p>
          {(
            [
              ["此时已确认事实", result.facts],
              ["作者计划", result.planned],
              ["待核实提议", result.proposals],
            ] as const
          ).map(([label, entries]) => (
            <section
              key={label}
              aria-label={label}
              className="grid min-w-0 gap-2"
            >
              <h5 className="text-sm font-semibold">{label}</h5>
              {!entries.length && (
                <p className="text-sm text-muted-foreground">此页无记录</p>
              )}
              {entries.map((entry) => (
                <p key={entry.id} className="break-words text-sm">
                  {ledgerKinds[entry.kind]} · {entry.state_key}：{entry.value}
                </p>
              ))}
            </section>
          ))}
          <p className="text-xs text-muted-foreground">
            共 {result.total} 条；此页从第{" "}
            {result.total ? result.offset + 1 : 0} 条起，实际返回{" "}
            {result.returned_count} 条。结果由服务端按披露范围与知情权限筛选。
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={busy || !offsets.length}
              onClick={() =>
                void query(
                  undefined,
                  offsets[offsets.length - 1],
                  offsets.slice(0, -1),
                )
              }
            >
              上一页查询结果
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || result.next_offset === null}
              onClick={() => {
                if (result.next_offset !== null)
                  void query(undefined, result.next_offset, [
                    ...offsets,
                    result.offset,
                  ]);
              }}
            >
              下一页查询结果
            </Button>
          </div>
        </section>
      )}
    </section>
  );
}
