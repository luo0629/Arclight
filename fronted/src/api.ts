export type User = { id: string; email: string; display_name: string };
export type Session = { access_token: string; user: User };
export type Project = {
  id: string;
  name: string;
  owner_id: string;
  archived: boolean;
  budget_limit: string;
  style_guide: Record<string, unknown>;
  output_profile: Record<string, unknown>;
};
export type Episode = {
  id: string;
  project_id: string;
  episode_no: number;
  title: string;
  status: string;
  target_duration_ms: number;
};
export type ContentVersion = {
  id: string;
  episode_id: string;
  version_no: number;
  content_json: Record<string, unknown>;
  status: string;
  run_id: string | null;
  script_version_id?: string;
};
export type Run = {
  id: string;
  status: string;
  revision: number;
  pending_review: {
    resource_type: "script" | "storyboard";
    version_id: string;
  } | null;
  error_code: string | null;
  created_at: string;
};
export type Member = {
  user_id: string;
  email: string;
  display_name: string;
  role: "editor" | "viewer";
};
export type GraphNode = {
  id: string;
  kind: string;
  resource_id?: string;
  title: string;
  status: string;
  version_no?: number;
  duration_ms?: number;
  description?: string;
};
export type Layout = {
  revision: number;
  positions: Record<string, { x: number; y: number }>;
  viewport: { x: number; y: number; zoom: number };
};
export type Graph = {
  nodes: GraphNode[];
  edges: { id: string; source: string; target: string }[];
  layout: Layout;
};

let token = "";
let unauthorized: () => void = () => {};
let pendingRefresh: Promise<Session> | null = null;
export function configureAuth(access: string, onUnauthorized?: () => void) {
  token = access;
  if (onUnauthorized) unauthorized = onUnauthorized;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId = "",
  ) {
    super(message);
  }
}
async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code ?? "NETWORK_ERROR",
      body.error?.message ?? `请求失败（${response.status}）`,
      body.error?.request_id ?? "",
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export async function refreshSession(): Promise<Session> {
  if (!pendingRefresh) {
    pendingRefresh = fetch("/api/v1/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
    })
      .then(parse<Session>)
      .then((session) => {
        token = session.access_token;
        return session;
      })
      .finally(() => {
        pendingRefresh = null;
      });
  }
  return pendingRefresh;
}
export async function api<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (response.status === 401 && retry && !path.startsWith("/auth/")) {
    try {
      await refreshSession();
    } catch {
      token = "";
      unauthorized();
      return parse<T>(response);
    }
    return api<T>(path, init, false);
  }
  if (response.status === 401 && !retry) {
    token = "";
    unauthorized();
  }
  return parse<T>(response);
}
export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export function message(error: unknown) {
  if (error instanceof ApiError)
    return `${error.message}${error.requestId ? ` · 请求 ${error.requestId}` : ""}`;
  return error instanceof Error ? error.message : "连接失败，请检查网络后重试";
}
export const statusText: Record<string, string> = {
  DRAFT: "草稿",
  READY_FOR_REVIEW: "待审核",
  APPROVED: "已批准",
  REJECTED: "已驳回",
  QUEUED: "排队中",
  RUNNING: "创作中",
  WAITING_REVIEW: "等待你审核",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  ARCHIVED: "已归档",
  LOCKED: "已锁定",
  STALE: "需更新",
};
