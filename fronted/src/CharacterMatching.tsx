import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import { api, json, message, type StoryEntry } from "./api";
import Dialog from "./Dialog";

type Entry = Pick<
  StoryEntry,
  "id" | "name" | "aliases" | "revision" | "archived"
>;
type Confirmation = {
  id: string;
  created_by: string;
  created_at: string;
  entry: Entry;
  decision: "CREATE" | "REUSE";
};
type Candidate = {
  name: string;
  aliases: string[];
  matches: string[];
  confirmation: Confirmation | null;
};
type Preview = {
  fingerprint: string;
  executing: boolean;
  entries: Entry[];
  candidates: Candidate[];
};
type Decision = {
  source_name: string;
  expected_fingerprint: string;
  decision: "CREATE" | "REUSE";
  entry_id: string | null;
};
type Props = {
  episodeId: string;
  sourceType: "scripts" | "story-analyses";
  versionId: string;
  writable: boolean;
  busy?: boolean;
};

export default function CharacterMatching(props: Props) {
  const path = `/episodes/${props.episodeId}/${props.sourceType}/${props.versionId}/character-matches`;
  return (
    <MatchingForm
      key={path}
      path={path}
      writable={props.writable}
      busy={props.busy ?? false}
    />
  );
}

function MatchingForm({
  path,
  writable,
  busy,
}: {
  path: string;
  writable: boolean;
  busy: boolean;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Preview | null>(null);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<Decision | null>(null);
  const live = useRef(false);
  const submitting = useRef(false);
  const sequence = useRef(0);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      sequence.current++;
    };
  }, []);
  async function load() {
    const ticket = ++sequence.current;
    setLoading(true);
    setError("");
    try {
      const result = await api<Preview>(path);
      if (live.current && ticket === sequence.current) setData(result);
    } catch (error) {
      if (live.current && ticket === sequence.current) setError(message(error));
    } finally {
      if (live.current && ticket === sequence.current) setLoading(false);
    }
  }
  async function confirm(body: Decision) {
    if (submitting.current || !writable || busy) return;
    submitting.current = true;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await api<Confirmation>(path, json("POST", body));
      if (!live.current) return;
      setData(
        (old) =>
          old && {
            ...old,
            candidates: old.candidates.map((candidate) =>
              candidate.name === body.source_name
                ? { ...candidate, confirmation: result }
                : candidate,
            ),
          },
      );
      setPending(null);
      setNotice("角色已确认；内容审核仍需单独完成。");
      await load();
    } catch (error) {
      if (live.current) setError(message(error));
    } finally {
      submitting.current = false;
      if (live.current) setSaving(false);
    }
  }
  const disabled = saving || loading || busy || !writable || !!data?.executing;
  return (
    <details
      className="studio-disclosure"
      open={open}
      onToggle={(event) => {
        const expanded = event.currentTarget.open;
        setOpen(expanded);
        if (expanded && !data && !loading) void load();
      }}
    >
      <summary className="cursor-pointer">角色匹配与确认</summary>
      {open && (
        <div className="flex min-w-0 flex-col gap-4 py-3">
          <p className="text-sm text-muted-foreground">
            先匹配故事设定中的名称和别名，再确认复用或新增。确认记录仅属于此版本；新增的是故事角色。剧本与视觉资产分别审核。
          </p>
          <Button
            variant="outline"
            disabled={saving || loading || !!pending}
            onClick={() => void load()}
          >
            {loading ? "正在匹配…" : "刷新角色匹配"}
          </Button>
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
          {data?.executing && (
            <Notice tone="info">
              项目中有生成任务，请等待结束后刷新匹配。
            </Notice>
          )}
          {!writable && (
            <Notice tone="info">当前项目只读，可查看匹配和确认记录。</Notice>
          )}
          {data?.candidates.length === 0 && <p>此版本没有需要确认的角色。</p>}
          {data?.candidates.map((candidate, index) => {
            const matched = data.entries.filter((entry) =>
              candidate.matches.includes(entry.id),
            );
            const choice =
              choices[candidate.name] ??
              (matched.length === 1 && !matched[0].archived
                ? matched[0].id
                : "");
            const confirmation = candidate.confirmation;
            const current =
              confirmation &&
              data.entries.find((entry) => entry.id === confirmation.entry.id);
            return (
              <section
                key={candidate.name}
                aria-label={`角色确认 ${candidate.name}`}
                className="flex min-w-0 flex-col gap-3 border-t pt-3"
              >
                <strong className="break-words">{candidate.name}</strong>
                {candidate.aliases.length > 0 && (
                  <p className="break-words">
                    来源别名：{candidate.aliases.join("、")}
                  </p>
                )}
                {confirmation ? (
                  <>
                    <p>
                      已确认
                      {confirmation.decision === "CREATE" ? "新增" : "复用"}：
                      {confirmation.entry.name} · 修订{" "}
                      {confirmation.entry.revision}
                    </p>
                    <p className="break-all text-xs text-muted-foreground">
                      角色 ID：{confirmation.entry.id} ·{" "}
                      {new Date(confirmation.created_at).toLocaleString(
                        "zh-CN",
                      )}
                    </p>
                    {current?.archived && (
                      <Notice tone="info">
                        当前角色已归档；确认时的记录仍保留。
                      </Notice>
                    )}
                    {current &&
                      current.revision !== confirmation.entry.revision && (
                        <p>当前设定已有更新，以上为确认时的快照。</p>
                      )}
                  </>
                ) : (
                  <>
                    <p>
                      {matched.length > 1
                        ? "多个角色匹配，请核对后选择。"
                        : matched.length === 1
                          ? `名称或别名匹配：${matched[0].name}${matched[0].archived ? "（已归档，请先到故事设定恢复）" : ""}`
                          : "未匹配到角色，请选择已有角色，或确认新增。"}
                    </p>
                    <FieldGroup>
                      <Field>
                        <FieldLabel htmlFor={`${id}-${index}`}>
                          处理 {candidate.name}
                        </FieldLabel>
                        <ChoiceSelect
                          id={`${id}-${index}`}
                          value={choice}
                          disabled={disabled}
                          placeholder="请选择角色处理方式"
                          options={[
                            ...data.entries.map((entry) => ({
                              value: entry.id,
                              label: `${entry.name}${entry.archived ? "（已归档）" : ""} · ${entry.id.slice(0, 8)}`,
                              disabled: entry.archived,
                            })),
                            ...(matched.length
                              ? []
                              : [
                                  {
                                    value: "new",
                                    label: `新增故事角色：${candidate.name}`,
                                  },
                                ]),
                          ]}
                          onValueChange={(value) =>
                            setChoices((old) => ({
                              ...old,
                              [candidate.name]: value,
                            }))
                          }
                        />
                      </Field>
                    </FieldGroup>
                    <Button
                      variant="outline"
                      disabled={
                        disabled ||
                        !choice ||
                        (choice === "new"
                          ? !!matched.length
                          : !data.entries.some(
                              (entry) => entry.id === choice && !entry.archived,
                            ))
                      }
                      onClick={() => {
                        const body: Decision = {
                          source_name: candidate.name,
                          expected_fingerprint: data.fingerprint,
                          decision: choice === "new" ? "CREATE" : "REUSE",
                          entry_id: choice === "new" ? null : choice,
                        };
                        if (choice === "new") setPending(body);
                        else void confirm(body);
                      }}
                    >
                      确认角色 {candidate.name}
                    </Button>
                  </>
                )}
              </section>
            );
          })}
        </div>
      )}
      {pending && (
        <Dialog
          labelledBy={`${id}-title`}
          busy={saving}
          onDismiss={() => setPending(null)}
        >
          <h2 id={`${id}-title`} className="text-lg font-semibold">
            确认新增故事角色
          </h2>
          <p className="break-words">名称：{pending.source_name}</p>
          <p className="break-words">
            别名：
            {data?.candidates
              .find((candidate) => candidate.name === pending.source_name)
              ?.aliases.join("、") || "无"}
          </p>
          <p>
            请核对是否是已有角色的另一种称呼。确认后会创建故事设定并保留来源记录。
          </p>
          {error && (
            <Notice tone="error" role="alert">
              {error}
            </Notice>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={saving}
              onClick={() => setPending(null)}
            >
              返回核对
            </Button>
            <Button
              disabled={saving || !writable || busy}
              onClick={() => void confirm(pending)}
            >
              {saving ? "正在确认…" : "确认新增角色"}
            </Button>
          </div>
        </Dialog>
      )}
    </details>
  );
}
