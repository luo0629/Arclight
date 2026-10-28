"use client";

import { useSearchParams } from "next/navigation";
import { episodeViews, type EpisodeView } from "@/lib/routes";

export default function useEpisodeNavigation() {
  const search = useSearchParams();
  const requestedView = search.get("view");
  const view: EpisodeView = episodeViews.includes(requestedView as EpisodeView)
    ? (requestedView as EpisodeView)
    : "canvas";
  const selectedId = search.get("node") ?? "";

  function navigate(
    patch: { view?: EpisodeView; node?: string },
    replace = false,
  ) {
    // Read the live URL so changes made in the same event are merged.
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(patch)) {
      if (!value || (key === "view" && value === "canvas"))
        url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    const href = url.pathname + url.search + url.hash;
    if (
      href ===
      window.location.pathname + window.location.search + window.location.hash
    )
      return;
    // Next.js integrates native history with useSearchParams. Local views do
    // not need a server round trip or a remount of the editor and its drafts.
    window.history[replace ? "replaceState" : "pushState"](null, "", href);
  }

  return {
    view,
    selectedId,
    navigate,
    setView: (value: EpisodeView) => navigate({ view: value }),
    setSelectedId: (value: string) => navigate({ node: value }),
  };
}
