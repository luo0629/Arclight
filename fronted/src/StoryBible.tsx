import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ChoiceSelect } from "@/components/choice-select";
import { FormLabel, Notice, PagePanel } from "@/components/ui-kit";
import {
  api,
  json,
  message,
  type StoryEntry,
  type StoryKind,
  type StoryRelation,
} from "./api";
import Dialog from "./Dialog";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";

const kinds: Record<StoryKind, string> = {
  WORLD: "世界观",
  CHARACTER: "角色",
  LOCATION: "地点",
  PROP: "物品",
  SCENE: "场景",
};
const links: Record<
  StoryRelation["relation"],
  { label: string; sources: StoryKind[]; targets: StoryKind[] }
> = {
  governs: {
    label: "约束",
    sources: ["WORLD"],
    targets: ["CHARACTER", "LOCATION", "PROP", "SCENE"],
  },
  located_in: {
    label: "位于",
    sources: ["CHARACTER", "PROP", "SCENE"],
    targets: ["LOCATION"],
  },
  appears_in: {
    label: "出现于",
    sources: ["CHARACTER", "PROP"],
    targets: ["SCENE"],
  },
  uses: { label: "使用", sources: ["CHARACTER", "SCENE"], targets: ["PROP"] },
};
type Props = { projectId: string; writable: boolean };
export default function StoryBible({ projectId, writable }: Props) {
  const path = "/projects/" + projectId + "/story-entries";
  const list = usePagedList<StoryEntry>(path);
  const [selected, setSelected] = useState<StoryEntry | null>(null);
  const [editorKey, setEditorKey] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState<(() => void) | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  function choose(row: StoryEntry | null) {
    const apply = () => {
      setSelected(row);
      setEditorKey((value) => value + 1);
      setDirty(false);
      setNotice("");
    };
    if (dirty) setPending(() => apply);
    else apply();
  }
  async function refresh() {
    if (busy) return;
    const apply = async () => {
      const rows = await list.reload();
      const latest = selected
        ? rows.find((row) => row.id === selected.id)
        : null;
      setSelected(latest ?? null);
      setEditorKey((value) => value + 1);
      setDirty(false);
    };
    if (dirty)
      setPending(() => () => {
        void apply().catch(() => {});
      });
    else await apply();
  }
  return (
    <PagePanel
      eyebrow="写清每一个小宇宙"
      title="故事设定"
      description="世界观、角色、地点、物品与场景使用稳定标识；新编剧任务保存设定快照，修改不回写历史稿件。"
    >
      <Notice tone="info">
        这不是视觉资产中心。只有绑定已批准的精确资产版本才构成正式资产引用；名称或设定
        ID 不等于资产。活动任务期间不可修改。
      </Notice>
      {!writable && (
        <Notice tone="info">当前项目只读，可查看设定与关联。</Notice>
      )}
      {notice && (
        <Notice tone="success" role="status">
          {notice}
        </Notice>
      )}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside
          className="flex min-w-0 flex-col gap-3"
          aria-label="故事设定目录"
        >
          {writable && (
            <Button disabled={busy} onClick={() => choose(null)}>
              新建设定
            </Button>
          )}
          {list.rows.map((row) => (
            <Button
              key={row.id}
              variant={selected?.id === row.id ? "secondary" : "outline"}
              disabled={busy}
              className="h-auto justify-start whitespace-normal break-all text-left"
              onClick={() => choose(row)}
            >
              {kinds[row.kind]} · {row.name}
              {row.archived ? "（已归档）" : ""}
            </Button>
          ))}
          <fieldset disabled={busy}>
            <ListPagination
              {...list}
              loading={busy || list.loading}
              reload={refresh}
              label="故事设定"
              count={list.rows.length}
            />
          </fieldset>
        </aside>
        <StoryEntryEditor
          key={editorKey}
          entry={selected}
          entries={list.rows}
          path={path}
          writable={writable}
          onDirty={setDirty}
          onBusy={setBusy}
          onSaved={async (row) => {
            setSelected(row);
            setDirty(false);
            setNotice("故事设定已保存");
            list.update(row);
            try {
              await list.reload();
            } catch {
              /* The mutation succeeded; list retry must never replay POST. */
            }
            setEditorKey((value) => value + 1);
          }}
        />
      </div>
      {pending && (
        <Dialog
          labelledBy="story-discard-title"
          busy={busy}
          onDismiss={() => setPending(null)}
        >
          <h2 id="story-discard-title">放弃未保存的设定？</h2>
          <p>继续会丢弃当前表单修改；保存失败的草稿也不会自动提交。</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setPending(null)}>
              继续编辑
            </Button>
            <Button
              onClick={() => {
                pending();
                setPending(null);
              }}
            >
              放弃修改并继续
            </Button>
          </div>
        </Dialog>
      )}
    </PagePanel>
  );
}
function StoryEntryEditor({
  entry,
  entries,
  path,
  writable,
  onDirty,
  onBusy,
  onSaved,
}: {
  entry: StoryEntry | null;
  entries: StoryEntry[];
  path: string;
  writable: boolean;
  onDirty: (dirty: boolean) => void;
  onBusy: (busy: boolean) => void;
  onSaved: (row: StoryEntry) => Promise<void>;
}) {
  const [kind, setKind] = useState<StoryKind>(entry?.kind ?? "WORLD");
  const [name, setName] = useState(entry?.name ?? "");
  const [description, setDescription] = useState(entry?.description ?? "");
  const [rules, setRules] = useState(entry?.rules.join("\n") ?? "");
  const [relations, setRelations] = useState<StoryRelation[]>(
    entry?.relations ?? [],
  );
  const [assetId, setAssetId] = useState(entry?.asset?.asset_id ?? "");
  const [versionId, setVersionId] = useState(
    entry?.asset?.asset_version_id ?? "",
  );
  const [relation, setRelation] = useState<StoryRelation["relation"]>(
    entry && entry.kind !== "WORLD" && entry.kind !== "LOCATION"
      ? "located_in"
      : "governs",
  );
  const [target, setTarget] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const readonly = !writable || !!entry?.archived;
  async function mutate(body: unknown, method: "POST" | "PUT" | "PATCH") {
    if (submitting.current || !writable) return;
    submitting.current = true;
    setBusy(true);
    onBusy(true);
    setError("");
    try {
      const row = await api<StoryEntry>(
        path + (entry ? "/" + entry.id : ""),
        json(method, body),
      );
      await onSaved(row);
    } catch (error) {
      setError(message(error));
    } finally {
      submitting.current = false;
      setBusy(false);
      onBusy(false);
    }
  }
  function save(event: FormEvent) {
    event.preventDefault();
    if (readonly) return;
    const parsedRules = rules
      .split("\n")
      .map((rule) => rule.trim())
      .filter(Boolean);
    if (
      parsedRules.length > 10 ||
      parsedRules.some((rule) => rule.length > 500)
    ) {
      setError("规则最多 10 条，每条最多 500 字符");
      return;
    }
    if (!!assetId.trim() !== !!versionId.trim()) {
      setError("资产 ID 与精确版本 ID 必须同时填写");
      return;
    }
    void mutate(
      {
        kind,
        name,
        description,
        rules: parsedRules,
        relations,
        asset: assetId.trim()
          ? { asset_id: assetId.trim(), asset_version_id: versionId.trim() }
          : null,
        ...(entry ? { expected_revision: entry.revision } : {}),
      },
      entry ? "PUT" : "POST",
    );
  }
  const choices = entries.filter(
    (row) =>
      !row.archived &&
      row.id !== entry?.id &&
      links[relation].targets.includes(row.kind),
  );
  return (
    <section aria-label="故事设定编辑" className="flex min-w-0 flex-col gap-4">
      <h2 className="text-lg font-semibold">
        {entry ? entry.name + " · 修订 " + entry.revision : "新建设定"}
      </h2>
      {entry && (
        <p className="break-all text-xs text-muted-foreground">
          稳定 ID：{entry.id}
          {entry.archived ? " · 已归档" : ""}
        </p>
      )}
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <form
        onSubmit={save}
        onChange={() => onDirty(true)}
        className="flex min-w-0 flex-col gap-4"
      >
        <fieldset
          disabled={busy || readonly}
          className="flex min-w-0 flex-col gap-4"
        >
          <FormLabel>
            设定类型
            <ChoiceSelect
              aria-label="设定类型"
              value={kind}
              disabled={!!entry || busy || readonly}
              options={Object.entries(kinds).map(([value, label]) => ({
                value,
                label,
              }))}
              onValueChange={(value) => {
                setKind(value as StoryKind);
                setRelations([]);
                setAssetId("");
                setVersionId("");
                setRelation(
                  value === "WORLD"
                    ? "governs"
                    : value === "LOCATION"
                      ? "governs"
                      : "located_in",
                );
                setTarget("");
                onDirty(true);
              }}
            />
          </FormLabel>
          <FormLabel>
            设定名称
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={100}
            />
          </FormLabel>
          <FormLabel>
            设定说明
            <Textarea
              aria-label="设定说明"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              maxLength={2000}
            />
          </FormLabel>
          <FormLabel>
            规则（每行一条，最多 10 条）
            <Textarea
              aria-label="规则（每行一条，最多 10 条）"
              value={rules}
              onChange={(event) => setRules(event.target.value)}
              maxLength={5009}
            />
          </FormLabel>
          <fieldset className="flex min-w-0 flex-col gap-2 rounded-xl border p-3">
            <legend>稳定关联</legend>
            {relations.map((link, index) => (
              <div
                className="flex min-w-0 flex-wrap items-center gap-2"
                key={link.relation + link.target_id}
              >
                <span className="min-w-0 break-all">
                  {links[link.relation].label}：
                  {entries.find((row) => row.id === link.target_id)?.name ??
                    link.target_id}
                </span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setRelations(relations.filter((_, i) => i !== index));
                    onDirty(true);
                  }}
                >
                  移除关联 {index + 1}
                </Button>
              </div>
            ))}
            {kind !== "LOCATION" && (
              <>
                <ChoiceSelect
                  aria-label="关联类型"
                  disabled={busy || readonly}
                  value={relation}
                  options={Object.entries(links)
                    .filter(([, value]) => value.sources.includes(kind))
                    .map(([value, info]) => ({ value, label: info.label }))}
                  onValueChange={(value) => {
                    setRelation(value as StoryRelation["relation"]);
                    setTarget("");
                  }}
                />
                <ChoiceSelect
                  aria-label="关联目标"
                  disabled={busy || readonly}
                  value={target}
                  placeholder="选择已加载的活动设定"
                  options={choices.map((row) => ({
                    value: row.id,
                    label: kinds[row.kind] + " · " + row.name,
                  }))}
                  onValueChange={setTarget}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={
                    !target ||
                    relations.length >= 20 ||
                    relations.some(
                      (link) =>
                        link.relation === relation && link.target_id === target,
                    )
                  }
                  onClick={() => {
                    setRelations([
                      ...relations,
                      { relation, target_id: target },
                    ]);
                    setTarget("");
                    onDirty(true);
                  }}
                >
                  添加关联
                </Button>
              </>
            )}
            <p className="text-xs text-muted-foreground">
              选择器只显示目录已加载的设定；可继续加载后再选择。归档被引用项前须先解除关联。
            </p>
          </fieldset>
          {kind !== "WORLD" && (
            <fieldset className="grid min-w-0 gap-3 rounded-xl border p-3">
              <legend>正式资产引用（可选）</legend>
              <FormLabel>
                资产 ID
                <Input
                  value={assetId}
                  onChange={(event) => setAssetId(event.target.value)}
                  maxLength={36}
                />
              </FormLabel>
              <FormLabel>
                资产精确版本 ID
                <Input
                  value={versionId}
                  onChange={(event) => setVersionId(event.target.value)}
                  maxLength={36}
                />
              </FormLabel>
              <p className="text-xs text-muted-foreground">
                仅接受当前项目、类型匹配、已批准或锁定且未失效的精确版本；不会自动创建或选用资产。
              </p>
            </fieldset>
          )}
          <Button disabled={busy || readonly} type="submit">
            {busy ? "保存中…" : "保存故事设定"}
          </Button>
        </fieldset>
      </form>
      {entry && writable && (
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            void mutate(
              { expected_revision: entry.revision, archived: !entry.archived },
              "PATCH",
            )
          }
        >
          {entry.archived ? "恢复故事设定" : "归档故事设定"}
        </Button>
      )}
    </section>
  );
}
