import "client-only";
import type { Session } from "@/types/api";

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
async function request(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<Response> {
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
      return response;
    }
    return request(path, init, false);
  }
  if (response.status === 401 && !retry) {
    token = "";
    unauthorized();
  }
  return response;
}
export async function api<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<T> {
  return parse<T>(await request(path, init, retry));
}
export async function apiBlob(
  path: string,
  signal: AbortSignal,
): Promise<Blob> {
  const response = await request(path, { signal });
  if (!response.ok) return parse<never>(response);
  return response.blob();
}
export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export function message(error: unknown) {
  if (error instanceof ApiError)
    return `${error.message}${error.requestId ? ` · 请求 ${error.requestId}` : ""}`;
  return error instanceof TypeError
    ? "网络连接失败，请检查网络后重试"
    : error instanceof Error
      ? error.message
      : "连接失败，请检查网络后重试";
}
