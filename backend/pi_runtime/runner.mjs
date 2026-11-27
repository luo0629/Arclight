import { createInterface } from 'node:readline';
import { Agent } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';

// Pi requires an API identifier in the model descriptor. The custom stream resolves
// every model request through Python's gateway.
const model = {
  id: 'arclight-gateway',
  name: 'Arclight Model Gateway',
  api: 'openai-completions',
  provider: 'arclight',
  baseUrl: '',
  reasoning: false,
  input: ['text'],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 1,
  maxTokens: 1,
};
const toolParameters = { type: 'object', additionalProperties: true };

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function toolBatch(value, names) {
  if (!Array.isArray(names) || !names.every((name) => typeof name === 'string' && name)) {
    throw new Error('Invalid tool declarations');
  }
  if (!Array.isArray(value) || value.length === 0 || value.length > 16) {
    throw new Error('Invalid tool batch');
  }
  const tools = new Set(names);
  const ids = new Set();
  const calls = value.map(({ id, name, arguments: args, signature }) => {
    if (typeof id !== 'string' || !id || ids.has(id) || !tools.has(name) || !isObject(args)) {
      throw new Error('Invalid tool call');
    }
    ids.add(id);
    return {
      type: 'toolCall',
      id,
      name,
      arguments: args,
      ...(typeof signature === 'string' ? { thoughtSignature: signature } : {}),
    };
  });
  return { calls, tools };
}

function completedStream(content, reason, usage = {}) {
  if (!isObject(usage)) throw new Error('Invalid model usage');
  const { input_tokens: input = 0, output_tokens: output = 0 } = usage;
  if (![input, output, input + output].every((count) => Number.isSafeInteger(count) && count >= 0)) {
    throw new Error('Invalid model usage');
  }
  const stream = createAssistantMessageEventStream();
  stream.push({
    type: 'done',
    reason,
    message: {
      role: 'assistant', content, api: model.api, provider: model.provider, model: model.id,
      usage: { input, output, cacheRead: 0, cacheWrite: 0, totalTokens: input + output, cost: { ...model.cost, total: 0 } },
      stopReason: reason,
      timestamp: Date.now(),
    },
  });
  return stream;
}

function send(frame) {
  process.stdout.write(`${JSON.stringify(frame)}\n`);
}

async function run(frames) {
  async function read() {
    const { value, done } = await frames.next();
    if (done) throw new Error('Host closed the request stream');
    const frame = JSON.parse(value);
    if (!isObject(frame)) throw new Error('Invalid protocol frame');
    return frame;
  }

  const start = await read();
  if (start.type !== 'start' || !['model', 'tools'].includes(start.resume)) {
    throw new Error('Invalid start frame');
  }
  const identity = start.agent;
  if (!isObject(identity) || ![identity.name, identity.stage, identity.instruction].every(
    (value) => typeof value === 'string' && value,
  ) || !Array.isArray(identity.input) || !identity.input.length || !identity.input.every(
    (message) => isObject(message) && message.role === 'user',
  )) {
    throw new Error('Invalid agent');
  }
  let savedBatch = start.resume === 'tools' ? toolBatch(start.calls, start.tools) : undefined;
  let requestContext;
  let stopped = false;
  let protocolFailed = false;

  async function exchange(frame) {
    try {
      send(frame);
      const reply = await read();
      if (reply.type !== 'continue' && reply.type !== 'stop') throw new Error('Invalid host reply');
      return reply;
    } catch (error) {
      protocolFailed = true;
      agent.abort();
      throw error;
    }
  }

  async function executeTool(name, id, args) {
    const reply = await exchange({ type: 'tool', id, name, arguments: args });
    if (reply.type === 'stop') {
      stopped = true;
      agent.abort();
    }
    const result = reply.result;
    return {
      content: [{ type: 'text', text: JSON.stringify(result ?? {}) }],
      details: result ?? {},
      isError: Boolean(result?.error),
    };
  }

  const agent = new Agent({
    initialState: { model, systemPrompt: identity.instruction },
    toolExecution: 'sequential',
    prepareRequest: ({ context }) => { requestContext = context; },
    streamFn: async () => {
      if (stopped || protocolFailed) return completedStream([], 'stop');
      let batch = savedBatch;
      savedBatch = undefined;
      let usage;
      if (!batch) {
        const reply = await exchange({ type: 'model' });
        usage = reply.usage;
        if (reply.type === 'stop') {
          const stream = completedStream(reply.result === undefined ? [] : [{
            type: 'text', text: JSON.stringify(reply.result),
          }], 'stop', usage);
          stopped = true;
          return stream;
        }
        batch = toolBatch(reply.calls, reply.tools);
      }
      // Python supplies the authorized tools and validates their original arguments at dispatch.
      requestContext.tools = Array.from(batch.tools, (name) => ({
        name,
        label: name,
        description: `Dispatch ${name} through the Arclight tool registry`,
        parameters: toolParameters,
        execute: (id, args) => executeTool(name, id, args),
      }));
      return completedStream(batch.calls, 'toolUse', usage);
    },
  });

  // The gateway transcript is durable in Python. On recovery the saved response is
  // supplied to Pi as the first completed stream, so Pi dispatches remaining work.
  await agent.prompt(JSON.stringify(identity.input));
  if (protocolFailed || !stopped) throw new Error('Pi session ended before the host stopped it');
  send({ type: 'done' });
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
try {
  await run(input[Symbol.asyncIterator]());
} catch {
  // A peer frame can contain provider data. Keep exception text out of this transport.
  send({ type: 'error' });
  process.exitCode = 1;
} finally {
  input.close();
}
