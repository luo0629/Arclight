import { useEffect, useRef, useState, type FormEvent } from "react";
import { Box, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ChoiceSelect } from "@/components/choice-select";
import {
  EmptyState,
  Notice,
  PagePanel,
  StatusBadge,
} from "@/components/ui-kit";
import {
  api,
  json,
  message,
  type VisualAsset,
  type VisualAssetType,
} from "./api";
import Dialog from "./Dialog";
import ListPagination from "./ListPagination";
import usePagedList from "./usePagedList";
import AssetMetadataPanel from "./AssetMetadata";
import CharacterPanel from "./CharacterProfile";
import ScenePropPanel from "./ScenePropProfile";
import AssetVersions from "./AssetVersions";

const kinds: Record<VisualAssetType, string> = {
  CHARACTER: "角色",
  SCENE: "场景",
  PROP: "道具",
  STYLE: "风格",
};
type Editor = { asset: VisualAsset | null };

export default function VisualAssets({
  projectId,
  writable,
  owner,
}: {
  projectId: string;
  writable: boolean;
  owner: boolean;
}) {
  const path = `/projects/${projectId}/assets`;
  const list = usePagedList<VisualAsset>(path);
  const [selected, setSelected] = useState<VisualAsset | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [archive, setArchive] = useState<VisualAsset | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  const request = useRef(0);
  const submitting = useRef(false);
  useEffect(() => {
    live.current = true;
    void list.reload().catch(() => {});
    return () => {
      live.current = false;
    };
  }, [list.reload]);
  async function choose(row: VisualAsset) {
    const sequence = ++request.current;
    setError("");
    try {
      const latest = await api<VisualAsset>(`${path}/${row.id}`);
      if (live.current && sequence === request.current) {
        setSelected(latest);
        list.update(latest);
      }
    } catch (error) {
      if (live.current && sequence === request.current)
        setError(message(error));
    }
  }
  async function refresh() {
    const rows = await list.reload();
    if (live.current)
      setSelected((current) =>
        current ? (rows.find((row) => row.id === current.id) ?? current) : null,
      );
  }
  async function saved(row: VisualAsset) {
    if (!live.current) return;
    ++request.current;
    setSelected(row);
    setEditor(null);
    setArchive(null);
    list.update(row);
    setNotice("视觉资产已保存");
    // A successful mutation stays successful even if refreshing the directory fails.
    try {
      await list.reload();
    } catch {
      /* Retry the directory, never replay a creation. */
    }
  }
  async function changeArchive() {
    if (!archive || !writable || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const row = await api<VisualAsset>(
        `${path}/${archive.id}`,
        json("PATCH", {
          archived: archive.status !== "ARCHIVED",
          expected_revision: archive.revision,
        }),
      );
      await saved(row);
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  const rows = list.rows.filter(
    (row) =>
      (filter === "ALL" || row.asset_type === filter) &&
      row.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const editable =
    writable && selected && !["ARCHIVED", "LOCKED"].includes(selected.status);
  return (
    <PagePanel
      className="studio-page"
      eyebrow="项目共用"
      title="视觉资产"
      description="管理角色、场景、道具与风格的目录、结构化资料和使用范围。媒体生成、版本选用和锁定尚未开放。"
    >
      {!writable && <Notice tone="info">当前项目只读，可查看视觉资产。</Notice>}
      {notice && (
        <Notice tone="success" role="status">
          {notice}
        </Notice>
      )}
      {error && !archive && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <div className="studio-split">
        <aside className="studio-index" aria-label="视觉资产目录">
          <div className="studio-index-heading">
            <Box />
            <h3>资产目录</h3>
            <span>{list.rows.length} 项已载入</span>
          </div>
          {writable && (
            <Button
              onClick={() => {
                setError("");
                setEditor({ asset: null });
              }}
            >
              <Plus data-icon="inline-start" />
              新建视觉资产
            </Button>
          )}
          <Input
            aria-label="搜索已载入资产"
            placeholder="搜索已载入资产…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <ChoiceSelect
            aria-label="筛选资产类型"
            value={filter}
            onValueChange={setFilter}
            options={[
              { value: "ALL", label: "全部类型" },
              ...Object.entries(kinds).map(([value, label]) => ({
                value,
                label,
              })),
            ]}
          />
          <ScrollArea className="studio-index-scroll">
            <div className="studio-index-items">
              {rows.map((row) => (
                <Button
                  key={row.id}
                  className="h-auto justify-start whitespace-normal break-all text-left"
                  variant={selected?.id === row.id ? "secondary" : "ghost"}
                  aria-pressed={selected?.id === row.id}
                  onClick={() => void choose(row)}
                >
                  {kinds[row.asset_type]} · {row.name}
                  {row.status === "ARCHIVED" ? "（已归档）" : ""}
                </Button>
              ))}
              {list.loaded && !rows.length && (
                <p className="p-3 text-sm text-muted-foreground">
                  没有匹配的已载入资产。
                </p>
              )}
            </div>
          </ScrollArea>
          <ListPagination
            {...list}
            reload={refresh}
            label="视觉资产"
            count={list.rows.length}
          />
        </aside>
        <section
          className="studio-detail flex min-w-0 flex-col gap-4"
          aria-label="视觉资产详情"
        >
          {selected ? (
            <>
              <h2 className="break-all text-lg font-semibold">
                {selected.name} · 修订 {selected.revision}
              </h2>
              <StatusBadge status={selected.status} />
              <dl className="grid min-w-0 gap-3 break-all text-sm">
                <div>
                  <dt className="text-muted-foreground">资产类型</dt>
                  <dd>{kinds[selected.asset_type]}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">稳定 ID</dt>
                  <dd>{selected.id}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">当前版本 ID</dt>
                  <dd>{selected.current_version_id ?? "尚未选用媒体版本"}</dd>
                </div>
              </dl>
              <AssetMetadataPanel
                key={selected.id}
                asset={selected}
                path={path}
                writable={writable}
                onSaved={saved}
              />
              <AssetVersions
                key={"versions-" + selected.id}
                asset={selected}
                path={path}
                writable={writable}
                owner={owner}
                onSaved={saved}
              />
              {selected.asset_type === "CHARACTER" && (
                <CharacterPanel
                  key={"character-" + selected.id}
                  asset={selected}
                  path={path}
                  writable={writable}
                  onSaved={saved}
                />
              )}
              {["SCENE", "PROP"].includes(selected.asset_type) && (
                <ScenePropPanel
                  key={"scene-prop-" + selected.id}
                  asset={selected}
                  path={path}
                  writable={writable}
                  onSaved={saved}
                />
              )}
              <p className="text-sm text-muted-foreground">
                目录修改保留已有媒体版本、选用指针与历史引用。归档后不能用于新的文本资产引用；恢复保留归档前状态。
              </p>
              {writable && (
                <div className="flex flex-wrap gap-2">
                  <Button
                    disabled={!editable}
                    onClick={() => setEditor({ asset: selected })}
                  >
                    编辑资产名称
                  </Button>
                  <Button
                    variant="outline"
                    disabled={selected.status === "LOCKED"}
                    onClick={() => {
                      setError("");
                      setArchive(selected);
                    }}
                  >
                    {selected.status === "ARCHIVED"
                      ? "恢复视觉资产"
                      : "归档视觉资产"}
                  </Button>
                </div>
              )}
            </>
          ) : (
            <EmptyState>
              <Box />
              <h2>选择一项视觉资产</h2>
              <p>创建目录后可以编辑名称、归档或恢复。</p>
            </EmptyState>
          )}
        </section>
      </div>
      {editor && (
        <AssetEditor
          asset={editor.asset}
          path={path}
          writable={writable}
          onDismiss={() => setEditor(null)}
          onSaved={saved}
        />
      )}
      {archive && (
        <Dialog
          labelledBy="asset-archive-title"
          busy={busy}
          onDismiss={() => setArchive(null)}
        >
          <h2 id="asset-archive-title">
            {archive.status === "ARCHIVED" ? "恢复" : "归档"}视觉资产？
          </h2>
          <p className="break-all">
            {archive.name}。已有版本和历史引用将保留。
          </p>
          {error && (
            <Notice tone="error" role="alert">
              {error}
            </Notice>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setArchive(null)}
            >
              取消
            </Button>
            <Button
              disabled={busy || !writable}
              onClick={() => void changeArchive()}
            >
              {busy ? "正在保存…" : "确认"}
            </Button>
          </div>
        </Dialog>
      )}
    </PagePanel>
  );
}

function AssetEditor({
  asset,
  path,
  writable,
  onDismiss,
  onSaved,
}: {
  asset: VisualAsset | null;
  path: string;
  writable: boolean;
  onDismiss: () => void;
  onSaved: (row: VisualAsset) => Promise<void>;
}) {
  const [name, setName] = useState(asset?.name ?? "");
  const [origin, setOrigin] = useState("MANUAL");
  const [originNote, setOriginNote] = useState("");
  const [kind, setKind] = useState<VisualAssetType>(
    asset?.asset_type ?? "CHARACTER",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [discard, setDiscard] = useState(false);
  const submitting = useRef(false);
  const dirty =
    name !== (asset?.name ?? "") ||
    kind !== (asset?.asset_type ?? "CHARACTER") ||
    origin !== "MANUAL" ||
    originNote !== "";
  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (dirty || busy) {
        event.preventDefault();
        event.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty, busy]);
  function dismiss() {
    if (busy) return;
    if (dirty) setDiscard(true);
    else onDismiss();
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!writable || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const row = await api<VisualAsset>(
        path + (asset ? `/${asset.id}` : ""),
        json(asset ? "PUT" : "POST", {
          name,
          ...(asset
            ? { expected_revision: asset.revision }
            : {
                asset_type: kind,
                creation_source: { kind: origin, note: originNote },
              }),
        }),
      );
      await onSaved(row);
    } catch (error) {
      setError(message(error));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <Dialog labelledBy="asset-editor-title" busy={busy} onDismiss={dismiss}>
      <h2 id="asset-editor-title">{asset ? "编辑资产名称" : "新建视觉资产"}</h2>
      {discard ? (
        <>
          <p>放弃未保存的资产修改？</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => setDiscard(false)}>
              继续编辑
            </Button>
            <Button onClick={onDismiss}>放弃修改并关闭</Button>
          </div>
        </>
      ) : (
        <form onSubmit={save} className="flex min-w-0 flex-col gap-4">
          {error && (
            <Notice tone="error" role="alert">
              {error}
            </Notice>
          )}
          <fieldset disabled={busy || !writable}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="asset-type">资产类型</FieldLabel>
                <ChoiceSelect
                  id="asset-type"
                  aria-label="资产类型"
                  value={kind}
                  disabled={!!asset || busy || !writable}
                  onValueChange={(value) => setKind(value as VisualAssetType)}
                  options={Object.entries(kinds).map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              </Field>
              {!asset && (
                <>
                  <Field>
                    <FieldLabel htmlFor="asset-origin">创建来源</FieldLabel>
                    <ChoiceSelect
                      id="asset-origin"
                      aria-label="创建来源"
                      value={origin}
                      onValueChange={setOrigin}
                      options={[
                        { value: "MANUAL", label: "人工创建" },
                        { value: "IMPORTED", label: "人工登记导入" },
                      ]}
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="asset-origin-note">
                      来源说明
                    </FieldLabel>
                    <Input
                      id="asset-origin-note"
                      maxLength={2000}
                      value={originNote}
                      onChange={(event) => setOriginNote(event.target.value)}
                    />
                  </Field>
                </>
              )}
              <Field>
                <FieldLabel htmlFor="asset-name">资产名称</FieldLabel>
                <Input
                  id="asset-name"
                  required
                  maxLength={160}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  autoFocus
                />
              </Field>
            </FieldGroup>
          </fieldset>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              disabled={
                busy || !writable || !name.trim() || (!!asset && !dirty)
              }
            >
              {busy ? "正在保存…" : "保存视觉资产"}
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
