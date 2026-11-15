import { expect, test } from "@playwright/test";
import { readEventStream, type StreamEvent } from "../src/lib/api/event-stream";

async function decode(chunks: Uint8Array[], abort = new AbortController()) {
  const events: StreamEvent[] = [];
  const response = new Response(
    new ReadableStream({
      start(controller) {
        chunks.forEach((chunk) => controller.enqueue(chunk));
        controller.close();
      },
    }),
  );
  await readEventStream(response, abort.signal, (event) => events.push(event));
  return events;
}

for (const newline of ["\n", "\r\n", "\r"]) {
  test(`UI-06：SSE 分隔符 ${JSON.stringify(newline)}、UTF-8 字节分块与多行内容`, async () => {
    const wire = [
      ": 心跳",
      "event: project.changed",
      "id: stable-id",
      "retry: 1500",
      "data: 第一行",
      "data: 第二行",
      "",
      "",
    ].join(newline);
    const bytes = new TextEncoder().encode(wire);
    const events = await decode(
      [...bytes].map((value) => new Uint8Array([value])),
    );
    expect(events).toEqual([
      {
        event: "project.changed",
        id: "stable-id",
        retry: 1500,
        data: "第一行\n第二行",
      },
    ]);
  });
}

test("UI-06：注释不生成事件，未知字段及无效 retry/id 不污染下一帧", async () => {
  const wire =
    ": keep-alive\n\nunknown: value\nid: bad\0id\nretry: 1.5\ndata: {}\n\n" +
    "event: \nid:\ndata: ok\n\n";
  const events = await decode([new TextEncoder().encode(wire)]);
  expect(events).toEqual([
    { event: "message", data: "{}", id: undefined, retry: undefined },
    { event: "message", data: "ok", id: "", retry: undefined },
  ]);
});

test("UI-06：EOF 不把未完成事件认作收到的通知", async () => {
  expect(
    await decode([
      new TextEncoder().encode(
        "id: received\ndata: first\n\nid: lost\ndata: partial",
      ),
    ]),
  ).toEqual([
    { event: "message", data: "first", id: "received", retry: undefined },
  ]);
});

test("UI-06：中止释放读取器且迟到字节不能调用订阅者", async () => {
  const abort = new AbortController();
  let cancelled = false;
  const events: StreamEvent[] = [];
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("data: first\n\n"));
      },
      cancel() {
        cancelled = true;
      },
    }),
  );
  await readEventStream(response, abort.signal, (event) => {
    events.push(event);
    abort.abort();
  });
  expect(events).toHaveLength(1);
  expect(cancelled).toBe(true);
  expect(response.body?.locked).toBe(false);
});

for (const wire of ["x".repeat(65537), "data: " + "x".repeat(65537) + "\n\n"]) {
  test(`UI-06：超限${wire.startsWith("data:") ? "事件" : "单行"}关闭连接`, async () => {
    await expect(decode([new TextEncoder().encode(wire)])).rejects.toThrow(
      "超出限制",
    );
  });
}
