"use client";

import { useState, useRef, useEffect, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import Dialog from "@/components/shared/app-dialog";
import { api, json, message } from "@/lib/api/client";
import type { VisualAsset, AssetMetadata, Episode } from "@/types/api";
import usePagedList from "@/hooks/use-paged-list";
import ListPagination from "@/components/shared/list-pagination";

const appearanceFields: [keyof AssetMetadata["appearance"], string, number][] =
  [
    ["age_range", "年龄段", 160],
    ["appearance", "外观", 2000],
    ["hairstyle", "发型", 500],
    ["clothing", "服装", 1000],
    ["build", "体态", 500],
    ["expressions", "常用表情", 500],
    ["colors", "色彩", 500],
    ["materials", "材质", 500],
  ];

export default function AssetMetadataPanel({
  asset,
  path,
  writable,
  onSaved,
}: {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  onSaved: (row: VisualAsset) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const data = asset.metadata;
  return (
    <section className="flex min-w-0 flex-col gap-3" aria-label="资产资料">
      <h3>资产资料</h3>
      <dl className="grid min-w-0 gap-3 break-all text-sm">
        <div>
          <dt>简介</dt>
          <dd className="whitespace-pre-wrap">
            {data.description || "未填写"}
          </dd>
        </div>
        <div>
          <dt>标签</dt>
          <dd>{data.tags.join("、") || "未填写"}</dd>
        </div>
        {appearanceFields.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd className="whitespace-pre-wrap">
              {data.appearance[key] || "未填写"}
            </dd>
          </div>
        ))}
        <div>
          <dt>创建来源</dt>
          <dd>
            {asset.creation_source
              ? asset.creation_source.kind === "MANUAL"
                ? "人工创建"
                : "人工登记导入"
              : "历史来源未记录"}{" "}
            {asset.creation_source?.note}
          </dd>
        </div>
        <div>
          <dt>创建者 ID</dt>
          <dd>{asset.created_by ?? "历史创建者未记录"}</dd>
        </div>
        <div>
          <dt>声明使用范围</dt>
          <dd>
            {data.usage_scope.kind === "PROJECT"
              ? "项目共用"
              : data.usage_scope.episode_ids.join("、")}
          </dd>
        </div>
      </dl>
      <p className="text-sm text-muted-foreground">
        使用范围是人工声明；实际引用查询另行提供。创建来源说明由人工登记，创建者由服务器记录。
      </p>
      {writable && (
        <Button
          disabled={["ARCHIVED", "LOCKED"].includes(asset.status)}
          onClick={() => setEditing(true)}
        >
          编辑资产资料
        </Button>
      )}
      {editing && (
        <MetadataEditor
          asset={asset}
          path={path}
          writable={writable}
          onDismiss={() => setEditing(false)}
          onSaved={async (row) => {
            await onSaved(row);
            setEditing(false);
          }}
        />
      )}
    </section>
  );
}

function MetadataEditor({
  asset,
  path,
  writable,
  onDismiss,
  onSaved,
}: {
  asset: VisualAsset;
  path: string;
  writable: boolean;
  onDismiss: () => void;
  onSaved: (row: VisualAsset) => Promise<void>;
}) {
  const [base] = useState(asset);
  const [data, setData] = useState(base.metadata);
  const [tags, setTags] = useState(data.tags.join(", "));
  const episodes = usePagedList<Episode>(
    "/projects/" + asset.project_id + "/episodes",
  );
  useEffect(() => {
    void episodes.reload().catch(() => {});
  }, [episodes.reload]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [discard, setDiscard] = useState(false);
  const submitting = useRef(false);
  const dirty =
    JSON.stringify(data) !== JSON.stringify(base.metadata) ||
    tags !== base.metadata.tags.join(", ");
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
    if (!busy) {
      if (dirty) setDiscard(true);
      else onDismiss();
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!writable || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      const row = await api<VisualAsset>(
        path + "/" + asset.id + "/metadata",
        json("PUT", {
          expected_revision: base.revision,
          metadata: {
            ...data,
            tags: tags
              .split(/[,，]/)
              .map((value) => value.trim())
              .filter(Boolean),
            usage_scope: {
              ...data.usage_scope,
              episode_ids:
                data.usage_scope.kind === "PROJECT"
                  ? []
                  : data.usage_scope.episode_ids,
            },
          },
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
    <Dialog labelledBy="metadata-title" busy={busy} onDismiss={dismiss}>
      <h2 id="metadata-title">编辑资产资料</h2>
      {discard ? (
        <>
          <p>放弃未保存的资产资料？</p>
          <Button onClick={() => setDiscard(false)}>继续编辑</Button>
          <Button onClick={onDismiss}>放弃修改并关闭</Button>
        </>
      ) : (
        <form className="flex min-w-0 flex-col gap-4" onSubmit={save}>
          {error && (
            <Notice tone="error" role="alert">
              {error}
            </Notice>
          )}
          <fieldset disabled={busy || !writable}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="asset-description">资产简介</FieldLabel>
                <Textarea
                  id="asset-description"
                  maxLength={4000}
                  value={data.description}
                  onChange={(event) =>
                    setData({ ...data, description: event.target.value })
                  }
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="asset-tags">
                  资产标签（逗号分隔）
                </FieldLabel>
                <Input
                  id="asset-tags"
                  value={tags}
                  onChange={(event) => setTags(event.target.value)}
                />
              </Field>
              {appearanceFields.map(([key, label, max]) => (
                <Field key={key}>
                  <FieldLabel htmlFor={"appearance-" + key}>{label}</FieldLabel>
                  <Textarea
                    id={"appearance-" + key}
                    maxLength={max}
                    value={data.appearance[key]}
                    onChange={(event) =>
                      setData({
                        ...data,
                        appearance: {
                          ...data.appearance,
                          [key]: event.target.value,
                        },
                      })
                    }
                  />
                </Field>
              ))}
              <Field>
                <FieldLabel htmlFor="asset-scope">使用范围</FieldLabel>
                <ChoiceSelect
                  id="asset-scope"
                  aria-label="使用范围"
                  value={data.usage_scope.kind}
                  options={[
                    { value: "PROJECT", label: "项目共用" },
                    { value: "EPISODES", label: "指定剧集" },
                  ]}
                  onValueChange={(value) =>
                    setData({
                      ...data,
                      usage_scope: {
                        ...data.usage_scope,
                        kind: value as "PROJECT" | "EPISODES",
                      },
                    })
                  }
                />
              </Field>
              {data.usage_scope.kind === "EPISODES" && (
                <section
                  aria-label="选择使用剧集"
                  className="flex flex-col gap-2"
                >
                  <p>
                    选择本项目剧集（最多100集），已选{" "}
                    {data.usage_scope.episode_ids.length} 集
                  </p>
                  {episodes.rows.map((episode) => (
                    <Field key={episode.id} orientation="horizontal">
                      <Button
                        type="button"
                        variant="outline"
                        role="checkbox"
                        id={"scope-" + episode.id}
                        aria-checked={data.usage_scope.episode_ids.includes(
                          episode.id,
                        )}
                        onClick={() =>
                          setData({
                            ...data,
                            usage_scope: {
                              ...data.usage_scope,
                              episode_ids:
                                !data.usage_scope.episode_ids.includes(
                                  episode.id,
                                )
                                  ? [
                                      ...data.usage_scope.episode_ids,
                                      episode.id,
                                    ]
                                  : data.usage_scope.episode_ids.filter(
                                      (id) => id !== episode.id,
                                    ),
                            },
                          })
                        }
                      >
                        {data.usage_scope.episode_ids.includes(episode.id)
                          ? "已选"
                          : "选择"}
                      </Button>
                      <FieldLabel htmlFor={"scope-" + episode.id}>
                        第{episode.episode_no}集 · {episode.title}
                      </FieldLabel>
                    </Field>
                  ))}
                  <ListPagination
                    label="范围剧集"
                    count={episodes.rows.length}
                    {...episodes}
                  />
                </section>
              )}
            </FieldGroup>
          </fieldset>
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy || !writable || !dirty}>
              {busy ? "正在保存…" : "保存资产资料"}
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
