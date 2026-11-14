"use client";

import { useCallback } from "react";
import useRenderList from "@/features/rendering/hooks/use-render-list";

const cursorOf = (row: { pageOffset: number }) => row.pageOffset;

/** Adapt offset resources to the existing abortable, scoped page controller. */
export default function useAudioPage<T>(
  scope: string,
  fetchPage: (offset: number | undefined, signal: AbortSignal) => Promise<T[]>,
  keyOf: (row: T) => string,
  unavailable: (cause: unknown) => void,
) {
  const read = useCallback(
    async (offset: number | undefined, signal: AbortSignal) => {
      const rows = await fetchPage(offset, signal);
      return rows.map((row, index) => ({
        ...row,
        id: keyOf(row),
        pageOffset: (offset ?? 0) + index + 1,
      }));
    },
    [fetchPage, keyOf],
  );
  const list = useRenderList(scope, read, cursorOf, unavailable);
  const replace = useCallback(
    (row: T) => list.replace({ ...row, id: keyOf(row), pageOffset: 0 }),
    [list.replace, keyOf],
  );
  return {
    ...list,
    replace,
    prepend: (row: T) =>
      list.prepend({ ...row, id: keyOf(row), pageOffset: 0 }),
  };
}
