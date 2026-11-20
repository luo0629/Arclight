"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, message } from "@/lib/api/client";

/** A late read may never replace data from another project, episode or page. */
export default function useScopedRead<T>(path: string | null) {
  const [epoch, setEpoch] = useState(0);
  const [state, setState] = useState<{
    path: string | null;
    data: T | null;
    error: string;
    loading: boolean;
  }>({ path: "", data: null, error: "", loading: true });
  const current = useRef(path);
  current.current = path;
  useEffect(() => {
    if (!path) return;
    const request = new AbortController();
    const live = () => !request.signal.aborted && current.current === path;
    setState((old) => ({
      path,
      data: old.path === path ? old.data : null,
      error: "",
      loading: true,
    }));
    void api<T>(path, { signal: request.signal, cache: "no-store" })
      .then((data) => {
        if (live()) setState({ path, data, error: "", loading: false });
      })
      .catch((cause: unknown) => {
        if (live())
          setState({ path, data: null, error: message(cause), loading: false });
      });
    return () => request.abort();
  }, [path, epoch]);

  return {
    data: state.path === path ? state.data : null,
    error: state.path === path ? state.error : "",
    loading: !!path && (state.path !== path || state.loading),
    refresh: useCallback(() => setEpoch((value) => value + 1), []),
  };
}
