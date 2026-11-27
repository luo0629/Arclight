import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

function startRuntime(t) {
  const node = spawn(process.execPath, [fileURLToPath(new URL('runner.mjs', import.meta.url))]);
  const exit = once(node, 'exit');
  const stream = createInterface({ input: node.stdout });
  const lines = stream[Symbol.asyncIterator]();
  t.after(() => {
    stream.close();
    if (node.exitCode === null) node.kill();
  });
  return {
    send: (frame) => node.stdin.write(`${JSON.stringify(frame)}\n`),
    disconnect: () => node.stdin.end(),
    async read() {
      const { value, done } = await lines.next();
      assert.equal(done, false, 'child closed before sending its next frame');
      return JSON.parse(value);
    },
    async expectExit(code) {
      assert.equal((await lines.next()).done, true, 'child emitted an extra frame');
      assert.deepEqual(await exit, [code, null]);
    },
  };
}

const investigate = {
  name: 'investigate_production',
  description: 'Read production status',
  parameters: {
    type: 'object',
    properties: { scene: { type: 'string' }, limit: { type: 'integer' } },
    required: ['scene'],
    additionalProperties: false,
  },
};
const retrieve = {
  name: 'retrieve_evidence',
  description: 'Read referenced evidence',
  parameters: {
    type: 'object',
    properties: { evidence: { type: 'array', items: { type: 'integer' } } },
    required: ['evidence'],
    additionalProperties: false,
  },
};
const query = {
  id: 'call_1', name: investigate.name, arguments: { scene: '竹林', limit: 8 },
  signature: 'opaque-value',
};
const fetch = { id: 'call_2', name: retrieve.name, arguments: { evidence: [1, 2] } };
const seed = { role: 'user', text: '{"role":"user","data":{"scene":"竹林","precise":9007199254740993}}' };
const observation = '{"role":"user","data":{"status":"ready"},"image_file_ids":["abc"]}';
const start = {
  type: 'start',
  agent: {
    name: 'director',
    stage: 'planning',
    instruction: '基于生产证据制定分镜。',
  },
  messages: [seed],
};
const result = { type: 'continue', result: '{"status":"SUCCEEDED"}', isError: false };

function callReference(call) {
  return { id: call.id, name: call.name };
}

function assistantReference(turn, calls) {
  return { role: 'assistant', turn, calls: calls.map(callReference) };
}

function toolReference(turn, call) {
  return { role: 'tool', turn, ...callReference(call) };
}

async function prepare(child, tools, messages = []) {
  assert.deepEqual(await child.read(), { type: 'prepare' });
  child.send({ type: 'continue', tools, messages });
}

async function expectModel(child, messages, tools) {
  assert.deepEqual(await child.read(), { type: 'model', messages, tools });
}

async function finish(child, response = { type: 'stop' }) {
  child.send(response);
  assert.deepEqual(await child.read(), { type: 'done' });
  await child.expectExit(0);
}

test('Pi sends actual schemas and carries ordered tool results to its next model call', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  const tools = [investigate, retrieve];
  const observed = { role: 'user', text: observation };
  child.send(start);
  await prepare(child, tools, [observation]);
  await expectModel(child, [seed, observed], tools);
  child.send({
    type: 'continue',
    turn: 1,
    calls: [query, fetch],
    usage: { input_tokens: 160, output_tokens: 40 },
  });
  assert.deepEqual(await child.read(), { type: 'tool', turn: 1, ...callReference(query) });
  child.send(result);
  assert.deepEqual(await child.read(), { type: 'tool', turn: 1, ...callReference(fetch) });
  child.send({ type: 'continue', result: '{"error":"STILL_AVAILABLE"}', isError: true });

  const later = { role: 'user', text: '{"role":"user","data":{"evidence":"竹林"}}' };
  await prepare(child, [retrieve], [later.text]);
  await expectModel(child, [
    seed, observed, assistantReference(1, [query, fetch]),
    toolReference(1, query), toolReference(1, fetch), later,
  ], [retrieve]);
  await finish(child, { type: 'stop', result: '{"decision":"COMPLETE"}', usage: { output_tokens: 12 } });
});

test('restores completed history and dispatches a recorded response through the same model stream', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  const tools = [investigate, retrieve];
  const prior = [
    seed,
    { role: 'assistant', turn: 2, calls: [query] },
    { ...toolReference(2, query), result: '{"status":"SUCCEEDED"}', isError: false },
  ];
  child.send({ ...start, messages: prior });
  await prepare(child, tools);
  const restored = [seed, assistantReference(2, [query]), toolReference(2, query)];
  await expectModel(child, restored, tools);
  child.send({ type: 'continue', turn: 3, calls: [query, fetch] });
  assert.deepEqual(await child.read(), { type: 'tool', turn: 3, ...callReference(query) });
  child.send(result);
  assert.deepEqual(await child.read(), { type: 'tool', turn: 3, ...callReference(fetch) });
  child.send(result);
  await prepare(child, tools);
  await expectModel(child, [
    ...restored, assistantReference(3, [query, fetch]), toolReference(3, query), toolReference(3, fetch),
  ], tools);
  await finish(child);
});

test('Pi reports invalid schema arguments without dispatching the tool', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  const invalid = { ...query, arguments: {} };
  child.send(start);
  await prepare(child, [investigate]);
  await expectModel(child, [seed], [investigate]);
  child.send({ type: 'continue', turn: 1, calls: [invalid] });
  assert.deepEqual(await child.read(), { type: 'tool_rejected', turn: 1, ...callReference(invalid) });
  child.send({ type: 'continue', result: '{"error":"INVALID_ARGUMENTS"}', isError: true });
  await prepare(child, [investigate]);
  await expectModel(child, [
    seed, assistantReference(1, [invalid]), toolReference(1, invalid),
  ], [investigate]);
  await finish(child);
});

test('host can stop Pi during a tool batch without dispatching later calls', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  const tools = [investigate, retrieve];
  child.send(start);
  await prepare(child, tools);
  await expectModel(child, [seed], tools);
  child.send({ type: 'continue', turn: 1, calls: [query, fetch] });
  assert.deepEqual(await child.read(), { type: 'tool', turn: 1, ...callReference(query) });
  await finish(child);
});

test('host can stop before the first model call', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  assert.deepEqual(await child.read(), { type: 'prepare' });
  await finish(child);
});

test('host can stop while preparing the next turn', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  await prepare(child, [investigate]);
  await expectModel(child, [seed], [investigate]);
  child.send({ type: 'continue', turn: 1, calls: [query] });
  assert.deepEqual(await child.read(), { type: 'tool', turn: 1, ...callReference(query) });
  child.send(result);
  assert.deepEqual(await child.read(), { type: 'prepare' });
  await finish(child);
});

test('a closed host pipe ends the process without dispatching further calls', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  await prepare(child, [investigate, retrieve]);
  await expectModel(child, [seed], [investigate, retrieve]);
  child.send({ type: 'continue', turn: 1, calls: [query, fetch] });
  assert.deepEqual(await child.read(), { type: 'tool', turn: 1, ...callReference(query) });
  child.disconnect();
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('an invalid model batch fails without logging model response data', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  await prepare(child, [investigate]);
  await expectModel(child, [seed], [investigate]);
  child.send({ type: 'continue', turn: 1, calls: [query, query] });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('each Pi model response may dispatch only the currently declared tools', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  await prepare(child, [investigate]);
  await expectModel(child, [seed], [investigate]);
  child.send({ type: 'continue', turn: 1, calls: [fetch] });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('a model response with invalid usage cannot terminate the session successfully', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(start);
  await prepare(child, [investigate]);
  await expectModel(child, [seed], [investigate]);
  child.send({ type: 'stop', usage: { input_tokens: -1 } });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('start frame requires the role instruction and frozen user messages', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ type: 'start', agent: { name: 'director', stage: 'planning' } });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});
