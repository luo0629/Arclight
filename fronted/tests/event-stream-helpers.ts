import { expect, type Page } from "@playwright/test";

export async function pauseClock(page: Page) {
  // Freeze wall time before pausing so the target cannot fall behind the browser.
  // Restore advancing dates without firing polling or reconnect timers.
  const pausedAt = await page.evaluate(() => Date.now());
  await page.clock.setFixedTime(pausedAt);
  await page.clock.pauseAt(pausedAt);
  await page.clock.setSystemTime(pausedAt);
}

export async function controlledStreams(page: Page, statuses: number[] = []) {
  // Only transport is controlled. Real auth, project navigation, query APIs and
  // the production React application remain in use. Never record Bearer values.
  await page.addInitScript(
    ({ statuses }) => {
      type Entry = {
        project: string;
        cursor: string;
        authorized: boolean;
        aborted: boolean;
        cancelled: boolean;
      };
      const requests: Entry[] = [];
      const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
      const original = window.fetch;
      const state = { requests, controllers, statuses };
      Object.assign(window, { __ui06Streams: state });
      window.fetch = async (input, init) => {
        const url = new URL(
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url,
          location.href,
        );
        const match =
          /^\/api\/v1\/projects\/([0-9a-f-]{36})\/events\/stream$/.exec(
            url.pathname,
          );
        if (!match) return original(input, init);
        const headers = new Headers(init?.headers);
        const index = requests.length;
        const entry = {
          project: match[1],
          cursor: headers.get("Last-Event-ID") ?? "",
          authorized: headers.has("Authorization"),
          aborted: false,
          cancelled: false,
        };
        requests.push(entry);
        const status = state.statuses[index] ?? 200;
        if (status !== 200)
          return Response.json(
            { error: { code: "UNAUTHORIZED", message: "会话需要刷新" } },
            { status },
          );
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controllers[index] = controller;
              const stop = () => {
                entry.aborted = true;
                try {
                  controller.error(new DOMException("Aborted", "AbortError"));
                } catch {}
              };
              init?.signal?.addEventListener("abort", stop, { once: true });
              if (init?.signal?.aborted) stop();
            },
            cancel() {
              entry.cancelled = true;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      };
    },
    { statuses },
  );
  const snapshot = () =>
    page.evaluate(() => {
      const state = (
        window as unknown as { __ui06Streams: { requests: unknown[] } }
      ).__ui06Streams;
      return state.requests;
    });
  async function push(index: number, chunks: string[]) {
    await page.evaluate(
      ({ index, chunks }) => {
        const state = (
          window as unknown as {
            __ui06Streams: {
              controllers: ReadableStreamDefaultController<Uint8Array>[];
            };
          }
        ).__ui06Streams;
        chunks.forEach((chunk) =>
          state.controllers[index].enqueue(new TextEncoder().encode(chunk)),
        );
      },
      { index, chunks },
    );
  }
  async function end(index: number, fail = false) {
    await page.evaluate(
      ({ index, fail }) => {
        const state = (
          window as unknown as {
            __ui06Streams: {
              controllers: ReadableStreamDefaultController<Uint8Array>[];
            };
          }
        ).__ui06Streams;
        if (fail)
          state.controllers[index].error(new TypeError("Network interrupted"));
        else state.controllers[index].close();
      },
      { index, fail },
    );
  }
  async function count(value: number) {
    await expect.poll(async () => (await snapshot()).length).toBe(value);
  }
  return { snapshot, push, end, count };
}

export function frame(
  project: string,
  event = "stream.ready",
  id = "",
  sourceId?: string,
) {
  const data = sourceId
    ? {
        project_id: project,
        source: "w",
        source_id: sourceId,
        created_at: "2026-11-15T00:00:00Z",
      }
    : { project_id: project };
  return `event: ${event}\nid: ${id}\ndata: ${JSON.stringify(data)}\n\n`;
}
