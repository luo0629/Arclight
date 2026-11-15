export type StreamEvent = {
  event: string;
  data: string;
  id?: string;
  retry?: number;
};

// Fetch keeps the existing Bearer/session-refresh contract; native EventSource
// cannot attach our Authorization header. The framing still follows SSE.
export async function readEventStream(
  response: Response,
  signal: AbortSignal,
  receive: (event: StreamEvent) => void,
) {
  if (!response.body) throw new Error("实时更新连接没有内容。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let event = "message";
  let id: string | undefined;
  let retry: number | undefined;
  let size = 0;
  const cancel = () => void reader.cancel().catch(() => {});
  signal.addEventListener("abort", cancel, { once: true });
  function line(value: string) {
    if (value === "") {
      if (data.length && !signal.aborted)
        receive({ event, data: data.join("\n"), id, retry });
      data = [];
      event = "message";
      id = undefined;
      retry = undefined;
      size = 0;
      return;
    }
    size += value.length;
    if (size > 65536) throw new Error("实时更新内容超出限制。");
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "data") data.push(content);
    else if (field === "event") event = content || "message";
    else if (field === "id" && !content.includes("\0")) id = content;
    else if (field === "retry" && /^[0-9]+$/.test(content)) {
      const value = Number(content);
      if (Number.isSafeInteger(value)) retry = value;
    }
  }
  try {
    while (!signal.aborted) {
      const part = await reader.read();
      if (part.done) break;
      buffer += decoder.decode(part.value, { stream: true });
      // A CR at the end of a chunk may be the first half of CRLF.
      let offset = 0;
      for (let index = 0; index < buffer.length; index++) {
        const char = buffer[index];
        if (char !== "\r" && char !== "\n") continue;
        if (char === "\r" && index === buffer.length - 1) break;
        line(buffer.slice(offset, index));
        if (char === "\r" && buffer[index + 1] === "\n") index++;
        offset = index + 1;
      }
      buffer = buffer.slice(offset);
      if (buffer.length > 65536) throw new Error("实时更新内容超出限制。");
    }
    if (!signal.aborted && buffer.endsWith("\r")) line(buffer.slice(0, -1));
    // An unfinished frame at EOF is deliberately not dispatched.
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
