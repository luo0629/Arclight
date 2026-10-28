import "server-only";

import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { Readable } from "node:stream";

const hopByHop = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

// Preserve Host, Origin and individual Set-Cookie headers. External rewrites
// change Host and would break FastAPI's same-origin authentication checks.
export async function proxyBackend(request: Request): Promise<Response> {
  const backend = new URL(
    process.env.ARCLIGHT_API_PROXY ?? "http://127.0.0.1:8000",
  );
  const incoming = new URL(request.url);
  backend.pathname = incoming.pathname;
  backend.search = incoming.search;
  const headers: Record<string, string> = {};
  const connectionHeaders = new Set(
    (request.headers.get("connection") ?? "")
      .split(",")
      .map((value) => value.trim().toLowerCase()),
  );
  request.headers.forEach((value, name) => {
    if (!hopByHop.has(name) && !connectionHeaders.has(name))
      headers[name] = value;
  });
  headers.host = request.headers.get("host") ?? incoming.host;

  try {
    return await new Promise<Response>((resolve, reject) => {
      const send = backend.protocol === "https:" ? httpsRequest : httpRequest;
      const upstream = send(
        backend,
        {
          method: request.method,
          headers,
          signal: request.signal,
          // Avoid reusing an idle socket closed by the backend's keep-alive
          // timeout, which can otherwise turn a page refresh into a 502.
          agent: false,
        },
        (response) => {
          const responseHeaders = new Headers();
          for (const [name, value] of Object.entries(response.headers)) {
            if (hopByHop.has(name) || value === undefined) continue;
            if (Array.isArray(value)) {
              for (const item of value) responseHeaders.append(name, item);
            } else responseHeaders.set(name, value);
          }
          const status = response.statusCode ?? 502;
          const empty =
            request.method === "HEAD" || [204, 205, 304].includes(status);
          if (empty) response.resume();
          resolve(
            new Response(
              empty
                ? null
                : (Readable.toWeb(response) as ReadableStream<Uint8Array>),
              { status, headers: responseHeaders },
            ),
          );
        },
      );
      upstream.on("error", reject);
      upstream.setTimeout(30_000, () =>
        upstream.destroy(new Error("Backend timeout")),
      );
      if (request.body) {
        const body = Readable.fromWeb(
          request.body as import("node:stream/web").ReadableStream,
        );
        body.on("error", (error) => upstream.destroy(error));
        body.pipe(upstream);
      } else upstream.end();
    });
  } catch {
    return Response.json(
      {
        error: {
          code: "BACKEND_UNAVAILABLE",
          message: "服务暂时不可用，请稍后重试",
        },
      },
      { status: 502 },
    );
  }
}
