"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import Dialog from "@/components/shared/app-dialog";
import ListPagination from "@/components/shared/list-pagination";
import { Notice } from "@/components/ui-kit";
import usePagedList from "@/hooks/use-paged-list";
import { useSessionDraft } from "@/providers/draft-provider";
import { api, json, message } from "@/lib/api/client";
import type { VisualAsset } from "@/types/api";
import type {
  ArtOption,
  ArtProposal,
  ArtRecord,
} from "@/features/assets/types/art-proposals";
import ArtDesignEditor, { newArtDesign } from "./art-design-editor";

type Draft = { proposal: ArtProposal; key: string; expected: number };
function option(source: string): ArtOption {
  const design = newArtDesign();
  return {
    option_id: crypto.randomUUID(),
    title: "",
    rationale: "",
    designs: [design],
    source_ids: [source],
    prompts: [
      {
        fragment_id: crypto.randomUUID(),
        kind: "POSITIVE",
        text: "",
        design_ids: [design.design_id],
        source_ids: [source],
      },
    ],
  };
}
function initial(projectId: string, base?: ArtRecord): Draft {
  if (base)
    return {
      proposal: structuredClone(base.version.proposal_json),
      key: crypto.randomUUID(),
      expected: base.revision,
    };
  const source = crypto.randomUUID();
  const first = option(source);
  return {
    key: crypto.randomUUID(),
    expected: 0,
    proposal: {
      project_id: projectId,
      origin: "MANUAL",
      visual_goal: "",
      sources: [{ kind: "MANUAL", source_id: source, statement: "" }],
      options: [first],
      preferred_option_id: first.option_id,
    },
  };
}
function valid(value: unknown): value is Draft {
  if (!value || typeof value !== "object") return false;
  const row = value as Partial<Draft>;
  const proposal = row.proposal;
  return (
    typeof row.key === "string" &&
    typeof row.expected === "number" &&
    !!proposal &&
    typeof proposal.visual_goal === "string" &&
    Array.isArray(proposal.sources) &&
    Array.isArray(proposal.options) &&
    proposal.options.length > 0 &&
    proposal.options.every(
      (option) =>
        typeof option.option_id === "string" &&
        typeof option.title === "string" &&
        Array.isArray(option.designs) &&
        option.designs.every(
          (design) =>
            typeof design.design_id === "string" &&
            typeof design.kind === "string",
        ) &&
        Array.isArray(option.prompts),
    )
  );
}

export default function ArtProposalEditor({
  projectId,
  base,
  onSaved,
  onClose,
}: {
  projectId: string;
  base?: ArtRecord;
  onSaved: (row: ArtRecord) => void;
  onClose: () => void;
}) {
  const [seed] = useState(() => initial(projectId, base));
  const local = useSessionDraft<Draft>({
    id: JSON.stringify([projectId, "art-proposal", base?.id ?? "new"]),
    episodeId: projectId,
    label: base ? `美术提案 v${base.version.version_no} 修订` : "新美术提案",
    initial: seed,
    validate: valid,
  });
  const assets = usePagedList<VisualAsset>(`/projects/${projectId}/assets`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const saving = useRef(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    void assets.reload().catch(() => {});
    return () => {
      live.current = false;
    };
  }, [assets.reload]);
  const draft = local.value;
  const proposal = draft.proposal;
  function update(next: ArtProposal) {
    local.update({ ...draft, proposal: next, key: crypto.randomUUID() });
  }
  function changeOption(index: number, next: ArtOption) {
    update({
      ...proposal,
      options: proposal.options.map((row, i) => (i === index ? next : row)),
    });
  }
  function dismiss() {
    if (
      !busy &&
      (!local.dirty ||
        window.confirm("草稿已保存在本机，关闭后可继续编辑。现在关闭？"))
    )
      onClose();
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    const sent = structuredClone(draft);
    local.update(sent);
    try {
      const path = `/projects/${projectId}/art-proposals${base ? `/${base.id}` : ""}`;
      const row = await api<ArtRecord>(
        path,
        json(base ? "PUT" : "POST", {
          creation_key: sent.key,
          expected_revision: sent.expected,
          proposal: sent.proposal,
        }),
      );
      if (live.current) {
        local.clear();
        onSaved(row);
      }
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      saving.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <Dialog
      labelledBy="art-editor-title"
      busy={busy}
      onDismiss={dismiss}
      className="sm:max-w-3xl"
    >
      <h2 id="art-editor-title" className="text-xl font-semibold">
        {base ? "修订美术提案" : "新建美术提案"}
      </h2>
      <p className="text-sm text-muted-foreground">
        先保存提案并查看变更比较，提交审核后由Owner确认正式生效。
      </p>
      {local.recovered && (
        <Notice tone="info">已恢复本机美术提案草稿，保留原始基准。</Notice>
      )}
      {local.invalid && (
        <Notice tone="error">
          本机草稿格式无效，已保留原记录。请重新填写并核对。
        </Notice>
      )}
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <form onSubmit={save} className="flex min-w-0 flex-col gap-5">
        <fieldset disabled={busy} className="min-w-0">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="art-goal">视觉目标</FieldLabel>
              <Textarea
                id="art-goal"
                required
                maxLength={4000}
                value={proposal.visual_goal}
                onChange={(event) =>
                  update({ ...proposal, visual_goal: event.target.value })
                }
              />
            </Field>
            {proposal.sources
              .filter((source) => source.kind === "MANUAL")
              .map((source) => (
                <Field key={source.source_id}>
                  <FieldLabel htmlFor={`source-${source.source_id}`}>
                    人工来源说明
                  </FieldLabel>
                  <Textarea
                    id={`source-${source.source_id}`}
                    required
                    maxLength={4000}
                    value={source.statement}
                    onChange={(event) =>
                      update({
                        ...proposal,
                        sources: proposal.sources.map((row) =>
                          row.source_id === source.source_id
                            ? { ...source, statement: event.target.value }
                            : row,
                        ),
                      })
                    }
                  />
                </Field>
              ))}
            {proposal.options.map((option, index) => (
              <fieldset
                key={option.option_id}
                className="flex min-w-0 flex-col gap-4 border-t pt-4"
              >
                <legend className="font-semibold">方案 {index + 1}</legend>
                <FieldGroup>
                  <Field>
                    <FieldLabel htmlFor={`title-${option.option_id}`}>
                      方案名称 {index + 1}
                    </FieldLabel>
                    <Input
                      id={`title-${option.option_id}`}
                      required
                      maxLength={80}
                      value={option.title}
                      onChange={(event) =>
                        changeOption(index, {
                          ...option,
                          title: event.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor={`reason-${option.option_id}`}>
                      方案依据 {index + 1}
                    </FieldLabel>
                    <Textarea
                      id={`reason-${option.option_id}`}
                      required
                      maxLength={4000}
                      value={option.rationale}
                      onChange={(event) =>
                        changeOption(index, {
                          ...option,
                          rationale: event.target.value,
                        })
                      }
                    />
                  </Field>
                  {option.designs.map((design, designIndex) => (
                    <ArtDesignEditor
                      key={design.design_id}
                      projectId={projectId}
                      assets={assets.rows}
                      index={`${index + 1}.${designIndex + 1}`}
                      design={design}
                      onChange={(next) =>
                        changeOption(index, {
                          ...option,
                          designs: option.designs.map((row) =>
                            row.design_id === next.design_id ? next : row,
                          ),
                        })
                      }
                    />
                  ))}
                  {option.prompts.map((prompt, promptIndex) => (
                    <Field key={prompt.fragment_id}>
                      <FieldLabel htmlFor={`prompt-${prompt.fragment_id}`}>
                        {prompt.kind === "POSITIVE"
                          ? "正向提示词"
                          : "负向提示词"}{" "}
                        {index + 1}.{promptIndex + 1}
                      </FieldLabel>
                      <Textarea
                        id={`prompt-${prompt.fragment_id}`}
                        required
                        maxLength={8000}
                        value={prompt.text}
                        onChange={(event) =>
                          changeOption(index, {
                            ...option,
                            prompts: option.prompts.map((row) =>
                              row.fragment_id === prompt.fragment_id
                                ? { ...row, text: event.target.value }
                                : row,
                            ),
                          })
                        }
                      />
                    </Field>
                  ))}
                </FieldGroup>
              </fieldset>
            ))}
          </FieldGroup>
        </fieldset>
        <ListPagination
          {...assets}
          count={assets.rows.length}
          label="提案资产选项"
        />
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={busy || proposal.options.length >= 4}
            onClick={() =>
              update({
                ...proposal,
                options: [
                  ...proposal.options,
                  option(proposal.sources[0].source_id),
                ],
              })
            }
          >
            添加备选方案
          </Button>
          {base && (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={async () => {
                setError("");
                try {
                  const current = await api<ArtRecord>(
                    `/projects/${projectId}/art-proposals/${base.id}`,
                  );
                  local.update({
                    ...draft,
                    expected: current.revision,
                    key: crypto.randomUUID(),
                  });
                } catch (error) {
                  setError(message(error));
                }
              }}
            >
              更新提案基准并保留草稿
            </Button>
          )}
          <Button type="submit" disabled={busy}>
            {busy ? "正在保存…" : "保存美术提案"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={dismiss}
          >
            关闭编辑
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              if (window.confirm("放弃此提案的本机草稿？")) {
                local.clear();
                onClose();
              }
            }}
          >
            放弃本机草稿
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
