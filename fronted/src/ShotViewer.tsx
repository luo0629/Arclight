import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Clapperboard, Film, X } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Notice, StatusBadge } from "@/components/ui-kit";
import ShotDetails, { type Performance } from "./ShotDetails";
import { message, type ContentVersion, type Graph } from "./api";

type Props = {
  graph: Graph;
  shotId: string;
  boards: ContentVersion[];
  ensureBoard: (id: string) => Promise<ContentVersion[]>;
  onSelect: (id: string) => void;
  onClose: () => void;
  onOpenBoard: (id: string) => void;
};
export default function ShotViewer({
  graph,
  shotId,
  boards,
  ensureBoard,
  onSelect,
  onClose,
  onOpenBoard,
}: Props) {
  const node = graph.nodes.find(
    (row) => row.id === shotId && row.kind === "shot",
  );
  const parentId = graph.edges.find(
    (edge) => edge.target === shotId && edge.source.startsWith("storyboard:"),
  )?.source;
  const parent = graph.nodes.find((row) => row.id === parentId);
  const siblings = graph.nodes.filter(
    (row) =>
      row.kind === "shot" &&
      graph.edges.some(
        (edge) => edge.source === parentId && edge.target === row.id,
      ),
  );
  const index = siblings.findIndex((row) => row.id === shotId);
  const version = boards.find((row) => row.id === parent?.resource_id);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setError("");
    if (parent?.resource_id && !version)
      void ensureBoard(parent.resource_id)
        .then((rows) => {
          if (live && !rows.some((row) => row.id === parent.resource_id))
            setError("没有找到所属分镜版本，请刷新内容后重试。");
        })
        .catch((error) => {
          if (live) setError(message(error));
        });
    return () => {
      live = false;
    };
  }, [parent?.resource_id, version, ensureBoard, attempt]);
  const rawShots = version?.content_json.shots;
  const shots = Array.isArray(rawShots)
    ? rawShots.filter(
        (row): row is Record<string, unknown> =>
          !!row && typeof row === "object",
      )
    : [];
  const shotNo = Number(node?.title.match(/\d+/)?.[0]);
  const shot = shots.find((row) => row.shot_no === shotNo);
  const text = (value: unknown) => (typeof value === "string" ? value : "");
  const strings = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string")
      : [];
  const details = {
    characters: strings(shot?.characters),
    composition: text(shot?.composition),
    dialogue_line_ids: strings(shot?.dialogue_line_ids),
    character_details: Array.isArray(shot?.character_details)
      ? shot.character_details
          .filter(
            (row): row is Performance =>
              !!row &&
              typeof row === "object" &&
              typeof row.character === "string",
          )
          .map((row) => ({
            character: row.character,
            action: text(row.action),
            expression: text(row.expression),
            gaze: text(row.gaze),
          }))
      : [],
    notes: text(shot?.notes),
    transition_out: text(shot?.transition_out),
    sound_effects: strings(shot?.sound_effects),
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="shot-viewer"
        showCloseButton={false}
        aria-describedby={undefined}
      >
        <header className="shot-viewer-header">
          <div>
            <p>镜头独立查看 · 分镜 v{parent?.version_no ?? "—"}</p>
            <DialogTitle>{node?.title ?? "镜头不存在"}</DialogTitle>
          </div>
          <div className="shot-viewer-controls">
            <Button
              variant="outline"
              disabled={index <= 0}
              aria-label="上一个镜头"
              onClick={() => onSelect(siblings[index - 1].id)}
            >
              <ArrowLeft />
            </Button>
            <span>
              {index < 0 ? 0 : index + 1} / {siblings.length}
            </span>
            <Button
              variant="outline"
              disabled={index < 0 || index >= siblings.length - 1}
              aria-label="下一个镜头"
              onClick={() => onSelect(siblings[index + 1].id)}
            >
              <ArrowRight />
            </Button>
            <Button variant="ghost" aria-label="关闭镜头查看" onClick={onClose}>
              <X />
            </Button>
          </div>
        </header>
        <ScrollArea className="shot-viewer-scroll">
          <div className="shot-viewer-body">
            <section className="shot-stage" aria-label="镜头画面描述">
              <div className="shot-stage-label">
                <Film />
                {node?.title}
                <span>{(node?.duration_ms ?? 0) / 1000} 秒</span>
              </div>
              <div className="shot-stage-content">
                <Clapperboard aria-hidden="true" />
                <p>{node?.description || "暂无画面描述"}</p>
                <small>文字分镜 · 图片和视频生成尚未开放</small>
              </div>
            </section>
            <aside className="shot-viewer-info" aria-label="镜头信息">
              <h3>镜头信息</h3>
              {node && <StatusBadge status={node.status} />}
              <dl>
                <div>
                  <dt>所属分镜</dt>
                  <dd>v{parent?.version_no ?? "—"}</dd>
                </div>
                <div>
                  <dt>时长</dt>
                  <dd>{(node?.duration_ms ?? 0) / 1000} 秒</dd>
                </div>
                {shot && (
                  <>
                    <div>
                      <dt>场景</dt>
                      <dd>{String(shot.scene_no ?? "—")}</dd>
                    </div>
                    <div>
                      <dt>景别</dt>
                      <dd>{text(shot.shot_size) || "—"}</dd>
                    </div>
                    <div>
                      <dt>运镜</dt>
                      <dd>{text(shot.camera_movement) || "—"}</dd>
                    </div>
                    <div>
                      <dt>角色</dt>
                      <dd>{details.characters.join("、") || "—"}</dd>
                    </div>
                  </>
                )}
              </dl>
              {error ? (
                <Notice tone="error" role="alert">
                  {error}
                  <Button
                    variant="outline"
                    onClick={() => setAttempt((value) => value + 1)}
                  >
                    重试读取分镜
                  </Button>
                </Notice>
              ) : !version && parent ? (
                <div role="status" className="flex flex-col gap-3">
                  <span>正在读取所属分镜…</span>
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-2/3" />
                </div>
              ) : null}
              {shot && (
                <>
                  <h4>字幕</h4>
                  <p className="shot-subtitle">
                    {text(shot.subtitle) || "暂无字幕"}
                  </p>
                  <ShotDetails
                    shot={details}
                    editing={false}
                    update={() => {}}
                  />
                </>
              )}
              {parentId && (
                <Button variant="outline" onClick={() => onOpenBoard(parentId)}>
                  <Clapperboard />
                  打开所属分镜
                </Button>
              )}
              <p className="shot-viewer-hint">
                镜头来自精确分镜版本。修改内容请打开所属分镜并另存新版本。
              </p>
            </aside>
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
