"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { VideoCandidateSummary } from "../types";
import VideoCandidateDetail from "./video-candidate-detail";
import VideoGenerationForm from "./video-generation-form";

export default function ShotVideoCandidates({
  projectId,
  episodeId,
  storyboardId,
  shotId,
  shotNo,
  description,
  writable,
  candidates,
  truncated,
  onChanged,
  unavailable,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shotId: string;
  shotNo: number;
  description: string;
  writable: boolean;
  candidates: VideoCandidateSummary[];
  truncated: boolean;
  onChanged: () => Promise<void>;
  unavailable: (cause: unknown) => void;
}) {
  const [selected, setSelected] = useState("");
  return (
    <section
      aria-label={"镜头 " + shotNo + " 视频候选"}
      className="mt-4 flex min-w-0 flex-col gap-3 border-t pt-4"
    >
      <h4 className="font-semibold">视频候选</h4>
      <VideoGenerationForm
        projectId={projectId}
        episodeId={episodeId}
        storyboardId={storyboardId}
        shotId={shotId}
        description={description}
        writable={writable}
        onChanged={onChanged}
        unavailable={unavailable}
      />
      {!candidates.length && (
        <p className="text-xs text-muted-foreground">
          此精确分镜镜头尚无已登记视频候选。
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {candidates.map((row) => (
          <Button
            key={row.id}
            variant={selected === row.id ? "default" : "outline"}
            onClick={() => setSelected(row.id)}
          >
            视频 v{row.version_no} · {row.status}
          </Button>
        ))}
      </div>
      {truncated && (
        <p className="text-xs text-muted-foreground">
          此处显示最近候选，完整历史可从内容与媒体列表查看。
        </p>
      )}
      {selected && (
        <VideoCandidateDetail
          key={projectId + ":" + selected}
          projectId={projectId}
          candidateId={selected}
          expectedStoryboardId={storyboardId}
          expectedShotId={shotId}
          unavailable={unavailable}
          onChanged={onChanged}
          onCreated={async (id) => {
            await onChanged();
            setSelected(id);
          }}
        />
      )}
    </section>
  );
}
