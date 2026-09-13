// Round 2 vertical slice: Node spawns Python, caches integration rows, and
// exposes Python station state/alerts without changing business tables.
const assert = require('node:assert/strict');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { DatabaseSync } = require('node:sqlite');

const root = path.resolve(__dirname, '..');
const demoRoot = process.env.YOUBIKE_DEMO_ROOT || 'C:\\Users\\gkjk0\\Downloads\\Ubike-main\\Ubike-main';
const realtimeCsv = [
  path.join(demoRoot, 'data', 'youbike_new_taipei_20260912_220009.csv'),
  path.join(demoRoot, 'data', 'youbike_new_taipei_20260912_223029.csv'),
];

function businessHash() {
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
}

async function run() {
  const before = businessHash();
  const child = spawn(process.execPath, ['--experimental-sqlite', 'server.js'], {
    cwd: root,
    env: {
      ...process.env,
      PORT: '0',
      PYTHONIOENCODING: 'utf-8',
      YOUBIKE_REALTIME_CSV: realtimeCsv.join(path.delimiter),
      YOUBIKE_MODEL_PACK: path.join(demoRoot, 'ntpc_youbike_optimized_pack.zip'),
    },
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Startup timeout: ' + stderr)), 20000);
      let output = '';
      child.stdout.on('data', data => {
        output += data;
        const match = output.match(/http:\/\/localhost:\d+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('exit', code => { clearTimeout(timer); reject(new Error(`Early exit ${code}: ${stderr}`)); });
    });
    const dashboard = await fetch(base + '/api/dashboard').then(response => response.json());
    assert.equal(dashboard.meta.source, 'PYTHON_PIPELINE');
    assert.equal(dashboard.meta.pipeline_status, 'DEGRADED');
    assert.equal(dashboard.meta.stale, false);
    assert.equal(dashboard.meta.prediction.status, 'BLOCKED');
    assert.equal(dashboard.meta.alerts.active_count, 888);
    assert.equal(dashboard.meta.alerts.realtime_count, 888);
    assert.equal(dashboard.meta.alerts.forecast_count, 0);
    assert.equal(dashboard.meta.districts.length, 29);
    assert.ok(dashboard.meta.identity.unmapped_assigned_station_count > 0);
    assert.equal(dashboard.stations.length, 1532);
    assert.equal(dashboard.stations.filter(station => station.forecast30 !== null || station.forecast60 !== null).length, 0);
    assert.ok(dashboard.stations.every(station => station.station_code && station.name && Number.isInteger(station.bikes)));

    const apiStations = await fetch(base + '/api/stations').then(response => response.json());
    assert.equal(apiStations.stations.length, 1532);
    assert.equal(apiStations.meta.prediction.status, 'BLOCKED');
    // Legacy district + station-name dispatch is rejected under active integration;
    // station identity is now station_code-based.
    const legacyResponse = await fetch(base + '/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: 'admin01', district: '板橋區', worker: '王小明',
        origin: '新北市政府', destination: '板橋車站（站前路）', suggested: 1
      })
    });
    assert.equal(legacyResponse.status, 409);

    // Round 3 manual dispatch: admin explicitly picks the responsible staff_id
    // (from the district's responsibility zones) plus origin/destination by
    // station_code. The server honours the chosen staff_id after validating it is
    // an in-service zone owner in the origin's district. W001 / 王小明 owns
    // 新莊區第1責任區, which contains 500229002 (新莊棒球場).
    const originCode = '500229002';
    const destinationCode = '500201001';
    const codeResponse = await fetch(base + '/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin01', staffId: 'W001', originCode, destinationCode, suggested: 1 })
    });
    assert.equal(codeResponse.status, 201, 'station_code dispatch should succeed');
    const created = await codeResponse.json();
    assert.equal(created.worker, '王小明', 'chosen staff_id W001 must map to 王小明');
    assert.equal(created.staff_id, 'W001', 'created task must record staff_id W001');
    assert.equal(created.origin_code, originCode);
    assert.equal(created.destination_code, destinationCode);
    assert.ok(Number.isInteger(created.id), 'created task must have an id');

    // A staff_id that is NOT a responsibility-zone owner in the origin district is rejected.
    const wrongStaff = await fetch(base + '/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin01', staffId: 'W012', originCode, destinationCode, suggested: 1 })
    });
    assert.equal(wrongStaff.status, 400, 'staff_id not owning a zone in the district must be rejected');

    // Clean up the task we just created so the business-data hash is restored.
    // This test verifies the runtime contract without leaving persisted rows.
    const writable = new DatabaseSync(path.join(root, 'dispatch-demo.sqlite'));
    try {
      writable.prepare('DELETE FROM task_destinations WHERE task_id = ?').run(created.id);
      writable.prepare('DELETE FROM tasks WHERE id = ?').run(created.id);
    } finally {
      writable.close();
    }

    assert.equal(businessHash(), before, 'Integration startup changed SQLite business data');
    console.log('PASS: Python->Node integration smoke; 1532 stations, 29 districts, 888 realtime alerts, BLOCKED prediction; station_code dispatch -> 王小明/W001');
  } finally {
    if (child.exitCode === null) { const ended = once(child, 'exit'); child.kill(); await ended; }
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
