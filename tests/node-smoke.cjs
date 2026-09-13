// No writes to business rows: only static GETs, login, and dashboard reads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const vm = require('node:vm');
const { DatabaseSync } = require('node:sqlite');

const root = path.resolve(__dirname, '..');
const businessHash = () => {
  const db = new DatabaseSync(path.join(root, 'dispatch-demo.sqlite'), { readOnly: true });
  try {
    const payload = {};
    for (const table of ['users', 'stations', 'tasks', 'district_assignments',
      'staff_members', 'task_destinations', 'worker_station_assignments']) {
      payload[table] = db.prepare(`SELECT * FROM ${table} ORDER BY rowid`).all();
    }
    return crypto.createHash('sha256').update(JSON.stringify(payload)).digest('hex');
  } finally {
    db.close();
  }
};
let checks = 0;
// Execute the existing page scripts with actual API data and a minimal DOM/map
// stand-in. This checks script/redirect contracts, not visual browser rendering.
async function pageCheck(base, user) {
  const page = user.role === 'admin' ? 'admin' : 'staff';
  const html = fs.readFileSync(path.join(root, page + '.html'), 'utf8');
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: '', innerHTML: '', value: id === 'district' ? '\u5168\u90e8\u884c\u653f\u5340' : '',
    options: [{}], add(option) { this.options.push(option); }, addEventListener() {},
    classList: { toggle() {} },
  }]));
  const chain = new Proxy({}, { get: (_, name) => name === 'getBounds' ? () => [] : () => chain });
  const context = vm.createContext({
    document: { querySelector: selector => {
      assert.ok(elements.has(selector.slice(1)), 'Missing page element: ' + selector);
      return elements.get(selector.slice(1));
    }, querySelectorAll: () => [] },
    sessionStorage: { getItem: () => JSON.stringify(user), clear() {} },
    location: { href: '' }, fetch: url => fetch(new URL(url, base)),
    Option: function(text, value) { this.text = text; this.value = value; },
    L: { map: () => chain, tileLayer: () => chain, layerGroup: () => chain,
      circleMarker: () => chain, polyline: () => chain }, console,
  });
  await vm.runInContext(fs.readFileSync(path.join(root, page + '.js'), 'utf8'), context);
  assert.ok(elements.get('identity').textContent.includes(user.name));
  assert.ok(elements.get(page === 'admin' ? 'taskRows' : 'stationRows').innerHTML.length);
  if (page === 'admin') {
    const values = elements.get('district').options.map(option => option.value).filter(Boolean);
    assert.equal(values.length, new Set(values).size, 'District dropdown contains duplicate districts');
  } else {
    const stationRows = elements.get('stationRows').innerHTML;
    assert.ok(stationRows.includes('預測暫不可用'), 'Blocked prediction message is missing');
    assert.ok(!stationRows.includes('null 台'), 'Blocked prediction rendered null as a forecast');
  }
  checks++;

  const loginElements = { '#username': { value: user.username }, '#password': { value: 'demo1234' }, '#error': {} };
  let submit, stored;
  loginElements['#loginForm'] = { addEventListener: (_, callback) => { submit = callback; } };
  const loginContext = vm.createContext({ document: { querySelector: id => loginElements[id] },
    fetch: (url, options) => { assert.equal(url, '/api/login'); return fetch(new URL(url, base), options); },
    sessionStorage: { setItem: (_, value) => { stored = JSON.parse(value); } }, location: { href: '' },
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'login.js'), 'utf8'), loginContext);
  await submit({ preventDefault() {} });
  assert.equal(stored.role, user.role);
  assert.equal(loginContext.location.href, '/' + page + '.html');
  checks++;
}
async function run() {
  const before = businessHash();
  const child = spawn(process.execPath, ['--experimental-sqlite', 'server.js'], {
    cwd: root, env: { ...process.env, PORT: '0' }, windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Startup timeout: ' + stderr)), 10000);
      let output = '';
      child.stdout.on('data', data => {
        output += data;
        const match = output.match(/http:\/\/localhost:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(new Error(`Early exit ${code}: ${stderr}`)); });
    });
    for (const [url, type] of [
      ['/', 'text/html'], ['/login.html', 'text/html'], ['/admin.html', 'text/html'], ['/staff.html', 'text/html'],
      ['/styles.css', 'text/css'], ['/enhancements.css', 'text/css'],
      ['/login.js', 'text/javascript'], ['/admin.js', 'text/javascript'], ['/staff.js', 'text/javascript'],
      ['/assets/youbike-logo.png', 'image/png'], ['/youbike-logo.png', 'image/png'],
    ]) {
      const response = await fetch(base + url);
      assert.equal(response.status, 200, url);
      assert.ok(response.headers.get('content-type').startsWith(type), url);
      const body = Buffer.from(await response.arrayBuffer());
      assert.ok(body.length, url);
      if (type === 'image/png') assert.ok(body.equals(fs.readFileSync(path.join(root, 'youbike-logo.png'))));
      checks++;
    }
    for (const url of ['/server.js', '/package.json', '/dispatch-demo.sqlite', '/backend/pipeline.py',
      '/tests/node-smoke.cjs', '/.git/config', '/.model-deps/', '/assets/../dispatch-demo.sqlite',
      '/assets/%2e%2e%2fdispatch-demo.sqlite', '/assets/%2e%2e%5cserver.js']) {
      assert.equal((await fetch(base + url)).status, 404, url);
      checks++;
    }
    for (const [username, role] of [['admin01', 'admin'], ['worker01', 'worker']]) {
      const response = await fetch(base + '/api/login', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password: 'demo1234' }) });
      assert.equal(response.status, 200);
      const user = await response.json();
      assert.equal(user.username, username);
      assert.equal(user.role, role);
      assert.equal(user.password, undefined);
      checks++;
      await pageCheck(base, user);
    }
    for (const url of ['/api/dashboard', '/api/staff-dashboard?username=worker01']) {
      const response = await fetch(base + url);
      assert.equal(response.status, 200, url);
      const data = await response.json();
      for (const key of ['stations', 'tasks', 'assignments']) assert.ok(Array.isArray(data[key]), key);
      assert.ok(data.stations.length > 0, url);
      console.log(`${url}: ${data.stations.length} stations, ${data.tasks.length} tasks`);
      checks++;
    }
  } finally {
    if (child.exitCode === null) { const ended = once(child, 'exit'); child.kill(); await ended; }
    assert.equal(businessHash(), before, 'Server startup/read-only requests changed SQLite business data');
    checks++;
  }
  console.log(`PASS: ${checks} smoke checks; SQLite business SHA-256 unchanged`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
