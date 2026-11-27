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

const query = {
  id: 'call_查看', name: 'investigate_production', arguments: {
    scene: '竹林', limit: '8', flags: [false, null],
  },
};
const fetch = { id: 'call_2', name: 'retrieve_evidence', arguments: { evidence: [1, 2] } };
const start = {
  type: 'start',
  agent: {
    name: 'director',
    stage: 'planning',
    instruction: '基于生产证据制定分镜。',
    input: [{ role: 'user', data: { scene: '竹林' }, image_file_ids: [] }],
  },
};
const resume = {
  ...start,
  resume: 'tools',
  calls: [query, fetch],
  tools: [query.name, fetch.name],
};

test('Pi dispatches each model-selected tool in order and requests another model round', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ ...start, resume: 'model' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({
    type: 'continue',
    calls: [{ ...query, signature: 'opaque-value' }, fetch],
    tools: [query.name, fetch.name],
    usage: { input_tokens: 160, output_tokens: 40 },
  });
  assert.deepEqual(await child.read(), { type: 'tool', ...query });
  child.send({ type: 'continue', result: { status: 'SUCCEEDED', result: { evidence: '竹林' } } });
  assert.deepEqual(await child.read(), { type: 'tool', ...fetch });
  child.send({ type: 'continue', result: { status: 'FAILED', error: { code: 'STILL_AVAILABLE' } } });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'continue', calls: [{ ...query, id: 'second-round' }], tools: [query.name] });
  assert.deepEqual(await child.read(), { type: 'tool', ...query, id: 'second-round' });
  child.send({ type: 'continue' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'stop', result: { decision: 'COMPLETE' }, usage: { output_tokens: 12 } });
  assert.deepEqual(await child.read(), { type: 'done' });
  await child.expectExit(0);
});

test('recovery dispatches recorded calls before asking for another model response', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(resume);
  assert.deepEqual(await child.read(), { type: 'tool', ...query });
  child.send({ type: 'continue' });
  assert.deepEqual(await child.read(), { type: 'tool', ...fetch });
  child.send({ type: 'continue' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'stop' });
  assert.deepEqual(await child.read(), { type: 'done' });
  await child.expectExit(0);
});

test('host can stop Pi during a tool batch without dispatching later calls', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(resume);
  assert.deepEqual(await child.read(), { type: 'tool', ...query });
  child.send({ type: 'stop' });
  assert.deepEqual(await child.read(), { type: 'done' });
  await child.expectExit(0);
});

test('a closed host pipe ends the process without dispatching further calls', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send(resume);
  assert.deepEqual(await child.read(), { type: 'tool', ...query });
  child.disconnect();
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('an invalid model batch fails without logging model response data', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ ...start, resume: 'model' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'continue', calls: [query, query], tools: [query.name] });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('each Pi model response may dispatch only the currently declared tools', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ ...start, resume: 'model' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'continue', calls: [query], tools: [query.name, fetch.name] });
  assert.deepEqual(await child.read(), { type: 'tool', ...query });
  child.send({ type: 'continue' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'continue', calls: [fetch], tools: [query.name] });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('a model response with invalid usage cannot terminate the session successfully', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ ...start, resume: 'model' });
  assert.deepEqual(await child.read(), { type: 'model' });
  child.send({ type: 'stop', usage: { input_tokens: -1 } });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});

test('start frame requires the role instruction and frozen user messages', { timeout: 10000 }, async (t) => {
  const child = startRuntime(t);
  child.send({ ...start, resume: 'model', agent: { name: 'director', stage: 'planning' } });
  assert.deepEqual(await child.read(), { type: 'error' });
  await child.expectExit(1);
});
