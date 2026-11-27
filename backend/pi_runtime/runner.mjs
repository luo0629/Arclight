import { createInterface } from 'node:readline';
import { Agent } from '@earendil-works/pi-agent-core';
import { createAssistantMessageEventStream, getCurrentTools } from '@earendil-works/pi-ai';

// Python's gateway resolves the model and capabilities. Pi uses this identity
// only to associate assistant messages with the custom stream.
const gatewayModel = {
  id: 'arclight-gateway',
  name: 'Arclight Model Gateway',
  api: 'unknown',
  provider: 'arclight',
  baseUrl: '',
  reasoning: false,
  input: [],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 0,
  maxTokens: 0,
};

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0;
}

function isTurn(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function toolIdentity(turn, id) {
  if (!isTurn(turn) || !isNonEmptyString(id)) throw new Error('Invalid tool identity');
  return String(turn) + ':' + id;
}

function toolReference(id, name) {
  const separator = id.indexOf(':');
  const turn = Number(id.slice(0, separator));
  const callId = id.slice(separator + 1);
  if (toolIdentity(turn, callId) !== id || !isNonEmptyString(name)) {
    throw new Error('Invalid tool reference');
  }
  return { turn, id: callId, name };
}

function toolCalls(turn, calls, allowed = undefined) {
  if (!isTurn(turn) || !Array.isArray(calls) || calls.length === 0 || calls.length > 16) {
    throw new Error('Invalid tool batch');
  }
  const ids = new Set();
  return calls.map((call) => {
    if (!isObject(call)) throw new Error('Invalid tool call');
    const { id, name, arguments: args, signature } = call;
    if (
      ids.has(id) || !isNonEmptyString(name) || (allowed && !allowed.has(name)) ||
      !isObject(args) || (signature !== undefined && typeof signature !== 'string')
    ) {
      throw new Error('Invalid tool call');
    }
    ids.add(id);
    return {
      type: 'toolCall',
      id: toolIdentity(turn, id),
      name,
      arguments: args,
      ...(signature === undefined ? {} : { thoughtSignature: signature }),
    };
  });
}

function textContent(text) {
  if (typeof text !== 'string') throw new Error('Invalid message text');
  return [{ type: 'text', text }];
}

function userMessage(text) {
  return { role: 'user', content: textContent(text), timestamp: 0 };
}

function assistantMessage(content, reason, usage = {}) {
  if (!isObject(usage)) throw new Error('Invalid model usage');
  const { input_tokens: input = 0, output_tokens: output = 0 } = usage;
  if (![input, output, input + output].every((count) => Number.isSafeInteger(count) && count >= 0)) {
    throw new Error('Invalid model usage');
  }
  return {
    role: 'assistant',
    content,
    api: gatewayModel.api,
    provider: gatewayModel.provider,
    model: gatewayModel.id,
    usage: {
      input, output, cacheRead: 0, cacheWrite: 0, totalTokens: input + output,
      cost: { ...gatewayModel.cost, total: 0 },
    },
    stopReason: reason,
    timestamp: Date.now(),
  };
}

function toolResult(result, isError) {
  if (typeof isError !== 'boolean') throw new Error('Invalid tool outcome');
  return { content: textContent(result), details: {}, isError };
}

function historyMessage(message) {
  if (!isObject(message)) throw new Error('Invalid history message');
  if (message.role === 'user') return userMessage(message.text);
  if (message.role === 'assistant') {
    return assistantMessage(toolCalls(message.turn, message.calls), 'toolUse');
  }
  if (message.role === 'tool' && isNonEmptyString(message.name)) {
    return {
      role: 'toolResult',
      toolCallId: toolIdentity(message.turn, message.id),
      toolName: message.name,
      ...toolResult(message.result, message.isError),
      timestamp: 0,
    };
  }
  throw new Error('Invalid history role');
}

function modelContext(context) {
  const messages = [];
  for (const message of context.messages) {
    if (message.role === 'system') continue;
    if (message.role === 'user') {
      messages.push({ role: 'user', text: message.content[0].text });
    } else if (message.role === 'assistant') {
      const references = message.content
        .filter((item) => item.type === 'toolCall')
        .map((item) => toolReference(item.id, item.name));
      const turn = references[0]?.turn;
      if (!turn || references.some((reference) => reference.turn !== turn)) {
        throw new Error('Invalid assistant history');
      }
      messages.push({
        role: 'assistant', turn, calls: references.map(({ id, name }) => ({ id, name })),
      });
    } else if (message.role === 'toolResult') {
      messages.push({ role: 'tool', ...toolReference(message.toolCallId, message.toolName) });
    } else {
      throw new Error('Invalid context role');
    }
  }
  return { messages, tools: getCurrentTools(context.messages) };
}

function completedStream(content, reason, usage) {
  const stream = createAssistantMessageEventStream();
  stream.push({ type: 'done', reason, message: assistantMessage(content, reason, usage) });
  return stream;
}

function send(frame) {
  process.stdout.write(JSON.stringify(frame) + '\n');
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
  const identity = start.agent;
  if (
    start.type !== 'start' || !isObject(identity) ||
    ![identity.name, identity.stage, identity.instruction].every(isNonEmptyString) ||
    !Array.isArray(start.messages) || start.messages[0]?.role !== 'user'
  ) {
    throw new Error('Invalid start frame');
  }
  const history = start.messages.map(historyMessage);
  let latestTurn = start.messages.reduce((turn, message) => Math.max(turn, message.turn ?? 0), 0);
  let agent;
  let stopped = false;
  let protocolFailed = false;
  const dispatched = new Set();

  async function exchange(frame, convert) {
    try {
      send(frame);
      const reply = await read();
      if (reply.type !== 'continue' && reply.type !== 'stop') throw new Error('Invalid host reply');
      return convert(reply);
    } catch (error) {
      protocolFailed = true;
      agent?.abort();
      throw error;
    }
  }

  async function dispatchTool(type, name, key) {
    return exchange({ type, ...toolReference(key, name) }, (reply) => {
      if (reply.type === 'stop') {
        stopped = true;
        agent.abort();
        return toolResult(reply.result ?? '{}', true);
      }
      return toolResult(reply.result, reply.isError);
    });
  }

  async function prepare() {
    return exchange({ type: 'prepare' }, (reply) => {
      if (reply.type === 'stop') {
        stopped = true;
        return undefined;
      }
      if (!Array.isArray(reply.tools) || !Array.isArray(reply.messages)) {
        throw new Error('Invalid preparation');
      }
      const names = new Set();
      const tools = reply.tools.map((tool) => {
        if (!isObject(tool)) throw new Error('Invalid tool declaration');
        const { name, description, parameters } = tool;
        if (
          !isNonEmptyString(name) || names.has(name) || !isNonEmptyString(description) ||
          !isObject(parameters)
        ) {
          throw new Error('Invalid tool declaration');
        }
        names.add(name);
        return {
          name, label: name, description, parameters,
          execute: (key) => {
            dispatched.add(key);
            return dispatchTool('tool', name, key);
          },
        };
      });
      return { tools, messages: reply.messages.map(userMessage) };
    });
  }

  const initial = await prepare();
  if (!initial) {
    send({ type: 'done' });
    return;
  }
  agent = new Agent({
    initialState: {
      model: gatewayModel,
      systemPrompt: identity.instruction,
      tools: initial.tools,
      messages: [...history, ...initial.messages],
    },
    toolExecution: 'sequential',
    finishTurn: () => (stopped || protocolFailed ? { action: 'end' } : undefined),
    prepareNextTurnWithContext: async ({ context }) => {
      const next = await prepare();
      if (!next) return undefined;
      return { context: { ...context, tools: next.tools }, messages: next.messages };
    },
    streamFn: async (_model, context) => {
      if (stopped || protocolFailed) return completedStream([], 'stop');
      const request = modelContext(context);
      return exchange({ type: 'model', ...request }, (reply) => {
        if (reply.type === 'stop') {
          const content = reply.result === undefined ? [] : textContent(reply.result);
          const stream = completedStream(content, 'stop', reply.usage);
          stopped = true;
          return stream;
        }
        if (!isTurn(reply.turn) || reply.turn <= latestTurn) throw new Error('Invalid model turn');
        latestTurn = reply.turn;
        const allowed = new Set(request.tools.map((tool) => tool.name));
        return completedStream(toolCalls(reply.turn, reply.calls, allowed), 'toolUse', reply.usage);
      });
    },
  });
  agent.subscribe(async (event, signal) => {
    if (event.type !== 'tool_execution_end') return;
    if (dispatched.delete(event.toolCallId) || stopped || protocolFailed || signal.aborted) return;

    // Pi's schema validation can reject a call before execute. Journal that
    // outcome through the host before Pi sends its next model request.
    await dispatchTool('tool_rejected', event.toolName, event.toolCallId);
  });

  await agent.continue();
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
