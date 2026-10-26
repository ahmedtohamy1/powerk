const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

// Run the actual embedded UI with a minimal DOM that forbids HTML parsing.
const script = fs.readFileSync('powerk.py', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  children = [];
  textContent = '';
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  set innerHTML(value) { throw Error('Device fields must never be parsed as HTML'); }
}
const state = {
  local_ip: '127.0.0.1', port: 10086,
  devices: [{ mac: 'AABBCCDDEEFF', name: 'test', model: '<img src=x onerror=alert(1)>',
    fw: '<script>malicious()</script>', on: false, online: true, last_seen: 1,
    power_w: 0, energy_kwh: 0,
    outlets: [{ n: 1, on: false, power_w: 0, temp_c: 30 }] }],
};
function harness() {
  const elements = Object.fromEntries(['hint', 'app', 'error'].map(id => [id, new Element()]));
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: id => elements[id], createElement: () => new Element() },
    location: { search: '?t=secret', pathname: '/' },
    history: { replaceState() {} }, URLSearchParams, AbortSignal,
    setTimeout() {},
    fetch: async (path, options) => {
      requests.push({ path, options });
      return { ok: true, text: async () => JSON.stringify(state) };
    },
  });
  vm.runInContext(script, context);
  return { context, elements, requests };
}
function descendants(element) {
  return [element, ...element.children.flatMap(descendants)];
}

test('device metadata stays text and query token authenticates API calls', async () => {
  const { context, elements, requests } = harness();
  await vm.runInContext('refresh()', context);
  assert.ok(descendants(elements.app).some(el => el.textContent.includes(state.devices[0].model)));
  assert.equal(requests[0].options.headers['X-Token'], 'secret');
});

test('failed polling disables controls and shows stale state', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  context.fetch = async () => { throw Error('network unavailable'); };
  await vm.runInContext('refresh()', context);
  assert.match(elements.error.textContent, /Connection lost: network unavailable/);
  const controls = descendants(elements.app).filter(el => el.onclick);
  assert.equal(controls.length, 2);
  assert.ok(controls.every(el => el.disabled));
});

test('unconfirmed command is visible and pending controls block duplicate commands', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  let complete;
  let commands = 0;
  context.fetch = async () => {
    commands++;
    await new Promise(resolve => { complete = resolve; });
    return { ok: true, text: async () => '{"ok":false,"confirmed":false}' };
  };
  const command = vm.runInContext("cmd('AABBCCDDEEFF',1,true)", context);
  await vm.runInContext("cmd('AABBCCDDEEFF',1,true)", context);
  assert.equal(commands, 1);
  assert.ok(descendants(elements.app).filter(el => el.onclick).every(el => el.disabled));
  complete();
  await command;
  assert.match(elements.error.textContent, /not confirmed/);
  assert.ok(descendants(elements.app).filter(el => el.onclick).every(el => !el.disabled));
});

test('HTTP errors retain the server message', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  context.fetch = async () => ({ ok: false, status: 503, text: async () => '{"error":"device offline"}' });
  await vm.runInContext("cmd('AABBCCDDEEFF',1,true)", context);
  assert.equal(elements.error.textContent, 'device offline');
});
