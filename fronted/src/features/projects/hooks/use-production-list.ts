"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import { PRODUCTION_PAGE_SIZE, type ProductionPage } from "../production-types";

type Resource = "tasks" | "pending-reviews" | "deliveries";
type Row = { id: string; project_id?: string };

/** Each response belongs to one project, filter, page and request generation. */
export default function useProductionList<T extends Row>({
  projectId,
  resource,
  query = "",
}: {
  projectId: string;
  resource: Resource;
  query?: string;
}) {
  const scope = `${projectId}:${resource}:${query}`;
  const [page, setPage] = useState({ scope, offset: 0 });
  const [version, setVersion] = useState(0);
  const offset = page.scope === scope ? page.offset : 0;
  const key = `${scope}:${offset}:${version}`;
  const current = useRef(key);
  current.current = key;
  const epoch = useRef(0);
  const [result, setResult] = useState({
    key: "",
    items: [] as T[],
    total: 0,
    loading: true,
    error: "",
  });

  useEffect(() => {
    const abort = new AbortController();
    const generation = ++epoch.current;
    const valid = () =>
      !abort.signal.aborted &&
      current.current === key &&
      epoch.current === generation;
    setResult({ key, items: [], total: 0, loading: true, error: "" });
    const params = new URLSearchParams(query);
    params.set("limit", String(PRODUCTION_PAGE_SIZE));
    params.set("offset", String(offset));
    void api<ProductionPage<T>>(
      `/projects/${encodeURIComponent(projectId)}/${resource}?${params}`,
      { signal: abort.signal },
    )
      .then((value) => {
        if (!valid()) return;
        if (
          !Array.isArray(value.items) ||
          value.items.length > PRODUCTION_PAGE_SIZE ||
          !Number.isSafeInteger(value.total) ||
          value.total < 0 ||
          value.limit !== PRODUCTION_PAGE_SIZE ||
          value.offset !== offset ||
          value.items.some(
            (row) =>
              !isResourceId(row.id) ||
              (row.project_id !== undefined && row.project_id !== projectId),
          )
        )
          throw new Error("INVALID_PAGE");
        // A shrinking result set may remove the last page while it is open.
        if (offset > 0 && value.items.length === 0 && value.total <= offset) {
          setPage({
            scope,
            offset:
              Math.max(0, Math.ceil(value.total / PRODUCTION_PAGE_SIZE) - 1) *
              PRODUCTION_PAGE_SIZE,
          });
          return;
        }
        setResult({
          key,
          items: value.items,
          total: value.total,
          loading: false,
          error: "",
        });
      })
      .catch((error: unknown) => {
        if (!valid()) return;
        const denied =
          error instanceof ApiError && [401, 403, 404].includes(error.status);
        setResult({
          key,
          items: [],
          total: 0,
          loading: false,
          error: denied
            ? "当前项目不可访问，请检查权限或重新选择项目。"
            : "生产列表暂时无法读取，请刷新重试。",
        });
      });
    return () => abort.abort();
  }, [key, projectId, resource, query, offset, scope, version]);

  const setOffset = useCallback(
    (value: number) => {
      if (!Number.isSafeInteger(value) || value < 0) return;
      setPage({
        scope,
        offset: Math.floor(value / PRODUCTION_PAGE_SIZE) * PRODUCTION_PAGE_SIZE,
      });
    },
    [scope],
  );
  const refresh = useCallback(() => setVersion((value) => value + 1), []);
  const visible = result.key === key;
  return {
    items: visible ? result.items : [],
    total: visible ? result.total : 0,
    loading: !visible || result.loading,
    error: visible ? result.error : "",
    offset,
    limit: PRODUCTION_PAGE_SIZE,
    setOffset,
    refresh,
  };
}
