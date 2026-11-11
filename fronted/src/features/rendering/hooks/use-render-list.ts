"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { message } from "@/lib/api/client";
import { PAGE_SIZE } from "../api";

/** A bounded server page; every scope change invalidates reads and old cursors. */
export default function useRenderList<T extends { id: string }, C>(
  scope: string,
  fetchPage: (cursor: C | undefined, signal: AbortSignal) => Promise<T[]>,
  cursorOf: (row: T) => C,
  onUnavailable: (cause: unknown) => void,
) {
  const [rows, setRows] = useState<T[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const currentRows = useRef<T[]>([]);
  const cursor = useRef<C | undefined>(undefined);
  const read = useRef<AbortController | null>(null);
  const live = useRef(false);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const load = useCallback(
    async (append = false) => {
      if (!scope || (append && read.current)) return;
      read.current?.abort();
      const controller = new AbortController();
      read.current = controller;
      setLoading(true);
      setError("");
      try {
        const next = await fetchPage(
          append ? cursor.current : undefined,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          !live.current ||
          currentScope.current !== scope
        )
          return;
        const merged = append ? [...currentRows.current, ...next] : next;
        const unique = merged.filter(
          (row, index) =>
            merged.findIndex((other) => other.id === row.id) === index,
        );
        currentRows.current = unique;
        setRows(unique);
        cursor.current = next.length
          ? cursorOf(next[next.length - 1])
          : cursor.current;
        setHasMore(next.length === PAGE_SIZE);
      } catch (cause) {
        if (
          controller.signal.aborted ||
          !live.current ||
          currentScope.current !== scope
        )
          return;
        currentRows.current = [];
        cursor.current = undefined;
        setRows([]);
        setHasMore(false);
        setError(message(cause));
        onUnavailable(cause);
      } finally {
        if (read.current === controller) {
          read.current = null;
          if (live.current) setLoading(false);
        }
      }
    },
    [scope, fetchPage, cursorOf, onUnavailable],
  );
  useEffect(() => {
    live.current = true;
    currentRows.current = [];
    cursor.current = undefined;
    setRows([]);
    setHasMore(false);
    setError("");
    void load();
    return () => {
      live.current = false;
      read.current?.abort();
      read.current = null;
    };
  }, [load]);
  const prepend = useCallback((row: T) => {
    read.current?.abort();
    read.current = null;
    setLoading(false);
    setError("");
    const next = [
      row,
      ...currentRows.current.filter((previous) => previous.id !== row.id),
    ];
    currentRows.current = next;
    setRows(next);
  }, []);
  const clear = useCallback(() => {
    read.current?.abort();
    read.current = null;
    setLoading(false);
    currentRows.current = [];
    cursor.current = undefined;
    setRows([]);
    setHasMore(false);
  }, []);
  const replace = useCallback((row: T) => {
    read.current?.abort();
    read.current = null;
    setLoading(false);
    setError("");
    const next = currentRows.current.map((previous) =>
      previous.id === row.id ? row : previous,
    );
    currentRows.current = next;
    setRows(next);
  }, []);
  return { rows, loading, error, hasMore, load, prepend, replace, clear };
}
