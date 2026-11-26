import "client-only";

import { isResourceId } from "@/lib/routes";
import type { FormalExportRequest, HumanReview, RenderManifest } from "./types";

export type FormalExportIntent =
  | { timelineId: string; request: FormalExportRequest }
  | { timelineId: string; manifestId: string };

const intents = new Map<string, FormalExportIntent[]>();
const storageKey = (scope: string) => `arclight:formal-export:${scope}`;

function readIntents(scope: string): FormalExportIntent[] {
  const current = intents.get(scope);
  if (current) return current;
  try {
    const raw = sessionStorage.getItem(storageKey(scope));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const saved = (parsed as FormalExportIntent[]).filter(
      (intent) =>
        intent &&
        isResourceId(intent.timelineId) &&
        ("manifestId" in intent
          ? isResourceId(intent.manifestId)
          : isResourceId(intent.request?.creation_key)),
    );
    intents.set(scope, saved);
    return saved;
  } catch {
    return [];
  }
}

export function readFormalExportIntent(scope: string, timelineId?: string) {
  const saved = readIntents(scope);
  return timelineId
    ? saved.find((intent) => intent.timelineId === timelineId)
    : saved[0];
}

export function writeFormalExportIntent(
  scope: string,
  timelineId: string,
  intent?: FormalExportIntent,
) {
  const previous = readIntents(scope).filter(
    (entry) => entry.timelineId !== timelineId,
  );
  const next = intent ? [intent, ...previous] : previous;
  intents.set(scope, next);
  try {
    if (next.length)
      sessionStorage.setItem(storageKey(scope), JSON.stringify(next));
    else sessionStorage.removeItem(storageKey(scope));
  } catch {
    // A browser that blocks web storage still retains the intent for this visit.
  }
}

export function matchesExportReview(
  review: HumanReview,
  manifest: RenderManifest,
) {
  return (
    review.project_id === manifest.project_id &&
    review.snapshot.project_id === manifest.project_id &&
    review.snapshot.episode_id === manifest.snapshot.timeline.episode_id &&
    review.snapshot.kind === "EXPORT" &&
    review.snapshot.object_id === manifest.id &&
    review.snapshot.version_id === manifest.id &&
    review.snapshot.content_sha256 === manifest.content_sha256
  );
}
