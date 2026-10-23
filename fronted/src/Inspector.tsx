import { Button } from "@/components/ui/button";
import { Notice, FormLabel, StatusBadge } from "@/components/ui-kit";
import { Field } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { useEffect, useId, useRef, useState } from "react";
import {
  api,
  json,
  type ContentVersion,
  type Episode,
  type GraphNode,
  type Run,
} from "./api";

import VersionEditor, { type Act, type VersionCreated } from "./VersionEditor";
import ManualStoryboard from "./ManualStoryboard";
import VersionComparison from "./VersionComparison";
import { useDraftStore, useSessionDraft } from "./Drafts";

export default function Inspector({
  node,
  episode,
  scripts,
  boards,
  runs,
  owner,
  writable,
  busy,
  act,
  reload,
  showTasks,
  versionLoading,
  retryVersion,
  onCreated,
  focusVersionId,
}: {
  node: GraphNode | null;
  episode: Episode;
  scripts: ContentVersion[];
  boards: ContentVersion[];
  runs: Run[];
  owner: boolean;
  writable: boolean;
  busy: boolean;
  act: Act;
  reload: () => Promise<void>;
  showTasks: () => void;
  versionLoading: boolean;
  retryVersion: () => Promise<unknown>;
  onCreated: VersionCreated;
  focusVersionId?: string;
}) {
  const storyId = useId();
  const storyDraft = useSessionDraft({
    id: JSON.stringify([episode.id, "story"]),
    episodeId: episode.id,
    label: "故事灵感",
    initial: "",
    validate: (value): value is string => typeof value === "string",
  });
  const story = storyDraft.value;
  const { drafts } = useDraftStore();
  const episodeDrafts = Object.values(drafts).filter(
    (draft) => draft.episodeId === episode.id,
  );
  const [creating, setCreating] = useState(false);
  const [creatingBoard, setCreatingBoard] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const directorKey = useRef({ scriptId: "", key: "" });
  const requestKey = useRef({ story: "", key: "" });
  const active = runs.some((run) =>
    ["QUEUED", "RUNNING", "WAITING_REVIEW"].includes(run.status),
  );
  const version =
    node?.kind === "script"
      ? scripts.find((row) => row.id === node.resource_id)
      : boards.find((row) => row.id === node?.resource_id);
  useEffect(() => {
    if (focusVersionId && version?.id === focusVersionId) {
      panel.current?.focus({ preventScroll: true });
      panel.current?.scrollIntoView({ block: "nearest" });
    }
  }, [focusVersionId, version?.id]);
  const created: VersionCreated = async (row, kind) => {
    setCreating(false);
    setCreatingBoard(false);
    await onCreated(row, kind);
  };
  async function generate() {
    if (requestKey.current.story !== story || !requestKey.current.key)
      requestKey.current = { story, key: crypto.randomUUID() };
    await act(async () => {
      await api(`/episodes/${episode.id}/script-runs`, {
        ...json("POST", { story }),
        headers: { "Idempotency-Key": requestKey.current.key },
      });
      storyDraft.clear();
      await reload();
      showTasks();
    });
  }
  async function direct(scriptId: string) {
    if (directorKey.current.scriptId !== scriptId || !directorKey.current.key)
      directorKey.current = { scriptId, key: crypto.randomUUID() };
    const ok = await act(async () => {
      await api(`/episodes/${episode.id}/storyboard-runs`, {
        ...json("POST", { script_version_id: scriptId }),
        headers: { "Idempotency-Key": directorKey.current.key },
      });
      await reload();
      showTasks();
    });
    if (ok) directorKey.current = { scriptId: "", key: "" };
  }
  return (
    <aside
      ref={panel}
      tabIndex={-1}
      className="inspector flex min-w-0 flex-col gap-4 rounded-xl border bg-card p-5 shadow-sm focus-visible:ring-2 focus-visible:ring-ring"
      aria-label="内容与属性"
    >
      <div className="inspector-heading flex items-center justify-between border-b border-dashed pb-3">
        <span className="eyebrow text-[11px] font-semibold tracking-widest text-primary">
          这一页的细节
        </span>
        <span
          className="mini-flower text-xl text-primary/60"
          aria-hidden="true"
        >
          ✿
        </span>
      </div>
      {episodeDrafts.length > 0 && (
        <Notice tone="info" role="status" className="episode-drafts">
          本剧集有未保存草稿：
          {episodeDrafts.map((draft) => draft.label).join("、")}。
          重新打开对应版本或人工起稿入口即可恢复。仅限当前标签页，不会自动提交。
        </Notice>
      )}
      {!node || node.kind === "episode" ? (
        <>
          {writable && (
            <>
              <Button
                variant="outline"
                size="lg"
                disabled={busy || active}
                onClick={() => {
                  setCreating(!creating);
                  setCreatingBoard(false);
                }}
              >
                {creating ? "收起人工起稿" : "自己写剧本"}
              </Button>
              {creating && (
                <>
                  <p className="hint text-xs leading-relaxed text-muted-foreground">
                    这是尚未保存的人工草稿。保存后自动打开待审核的新版本，已有版本不会被覆盖。
                  </p>
                  <VersionEditor
                    kind="script"
                    episode={episode}
                    writable={writable}
                    busy={busy}
                    act={act}
                    latestNo={Math.max(
                      0,
                      ...scripts.map((row) => row.version_no),
                    )}
                    active={active}
                    onCreated={created}
                  />
                </>
              )}
              <Button
                variant="outline"
                size="lg"
                disabled={busy || active}
                onClick={() => {
                  setCreatingBoard(!creatingBoard);
                  setCreating(false);
                }}
              >
                {creatingBoard ? "收起人工分镜" : "自己写分镜"}
              </Button>
              {creatingBoard && (
                <ManualStoryboard
                  episode={episode}
                  writable={writable}
                  busy={busy}
                  active={active}
                  act={act}
                  latestNo={Math.max(0, ...boards.map((row) => row.version_no))}
                  onCreated={created}
                />
              )}
            </>
          )}
          <h2>先写下你的灵感</h2>
          <p className="muted text-sm text-muted-foreground">
            一场相遇、一段对白，或者一个小小的“如果”。编剧会把它展开成可审核的剧本。
          </p>
          <Field className="flex min-w-0 flex-col gap-2">
            <FormLabel htmlFor={storyId}>故事灵感</FormLabel>
            <Textarea
              id={storyId}
              className="story-input min-h-40"
              value={story}
              onChange={(event) => storyDraft.update(event.target.value)}
              maxLength={20000}
              placeholder="雨夜，一位修伞的女孩遇到了一只会说话的小猫……"
              disabled={!writable || busy}
            />
          </Field>
          <p className="counter -mt-2 text-right text-xs text-muted-foreground">
            {story.length} / 20000
          </p>
          <Button
            variant="default"
            size="lg"
            className="wide w-full"
            disabled={!writable || busy || active || !story.trim()}
            onClick={() => void generate()}
          >
            {busy ? "正在提交…" : "请编剧开始创作"}
          </Button>
          {active && (
            <p className="hint text-xs leading-relaxed text-muted-foreground">
              已有进行中的创作任务。
              <Button
                variant="ghost"
                size="lg"
                className=""
                onClick={showTasks}
              >
                前往任务与审核
              </Button>
            </p>
          )}
          <p className="hint text-xs leading-relaxed text-muted-foreground">
            流程：生成剧本 → 你审核 → 生成分镜 →
            你审核。真实生成需要配置模型，任务执行需要启动 Worker。
          </p>
        </>
      ) : node.kind === "shot" ? (
        <>
          <h2>{node.title}</h2>
          <StatusBadge status={node.status} />
          <p>{node.description}</p>
          <p className="muted text-sm text-muted-foreground">
            时长：{(node.duration_ms ?? 0) / 1000} 秒
          </p>
          <p className="hint text-xs leading-relaxed text-muted-foreground">
            修改镜头请选择所属分镜，再另存分镜新版本。图片生成链路尚未开放。
          </p>
        </>
      ) : version ? (
        <>
          <VersionComparison
            key={`compare:${version.id}`}
            episodeId={episode.id}
            kind={node.kind === "script" ? "script" : "storyboard"}
            version={version}
          />
          <VersionEditor
            key={version.id}
            version={version}
            kind={node.kind === "script" ? "script" : "storyboard"}
            onCreated={created}
            episode={episode}
            writable={writable}
            busy={busy}
            act={act}
            latestNo={Math.max(
              0,
              ...(node.kind === "script" ? scripts : boards).map(
                (row) => row.version_no,
              ),
            )}
            active={active}
          />
          {node.kind === "script" &&
            version.status === "APPROVED" &&
            writable && (
              <>
                <Button
                  variant="default"
                  size="lg"
                  className="wide w-full"
                  disabled={busy || active}
                  onClick={() => void direct(version.id)}
                >
                  从此版本生成分镜
                </Button>
                <p className="hint text-xs leading-relaxed text-muted-foreground">
                  导演只使用此精确剧本版本，不重新调用编剧；生成后仍需审核分镜。
                </p>
              </>
            )}
          {version.status === "READY_FOR_REVIEW" &&
            (version.run_id ? (
              <Button
                variant="default"
                size="lg"
                className="wide w-full"
                onClick={showTasks}
              >
                前往任务审核此版本
              </Button>
            ) : (
              owner &&
              writable && (
                <div className="manual-review flex flex-col gap-3 rounded-lg bg-accent p-4">
                  <p>该版本由人工编辑创建，可在这里独立审核。</p>
                  <div className="button-row flex flex-wrap items-center gap-2">
                    {["APPROVE", "REJECT"].map((decision) => (
                      <Button
                        variant="outline"
                        size="lg"
                        key={decision}
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await api(
                              `/episodes/${episode.id}/${node.kind === "script" ? "scripts" : "storyboards"}/${version.id}/review`,
                              json("POST", { decision }),
                            );
                            await reload();
                          })
                        }
                      >
                        {decision === "APPROVE" ? "批准该版本" : "驳回该版本"}
                      </Button>
                    ))}
                  </div>
                </div>
              )
            ))}
        </>
      ) : (
        <div
          className="hint text-xs leading-relaxed text-muted-foreground"
          role="status"
        >
          {versionLoading
            ? "正在读取此历史版本…"
            : "此版本尚未加载，请重试或继续加载版本列表。"}
          <Button
            variant="outline"
            size="lg"
            disabled={versionLoading}
            onClick={() => void retryVersion().catch(() => {})}
          >
            重试加载此版本
          </Button>
        </div>
      )}
    </aside>
  );
}
