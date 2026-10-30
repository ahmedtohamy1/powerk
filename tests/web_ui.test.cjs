const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

// Run the actual embedded UI with a minimal DOM that forbids HTML parsing.
const script = fs.readFileSync('powerk.py', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  children = [];
  textContent = '';
  attributes = {};
  dataset = {};
  append(child) { child.parent = this; this.children.push(child); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  scrollIntoView() {}
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  set innerHTML(value) { throw Error('Device fields must never be parsed as HTML'); }
}
const state = {
  local_ip: '127.0.0.1', port: 10086,
  devices: [{ mac: 'AABBCCDDEEFF', name: 'test', model: '<img src=x onerror=alert(1)>',
    fw: '<script>malicious()</script>', on: false, online: true, last_seen: 1,
    power_w: 0, energy_kwh: 0,
    outlets: [{ n: 1, on: false, power_w: 0, temp_c: 30 }] }],
};
function harness(initialState = state) {
  const elements = {};
  const storage = new Map();
  const requests = [];
  const context = vm.createContext({
    document: {
      documentElement: new Element(),
      getElementById: id => elements[id] ||= new Element(),
      createElement: () => new Element(),
    },
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    location: { search: '?t=secret', pathname: '/' },
    history: { replaceState() {} }, URLSearchParams, AbortSignal,
    setTimeout() {},
    fetch: async (path, options) => {
      requests.push({ path, options });
      return { ok: true, text: async () => JSON.stringify(initialState) };
    },
  });
  vm.runInContext(script, context);
  return { context, elements, requests, storage };
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
  assert.equal(controls.length, 3);
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
  assert.equal(elements.error.dataset.kind, 'error');
  assert.ok(descendants(elements.app).filter(el => el.onclick).every(el => !el.disabled));
});

test('HTTP errors retain the server message', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  context.fetch = async () => ({ ok: false, status: 503, text: async () => '{"error":"device offline"}' });
  await vm.runInContext("cmd('AABBCCDDEEFF',1,true)", context);
  assert.equal(elements.error.textContent, 'device offline');
});

test('live updates preserve outlet controls and open device details', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  const toggle = descendants(elements.app).find(el => el.getAttribute('role') === 'switch');
  const details = descendants(elements.app).find(el => el.className === 'device-details');
  details.open = true;
  context.fetch = async () => ({ok:true, text:async () => JSON.stringify({
    ...state, devices:[{...state.devices[0], power_w:23, outlets:[{...state.devices[0].outlets[0], on:true, power_w:23}]}],
  })});
  await vm.runInContext('refresh()', context);
  const updated = descendants(elements.app).find(el => el.getAttribute('role') === 'switch');
  assert.equal(updated, toggle);
  assert.equal(toggle.getAttribute('aria-checked'), 'true');
  assert.equal(details.open, true);
  assert.equal(elements['total-power'].textContent, '23');
  assert.equal(elements['active-outlets'].textContent, '1');
});

test('all on and all off send explicit values for mixed outlet states', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  const commands = [];
  context.fetch = async (path, options) => {
    if (options.body) commands.push(JSON.parse(options.body));
    return {ok:true, text:async () => JSON.stringify(options.body ? {confirmed:true} : state)};
  };
  await descendants(elements.app).find(el => el.textContent === 'All on').onclick();
  await descendants(elements.app).find(el => el.textContent === 'All off').onclick();
  assert.deepEqual(commands, [
    {mac:'AABBCCDDEEFF', outlet:0, on:true},
    {mac:'AABBCCDDEEFF', outlet:0, on:false},
  ]);
  assert.equal(elements.error.dataset.kind, 'success');
});

test('offline readings do not appear as live totals', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  context.fetch = async () => ({ok:true, text:async () => JSON.stringify({
    ...state, devices:[{...state.devices[0], online:false, power_w:100, energy_kwh:12}],
  })});
  await vm.runInContext('refresh()', context);
  assert.equal(elements['total-power'].textContent, '—');
  assert.equal(elements['active-outlets'].textContent, '—');
  assert.equal(elements['total-energy'].textContent, '12');
  assert.match(elements['energy-note'].textContent, /stale/);
  assert.ok(descendants(elements.app).filter(el => el.onclick).every(el => el.disabled));
});

test('theme choice is persisted and setup opens on request', async () => {
  const { context, elements, storage } = harness();
  await vm.runInContext('refresh()', context);
  elements.theme.onclick();
  assert.equal(storage.get('powerk-theme'), 'dark');
  assert.equal(elements.theme.getAttribute('aria-pressed'), 'true');
  elements['setup-button'].onclick();
  assert.equal(elements.setup.open, true);
});

test('removed devices and outlets do not leave stale controls', async () => {
  const { context, elements } = harness();
  await vm.runInContext('refresh()', context);
  context.fetch = async () => ({ok:true, text:async () => JSON.stringify({...state, devices:[]})});
  await vm.runInContext('refresh()', context);
  assert.equal(elements.app.children.length, 0);
  assert.equal(elements.empty.hidden, false);
  assert.match(elements['empty-title'].textContent, /first connection/);
});

test('all four outlet switches send on and off commands to the correct strip', async () => {
  const first = {...state.devices[0], outlets:[1, 2, 3, 4].map(n => ({
    n, on:false, power_w:0, energy_kwh:0, temp_c:30,
  }))};
  const second = {...first, mac:'112233445566', name:'second strip', outlets:first.outlets.map(o => ({...o}))};
  const live = {...state, devices:[first, second]};
  const { context, elements } = harness(live);
  await vm.runInContext('refresh()', context);
  const sent = [];
  context.fetch = async (path, options) => {
    assert.equal(options.headers['X-Token'], 'secret');
    if (options.body) {
      assert.equal(path, 'api/onoff');
      const command = JSON.parse(options.body);
      sent.push(command);
      const strip = live.devices.find(d => d.mac === command.mac);
      strip.outlets.find(o => o.n === command.outlet).on = command.on;
    } else {
      assert.equal(path, 'api/state');
    }
    return {ok:true, text:async () => JSON.stringify(options.body ? {ok:true, confirmed:true} : live)};
  };
  for (const strip of live.devices) {
    for (const n of [1, 2, 3, 4]) {
      const toggle = descendants(elements.app).find(el => el.getAttribute('aria-label') === `Outlet ${n} on ${strip.name}`);
      assert.ok(toggle, `Missing outlet ${n} on ${strip.name}`);
      await toggle.onclick();
      assert.deepEqual(sent.at(-1), {mac:strip.mac, outlet:n, on:true});
      assert.equal(toggle.getAttribute('aria-checked'), 'true');
      await toggle.onclick();
      assert.deepEqual(sent.at(-1), {mac:strip.mac, outlet:n, on:false});
      assert.equal(toggle.getAttribute('aria-checked'), 'false');
    }
  }
  assert.equal(sent.length, 16);
});

test('the main strip view retains all original readings and power precision', async () => {
  const strip = {...state.devices[0], model:'MTTL-W01', fw:'0.1.54-1.0.66',
    power_w:0.01, energy_kwh:0.001, voltage:224.5, current_a:0.02, rssi:-52,
    outlets:[{n:1, on:true, power_w:0.01, energy_kwh:0.001, temp_c:31}],
  };
  const { context, elements } = harness({...state, devices:[strip]});
  await vm.runInContext('refresh()', context);
  const caption = descendants(elements.app).find(el => el.className === 'strip-caption');
  assert.match(caption.textContent, /MTTL-W01/);
  assert.match(caption.textContent, /0\.1\.54-1\.0\.66/);
  const readings = descendants(elements.app).find(el => el.className === 'strip-summary').textContent;
  for (const expected of ['0.01 W', '0.001 kWh', '224.5 V', '0.02 A', '-52 dBm']) {
    assert.ok(readings.includes(expected), `Missing visible reading: ${expected}`);
  }
  assert.equal(elements['total-power'].textContent, '0.01');
  assert.ok(descendants(elements.app).some(el => el.className === 'outlet-detail' && el.textContent.includes('31 °C')));
  assert.ok(descendants(elements.app).some(el => el.className === 'outlet-power' && el.children.some(child => child.textContent === '0.01')));
});
