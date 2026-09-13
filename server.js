const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { DatabaseSync } = require('node:sqlite');

const port = process.env.PORT || 3000;
// Only these browser assets are downloadable; never expose the repository.
const staticFiles = new Map([
  ['/', 'login.html'],
  ...['login.html', 'admin.html', 'staff.html', 'login.js', 'admin.js', 'staff.js',
    'styles.css', 'enhancements.css', 'youbike-logo.png'].map(file => ['/' + file, file]),
  ['/assets/youbike-logo.png', 'youbike-logo.png'],
]);
const db = new DatabaseSync(path.join(__dirname, 'dispatch-demo.sqlite'));
const SYSTEM_CONFIG = { safeMin: 0.3, safeMax: 0.7, targetRatio: 0.5, defaultVehicleCapacity: 14, realtimeRefreshMinutes: 10, forecastRefreshMinutes: 30, taskSlaMinutes: 120 };

db.exec(`
  CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, districts TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS stations (id INTEGER PRIMARY KEY, district TEXT NOT NULL, name TEXT NOT NULL, bikes INTEGER NOT NULL, slots INTEGER NOT NULL, forecast30 INTEGER NOT NULL, forecast60 INTEGER NOT NULL, status TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, district TEXT NOT NULL, worker TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, suggested INTEGER NOT NULL, actual INTEGER, status TEXT NOT NULL, note TEXT);
  CREATE TABLE IF NOT EXISTS district_assignments (district TEXT PRIMARY KEY, zone TEXT NOT NULL, worker TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS staff_members (staff_id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, role TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '未設定', status TEXT NOT NULL DEFAULT '在職');
  CREATE TABLE IF NOT EXISTS task_destinations (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, station_name TEXT NOT NULL, planned_quantity INTEGER NOT NULL, actual_quantity INTEGER, stop_order INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS worker_station_assignments (staff_id TEXT NOT NULL, station_id INTEGER NOT NULL, zone_label TEXT NOT NULL, PRIMARY KEY (staff_id, station_id));
`);
db.exec(`
  CREATE TABLE IF NOT EXISTS integration_pipeline_snapshots (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, pipeline_status TEXT NOT NULL, stale INTEGER NOT NULL, generated_at TEXT, source_snapshot TEXT, prediction_status TEXT NOT NULL, alert_count INTEGER NOT NULL, updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS integration_station_state (official_station_id TEXT PRIMARY KEY, station_name TEXT NOT NULL, district TEXT NOT NULL, available_bikes INTEGER NOT NULL, total_slots INTEGER NOT NULL, available_docks INTEGER, latitude REAL, longitude REAL, observed_at TEXT, status TEXT NOT NULL, alert_severity TEXT, alert_types TEXT, payload TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS integration_alerts (alert_id TEXT PRIMARY KEY, official_station_id TEXT NOT NULL, station_name TEXT NOT NULL, alert_type TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, current_bikes INTEGER, current_docks INTEGER, total_slots INTEGER, triggered_by TEXT NOT NULL, forecast_horizons TEXT NOT NULL, reason_codes TEXT NOT NULL, payload TEXT NOT NULL);
`);
function ensureColumn(table, name, definition) {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all().map(column => column.name);
  if (!columns.includes(name)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${name} ${definition}`);
}
[
  ['station_code', 'TEXT'], ['address', 'TEXT'], ['observed_at', 'TEXT'], ['available_docks', 'INTEGER'], ['active', 'INTEGER DEFAULT 1'], ['data_source', 'TEXT DEFAULT \'DEMO_MOCK\'']
].forEach(([name, definition]) => ensureColumn('stations', name, definition));
[
  ['task_code', 'TEXT'], ['priority', 'TEXT DEFAULT \'HIGH\''], ['light_status', 'TEXT DEFAULT \'ORANGE\''], ['vehicle_capacity', 'INTEGER DEFAULT 14'],
  ['published_at', 'TEXT'], ['accepted_at', 'TEXT'], ['picked_up_at', 'TEXT'], ['completed_at', 'TEXT'], ['reminder_count', 'INTEGER DEFAULT 0'],
  ['faulty_bike_count', 'INTEGER DEFAULT 0'], ['fault_note', 'TEXT'], ['deferred_at', 'TEXT']
].forEach(([name, definition]) => ensureColumn('tasks', name, definition));
// Seed only a completely empty demo database. Partial/existing databases are
// never repaired, reseeded, or recalculated as a side effect of startup.
const businessTables = ['users', 'stations', 'tasks', 'district_assignments',
  'staff_members', 'task_destinations', 'worker_station_assignments'];
db.exec('BEGIN IMMEDIATE');
try {
  const emptyDatabase = businessTables.every(table =>
    db.prepare('SELECT COUNT(*) AS count FROM ' + table).get().count === 0);
  if (emptyDatabase) {
    db.prepare("UPDATE tasks SET status = CASE status WHEN '待處理' THEN 'PUBLISHED' WHEN '待接收' THEN 'PUBLISHED' WHEN '進行中' THEN 'ACCEPTED' WHEN '已完成' THEN 'COMPLETED' ELSE status END").run();
    db.prepare("UPDATE tasks SET task_code = CASE district WHEN '板橋區' THEN 'BQ-' WHEN '新莊區' THEN 'XZ-' WHEN '新店區' THEN 'XD-' WHEN '三重區' THEN 'SC-' ELSE 'NT-' END || printf('%03d', id - 100) WHERE task_code IS NULL").run();
    db.prepare("UPDATE tasks SET published_at = datetime('now','-30 minutes') WHERE published_at IS NULL").run();
    db.prepare("UPDATE tasks SET accepted_at = datetime('now','-20 minutes') WHERE status IN ('ACCEPTED','PICKED_UP','COMPLETED') AND accepted_at IS NULL").run();
    db.prepare("UPDATE tasks SET picked_up_at = datetime('now','-10 minutes') WHERE status IN ('PICKED_UP','COMPLETED') AND picked_up_at IS NULL").run();
    db.prepare("UPDATE tasks SET completed_at = datetime('now') WHERE status = 'COMPLETED' AND completed_at IS NULL").run();

    if (db.prepare('SELECT COUNT(*) AS count FROM users').get().count === 0) {
      db.exec(`
        INSERT INTO users VALUES ('worker01', 'demo1234', '王小明', 'worker', '板橋區、新莊區');
        INSERT INTO users VALUES ('admin01', 'demo1234', '系統管理員', 'admin', '全部行政區');
        INSERT INTO stations (id,district,name,bikes,slots,forecast30,forecast60,status,lat,lng) VALUES
          (1, '板橋區', '板橋車站（站前路）', 2, 48, 1, 0, 'shortage', 25.0142, 121.4637),
          (2, '板橋區', '板橋第一運動場', 37, 40, 39, 40, 'full', 25.0098, 121.4660),
          (3, '新莊區', '新莊體育館', 38, 40, 39, 40, 'full', 25.0414, 121.4478),
          (4, '新店區', '捷運大坪林站', 4, 58, 2, 1, 'shortage', 24.9827, 121.5412),
          (5, '淡水區', '淡水老街廣場', 31, 50, 28, 25, 'safe', 25.1680, 121.4435),
          (6, '三重區', '捷運三重站', 43, 46, 45, 46, 'full', 25.0559, 121.4842);
        INSERT INTO tasks (id, district, worker, origin, destination, suggested, actual, status, note) VALUES
          (101, '板橋區', '王小明', '板橋第一運動場', '板橋車站（站前路）', 8, NULL, 'PUBLISHED', NULL),
          (102, '新莊區', '王小明', '新莊體育館', '新莊區公所', 10, NULL, 'ACCEPTED', NULL),
          (103, '新店區', '陳怡君', '新店區公所', '捷運大坪林站', 6, 5, 'COMPLETED', '尖峰車流影響，實際完成 5 台。'),
          (104, '三重區', '林志豪', '捷運三重站', '三重國民運動中心', 7, 7, 'COMPLETED', '依建議數量完成。');
      `);
    }

    const districtAssignments = [
      ['板橋區', '板橋一區', '王小明'], ['三重區', '三重一區', '林志豪'], ['中和區', '中和一區', '張家豪'],
      ['永和區', '永和區', '張家豪'], ['新莊區', '新莊一區', '王小明'], ['新店區', '新店一區', '陳怡君'],
      ['土城區', '土城區', '李佳穎'], ['蘆洲區', '蘆洲區', '林志豪'], ['樹林區', '樹林區', '李佳穎'],
      ['汐止區', '汐止一區', '周俊宇'], ['鶯歌區', '鶯歌區', '黃子庭'], ['三峽區', '三峽區', '黃子庭'],
      ['淡水區', '淡水一區', '許雅雯'], ['瑞芳區', '瑞芳區', '吳承翰'], ['五股區', '五股區', '趙怡安'],
      ['泰山區', '泰山區', '趙怡安'], ['林口區', '林口區', '蔡宗翰'], ['深坑區', '深坑區', '周俊宇'],
      ['石碇區', '石碇區', '周俊宇'], ['坪林區', '坪林區', '陳怡君'], ['三芝區', '三芝區', '許雅雯'],
      ['石門區', '石門區', '許雅雯'], ['八里區', '八里區', '趙怡安'], ['平溪區', '平溪區', '吳承翰'],
      ['雙溪區', '雙溪區', '吳承翰'], ['貢寮區', '貢寮區', '吳承翰'], ['金山區', '金山區', '蔡宗翰'],
      ['萬里區', '萬里區', '蔡宗翰'], ['烏來區', '烏來區', '陳怡君']
    ];
    const addAssignment = db.prepare('INSERT OR IGNORE INTO district_assignments (district, zone, worker) VALUES (?, ?, ?)');
    districtAssignments.forEach(row => addAssignment.run(...row));
    const staffNames = [...new Set(districtAssignments.map(row => row[2]))];
    const addStaff = db.prepare('INSERT OR IGNORE INTO staff_members (staff_id, name, role, phone, status) VALUES (?, ?, ?, ?, ?)');
    staffNames.forEach((name, index) => addStaff.run(`W${String(index + 1).padStart(3, '0')}`, name, '調度管理人員', '未設定', '在職'));
    addStaff.run('W012', '李冠宇', '調度管理人員', '未設定', '在職');
    db.prepare("UPDATE staff_members SET phone = '09' || printf('%08d', 10000000 + CAST(SUBSTR(staff_id,2) AS INTEGER) * 137) WHERE phone = '未設定'").run();
    db.prepare("UPDATE stations SET bikes = 37, forecast30 = 39, forecast60 = 40, status = 'full' WHERE id = 2 AND name = '板橋第一運動場'").run();
    db.exec(`
      INSERT OR IGNORE INTO stations (id,district,name,bikes,slots,forecast30,forecast60,status,lat,lng) VALUES
        (7, '板橋區', '新北市政府', 44, 50, 46, 48, 'full', 25.0123, 121.4652),
        (8, '板橋區', '捷運江子翠站', 5, 48, 3, 1, 'shortage', 25.0301, 121.4724),
        (9, '新莊區', '捷運輔大站', 6, 60, 4, 2, 'shortage', 25.0328, 121.4352),
        (10, '新莊區', '新莊區公所', 3, 40, 2, 1, 'shortage', 25.0361, 121.4529),
        (11, '新莊區', '新莊副都心廣場', 51, 60, 54, 57, 'full', 25.0591, 121.4462),
        (12, '板橋區', '板橋國民運動中心', 22, 48, 21, 19, 'safe', 25.0223, 121.4587),
        (13, '新莊區', '捷運新莊站', 28, 52, 26, 24, 'safe', 25.0360, 121.4521);
      INSERT OR IGNORE INTO tasks (id, district, worker, origin, destination, suggested, actual, status, note) VALUES
        (105, '板橋區', '王小明', '新北市政府', '捷運江子翠站', 7, NULL, 'PUBLISHED', NULL),
        (106, '新莊區', '王小明', '新莊副都心廣場', '捷運輔大站', 9, NULL, 'PUBLISHED', NULL),
        (107, '板橋區', '王小明', '板橋第一運動場', '捷運江子翠站', 5, 5, 'COMPLETED', '依建議數量完成。'),
        (108, '新莊區', '王小明', '新莊體育館', '捷運輔大站', 6, NULL, 'PUBLISHED', NULL),
        (109, '板橋區', '王小明', '板橋第一運動場', '板橋車站（站前路）', 14, NULL, 'PUBLISHED', NULL);
      INSERT OR IGNORE INTO task_destinations (id, task_id, station_name, planned_quantity, actual_quantity, stop_order) VALUES
        (1, 109, '板橋車站（站前路）', 8, NULL, 1),
        (2, 109, '捷運江子翠站', 6, NULL, 2);
    `);
    db.prepare("UPDATE tasks SET task_code = CASE district WHEN '板橋區' THEN 'BQ-' WHEN '新莊區' THEN 'XZ-' WHEN '新店區' THEN 'XD-' WHEN '三重區' THEN 'SC-' ELSE 'NT-' END || printf('%03d', id - 100) WHERE task_code IS NULL").run();
    db.prepare("UPDATE tasks SET published_at = datetime('now','-5 minutes') WHERE published_at IS NULL").run();
    db.prepare("UPDATE tasks SET accepted_at = datetime('now','-4 minutes') WHERE status IN ('ACCEPTED','PICKED_UP','COMPLETED') AND accepted_at IS NULL").run();
    db.prepare("UPDATE tasks SET picked_up_at = datetime('now','-2 minutes') WHERE status IN ('PICKED_UP','COMPLETED') AND picked_up_at IS NULL").run();
    db.prepare("UPDATE tasks SET completed_at = datetime('now') WHERE status = 'COMPLETED' AND completed_at IS NULL").run();
    db.prepare(`UPDATE tasks SET suggested = COALESCE((
      SELECT MIN(14, MAX(1, s.bikes - CAST((s.slots * 3 + 9) / 10 AS INTEGER)))
      FROM stations s WHERE s.name = tasks.origin AND s.district = tasks.district
    ), suggested), vehicle_capacity = 14`).run();
    const assignStation = db.prepare('INSERT OR IGNORE INTO worker_station_assignments (staff_id, station_id, zone_label) VALUES (?, ?, ?)');
    db.prepare('DELETE FROM worker_station_assignments').run();
    db.prepare("DELETE FROM staff_members WHERE staff_id LIKE 'NT%' OR staff_id LIKE 'RG%' OR staff_id LIKE 'CR%'").run();
    const demoSurnames = ['陳', '林', '黃', '張', '李', '王', '吳', '劉', '蔡', '楊', '許', '鄭', '謝', '洪', '郭', '邱', '曾', '廖', '賴', '周'];
    const demoGivenNames = ['冠宇', '承恩', '柏翰', '俊傑', '家豪', '宗翰', '彥廷', '子軒', '品睿', '宇翔', '雅婷', '怡君', '佳穎', '欣怡', '郁晴', '詩涵', '佩珊', '婉婷', '思妤', '庭瑜'];
    const usedStaffNames = new Set(db.prepare('SELECT name FROM staff_members').all().map(item => item.name));
    let demoNameNumber = 0;
    function nextDemoStaffName() {
      while (demoNameNumber < demoSurnames.length * demoGivenNames.length) {
        const index = demoNameNumber++;
        const name = demoSurnames[Math.floor(index / demoGivenNames.length)] + demoGivenNames[index % demoGivenNames.length];
        if (!usedStaffNames.has(name)) { usedStaffNames.add(name); return name; }
      }
      throw new Error('示範工作人員姓名組合不足');
    }
    const legacyByDistrict = new Map(districtAssignments.map(([district, , name]) => [district, name]));
    const usedLegacyNames = new Set(['王小明', '李冠宇']);
    let generatedStaffNumber = 1;
    const pooledSmallDistrictStations = [];
    for (const districtRow of db.prepare('SELECT DISTINCT district FROM stations WHERE active = 1 ORDER BY district').all()) {
      const district = districtRow.district;
      const remaining = db.prepare('SELECT id,name,lat,lng FROM stations WHERE active = 1 AND district = ? ORDER BY id').all(district);
      if (remaining.length < 8) { pooledSmallDistrictStations.push(...remaining.map(station => ({ ...station, district }))); continue; }
      const minimumGroups = Math.ceil(remaining.length / 10);
      const maximumGroups = Math.floor(remaining.length / 8);
      const canPartitionWholeDistrict = minimumGroups <= maximumGroups;
      const groupCount = canPartitionWholeDistrict ? minimumGroups : Math.floor(remaining.length / 10);
      const baseSize = canPartitionWholeDistrict ? Math.floor(remaining.length / groupCount) : 10;
      const largerGroups = canPartitionWholeDistrict ? remaining.length % groupCount : 0;
      for (let groupIndex = 0; groupIndex < groupCount; groupIndex += 1) {
        const groupSize = baseSize + (groupIndex < largerGroups ? 1 : 0);
        const seed = remaining[0];
        const cluster = remaining.sort((a, b) => {
          const distance = station => ((station.lat - seed.lat) ** 2) + (((station.lng - seed.lng) * .91) ** 2);
          return distance(a) - distance(b);
        }).slice(0, groupSize);
        cluster.forEach(station => remaining.splice(remaining.findIndex(item => item.id === station.id), 1));
        let staffId;
        let staffName;
        if (district === '新莊區' && groupIndex === 0) { staffId = 'W001'; staffName = '王小明'; }
        else if (district === '板橋區' && groupIndex === 0) { staffId = 'W012'; staffName = '李冠宇'; }
        else {
          const legacyName = groupIndex === 0 ? legacyByDistrict.get(district) : null;
          if (legacyName && !usedLegacyNames.has(legacyName)) {
            const legacy = db.prepare('SELECT staff_id,name FROM staff_members WHERE name=?').get(legacyName);
            staffId = legacy.staff_id; staffName = legacy.name; usedLegacyNames.add(legacyName);
          } else {
            staffId = `RG${String(generatedStaffNumber).padStart(4, '0')}`;
            staffName = nextDemoStaffName();
            generatedStaffNumber += 1;
            addStaff.run(staffId, staffName, '調度管理人員', `09${String(20000000 + generatedStaffNumber * 137).slice(-8)}`, '在職');
          }
        }
        const zoneLabel = `${district}第${groupIndex + 1}責任區`;
        cluster.forEach(station => assignStation.run(staffId, station.id, zoneLabel));
      }
      pooledSmallDistrictStations.push(...remaining.map(station => ({ ...station, district })));
    }
    const pooledGroupCount = Math.max(1, Math.ceil(pooledSmallDistrictStations.length / 10));
    const pooledBaseSize = Math.floor(pooledSmallDistrictStations.length / pooledGroupCount);
    const pooledLargerGroups = pooledSmallDistrictStations.length % pooledGroupCount;
    for (let groupIndex = 0; groupIndex < pooledGroupCount; groupIndex += 1) {
      const seed = pooledSmallDistrictStations[0];
      const groupSize = pooledBaseSize + (groupIndex < pooledLargerGroups ? 1 : 0);
      const cluster = pooledSmallDistrictStations.sort((a, b) => {
        const distance = station => ((station.lat - seed.lat) ** 2) + (((station.lng - seed.lng) * .91) ** 2);
        return distance(a) - distance(b);
      }).slice(0, groupSize);
      cluster.forEach(station => pooledSmallDistrictStations.splice(pooledSmallDistrictStations.findIndex(item => item.id === station.id), 1));
      const staffId = `CR${String(groupIndex + 1).padStart(3, '0')}`;
      const staffName = nextDemoStaffName();
      generatedStaffNumber += 1;
      addStaff.run(staffId, staffName, '調度管理人員', `09${String(20000000 + generatedStaffNumber * 137).slice(-8)}`, '在職');
      cluster.forEach(station => assignStation.run(staffId, station.id, `跨區第${groupIndex + 1}責任區`));
    }
  }
  db.exec('COMMIT');
} catch (error) {
  db.exec('ROLLBACK');
  throw error;
}

function withDestinations(tasks) {
  const getStops = db.prepare('SELECT station_name, planned_quantity, actual_quantity, stop_order FROM task_destinations WHERE task_id = ? ORDER BY stop_order');
  return tasks.map(task => ({ ...task, destinations: getStops.all(task.id) }));
}

function sendJson(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(data));
}

function splitPathList(value) {
  return String(value || '').split(path.delimiter).map(item => item.trim()).filter(Boolean);
}

function integrationArgs() {
  const realtimeCsv = splitPathList(process.env.YOUBIKE_REALTIME_CSV);
  if (process.env.YOUBIKE_REALTIME_CSV_1) realtimeCsv.push(process.env.YOUBIKE_REALTIME_CSV_1);
  if (process.env.YOUBIKE_REALTIME_CSV_2) realtimeCsv.push(process.env.YOUBIKE_REALTIME_CSV_2);
  if (!realtimeCsv.length) return null;
  const args = ['-m', 'backend.integration_runner', '--realtime-csv', ...realtimeCsv];
  for (const [envName, option] of [
    ['YOUBIKE_HISTORY_SOURCE', '--history-source'],
    ['YOUBIKE_MODEL_PACK', '--model-pack'],
    ['YOUBIKE_HOLIDAY_CALENDAR', '--holiday-calendar'],
    ['YOUBIKE_COORDINATE_SOURCE', '--coordinate-source'],
  ]) {
    if (process.env[envName]) args.push(option, process.env[envName]);
  }
  return args;
}

function pythonCommand(args) {
  const executable = process.env.YOUBIKE_PYTHON || 'py';
  const version = process.env.YOUBIKE_PYTHON_VERSION || '-3.12';
  return { executable, args: executable === 'py' && version ? [version, ...args] : args };
}

function runPythonIntegration() {
  const args = integrationArgs();
  if (!args) return null;
  const command = pythonCommand(args);
  const result = spawnSync(command.executable, command.args, {
    cwd: __dirname,
    env: process.env,
    encoding: 'utf8',
    windowsHide: true,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  const stdout = String(result.stdout || '').trim();
  if (!stdout) throw new Error(String(result.stderr || 'Python integration produced no JSON'));
  const payload = JSON.parse(stdout);
  if (!payload || !Array.isArray(payload.stations)) throw new Error('Python integration JSON missing stations');
  return payload;
}

function stationStatus(station, alerts) {
  const active = alerts.filter(alert => alert.official_station_id === station.official_station_id && alert.status === 'active');
  if (active.some(alert => alert.alert_type === 'low_bikes')) return 'shortage';
  if (active.some(alert => alert.alert_type === 'low_docks')) return 'full';
  return 'safe';
}

function cacheIntegrationSnapshot(payload) {
  const alerts = payload.alerts?.active_alerts || [];
  const insertStation = db.prepare(`INSERT OR REPLACE INTO integration_station_state
    (official_station_id, station_name, district, available_bikes, total_slots, available_docks, latitude, longitude, observed_at, status, alert_severity, alert_types, payload)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  const insertAlert = db.prepare(`INSERT OR REPLACE INTO integration_alerts
    (alert_id, official_station_id, station_name, alert_type, severity, status, current_bikes, current_docks, total_slots, triggered_by, forecast_horizons, reason_codes, payload)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM integration_station_state').run();
    db.prepare('DELETE FROM integration_alerts').run();
    for (const station of payload.stations) {
      const stationAlerts = alerts.filter(alert => alert.official_station_id === station.official_station_id);
      insertStation.run(station.official_station_id, station.station_name, station.district,
        station.available_bikes, station.total_slots, station.available_docks ?? null,
        station.latitude ?? null, station.longitude ?? null, station.observed_at ?? null,
        stationStatus(station, alerts), stationAlerts[0]?.severity || null,
        JSON.stringify([...new Set(stationAlerts.map(alert => alert.alert_type))]),
        JSON.stringify(station));
    }
    for (const alert of alerts) {
      insertAlert.run(alert.alert_id, alert.official_station_id, alert.station_name, alert.alert_type,
        alert.severity, alert.status, alert.current_bikes ?? null, alert.current_docks ?? null,
        alert.total_slots ?? null, JSON.stringify(alert.triggered_by || []),
        JSON.stringify(alert.forecast_horizons || []), JSON.stringify(alert.reason_codes || []),
        JSON.stringify(alert));
    }
    db.prepare(`INSERT OR REPLACE INTO integration_pipeline_snapshots
      (id, payload, pipeline_status, stale, generated_at, source_snapshot, prediction_status, alert_count, updated_at)
      VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))`).run(
      JSON.stringify(payload), String(payload.pipeline_status || 'unavailable').toUpperCase(),
      payload.stale ? 1 : 0, payload.generated_at || null, JSON.stringify(payload.source_snapshot || null),
      String(payload.prediction?.status || 'unavailable').toUpperCase(), alerts.length);
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

function loadIntegrationSnapshot() {
  const row = db.prepare('SELECT payload FROM integration_pipeline_snapshots WHERE id = 1').get();
  return row ? JSON.parse(row.payload) : null;
}

function loadIntegrationStations(where = '', values = []) {
  return db.prepare(`SELECT official_station_id AS station_code, station_name AS name, district,
      available_bikes AS bikes, total_slots AS slots, available_docks, latitude AS lat, longitude AS lng,
      observed_at, status, alert_severity, alert_types, NULL AS forecast30, NULL AS forecast60,
      'PYTHON_PIPELINE' AS data_source, 1 AS active
    FROM integration_station_state ${where} ORDER BY district, name`).all(...values);
}

function integrationMeta(stations) {
  const snapshot = loadIntegrationSnapshot();
  if (!snapshot) return { lastUpdated: new Date().toISOString(), stationCount: stations.length, source: 'DEMO_MOCK', config: SYSTEM_CONFIG };
  const activeAlerts = snapshot.alerts?.active_alerts || [];
  const districts = [...new Set(stations.map(station => station.district).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-Hant'));
  const totalAssigned = db.prepare(`SELECT COUNT(*) AS count FROM stations s
    JOIN worker_station_assignments wsa ON wsa.station_id = s.id
    WHERE s.station_code IS NOT NULL`).get().count;
  const mappedAssigned = db.prepare(`SELECT COUNT(*) AS count FROM stations s
    JOIN worker_station_assignments wsa ON wsa.station_id = s.id
    JOIN integration_station_state iss ON iss.official_station_id = s.station_code
    WHERE s.station_code IS NOT NULL`).get().count;
  return {
    lastUpdated: snapshot.generated_at,
    stationCount: stations.length,
    districts,
    source: 'PYTHON_PIPELINE',
    pipeline_status: String(snapshot.pipeline_status || 'unavailable').toUpperCase(),
    stale: Boolean(snapshot.stale),
    generated_at: snapshot.generated_at,
    source_snapshot: snapshot.source_snapshot || null,
    prediction: { status: String(snapshot.prediction?.status || 'unavailable').toUpperCase() },
    alerts: {
      active_count: activeAlerts.length,
      realtime_count: activeAlerts.filter(alert => (alert.triggered_by || []).includes('realtime')).length,
      forecast_count: activeAlerts.filter(alert => (alert.forecast_horizons || []).length).length,
      summary: snapshot.alerts?.summary || {},
    },
    identity: {
      canonical: 'station_code=official_station_id',
      assigned_station_count: totalAssigned,
      mapped_assigned_station_count: mappedAssigned,
      unmapped_assigned_station_count: Math.max(0, totalAssigned - mappedAssigned),
    },
    config: SYSTEM_CONFIG,
  };
}

// Round 3 contract: station_code -> responsibility zone -> worker.
// Zone labels look like "新莊區第1責任區"; extract the numeric order so the UI can
// sort 第1、第2 ... 第10 numerically rather than lexicographically.
function extractZoneOrder(zoneLabel) {
  const match = String(zoneLabel || '').match(/第\s*(\d+)/);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}

// Given a canonical station_code, resolve the responsibility zone and its
// assigned worker via worker_station_assignments -> staff_members. Returns null
// when the station is not part of any responsibility zone.
function resolveAssignmentByStationCode(stationCode) {
  if (!stationCode) return null;
  return db.prepare(`SELECT s.station_code AS stationCode, s.name AS stationName, s.district,
      wsa.zone_label AS zone, m.staff_id AS staffId, m.name AS worker, m.status AS staffStatus
    FROM stations s
    JOIN worker_station_assignments wsa ON wsa.station_id = s.id
    JOIN staff_members m ON m.staff_id = wsa.staff_id
    WHERE s.station_code = ?
    LIMIT 1`).get(String(stationCode)) || null;
}

function enrichAssignments(assignments, stations) {
  const byDistrict = new Map();
  for (const station of stations) {
    const item = byDistrict.get(station.district) || { stationCount: 0, shortageCount: 0, fullCount: 0 };
    item.stationCount += 1;
    if (station.status === 'shortage') item.shortageCount += 1;
    if (station.status === 'full') item.fullCount += 1;
    byDistrict.set(station.district, item);
  }
  return assignments.map(assignment => ({ ...assignment, ...(byDistrict.get(assignment.district) || {}) }));
}

try {
  const integration = runPythonIntegration();
  if (integration) cacheIntegrationSnapshot(integration);
} catch (error) {
  console.error('Python integration unavailable:', error.message);
}

function serveFile(res, filePath) {
  const ext = path.extname(filePath);
  const types = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.png': 'image/png' };
  fs.readFile(filePath, (error, data) => {
    if (error) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
    res.end(data);
  });
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => resolve(body ? JSON.parse(body) : {}));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (req.method === 'POST' && url.pathname === '/api/login') {
    const { username, password } = await readBody(req);
    const user = db.prepare('SELECT username, name, role, districts FROM users WHERE username = ? AND password = ?').get(username, password);
    return user ? sendJson(res, 200, user) : sendJson(res, 401, { message: '帳號或密碼不正確' });
  }
  if (req.method === 'GET' && url.pathname === '/api/staff-dashboard') {
    const username = url.searchParams.get('username');
    const user = db.prepare("SELECT username, name FROM users WHERE username = ? AND role = 'worker'").get(username);
    if (!user) return sendJson(res, 403, { message: '沒有工作人員權限' });
    const assignments = db.prepare(`SELECT DISTINCT s.district, wsa.zone_label AS zone FROM worker_station_assignments wsa
      JOIN stations s ON s.id=wsa.station_id JOIN staff_members m ON m.staff_id=wsa.staff_id
      WHERE m.name=? ORDER BY s.district,wsa.zone_label`).all(user.name);
    const districts = assignments.map(item => item.district);
    if (districts.length === 0) return sendJson(res, 200, { assignments, stations: [], tasks: [] });
    const placeholders = districts.map(() => '?').join(',');
    const assignedCodes = db.prepare(`SELECT s.station_code FROM stations s JOIN worker_station_assignments wsa ON wsa.station_id = s.id JOIN staff_members m ON m.staff_id = wsa.staff_id WHERE m.name = ? AND s.station_code IS NOT NULL`).all(user.name).map(item => item.station_code);
    const integratedStations = assignedCodes.length
      ? loadIntegrationStations(`WHERE official_station_id IN (${assignedCodes.map(() => '?').join(',')})`, assignedCodes)
      : [];
    const stations = integratedStations.length ? integratedStations : db.prepare(`SELECT s.*, wsa.zone_label FROM stations s JOIN worker_station_assignments wsa ON wsa.station_id = s.id JOIN staff_members m ON m.staff_id = wsa.staff_id WHERE m.name = ? AND s.active = 1 ORDER BY CASE s.status WHEN 'shortage' THEN 1 WHEN 'full' THEN 2 ELSE 3 END, s.district, s.name`).all(user.name);
    const tasks = withDestinations(db.prepare(`SELECT t.* FROM tasks t WHERE t.worker = ? AND t.district IN (${placeholders})
      AND EXISTS (SELECT 1 FROM worker_station_assignments wsa JOIN staff_members m ON m.staff_id=wsa.staff_id
        JOIN stations s ON s.id=wsa.station_id WHERE m.name=? AND (s.name=t.origin OR s.name=t.destination))
      ORDER BY CASE t.status WHEN 'PUBLISHED' THEN 1 WHEN 'ACCEPTED' THEN 2 WHEN 'PICKED_UP' THEN 3 ELSE 4 END, t.id`).all(user.name, ...districts, user.name));
    return sendJson(res, 200, { assignments: enrichAssignments(assignments, stations), stations, tasks, meta: integrationMeta(stations) });
  }
  if (req.method === 'GET' && url.pathname === '/api/stations') {
    const district = url.searchParams.get('district');
    let stations = district ? loadIntegrationStations('WHERE district = ?', [district]) : loadIntegrationStations();
    if (!stations.length) {
      stations = district ? db.prepare('SELECT * FROM stations WHERE district = ? AND active = 1 ORDER BY name').all(district) : db.prepare('SELECT * FROM stations WHERE active = 1 ORDER BY district, name').all();
    }
    return sendJson(res, 200, { stations, stationCount: stations.length, lastUpdated: stations.map(item => item.observed_at).filter(Boolean).sort().at(-1) || null, meta: integrationMeta(stations) });
  }
  const acceptMatch = url.pathname.match(/^\/api\/tasks\/(\d+)\/accept$/);
  if (req.method === 'POST' && acceptMatch) {
    const { username } = await readBody(req);
    const user = db.prepare("SELECT name FROM users WHERE username = ? AND role = 'worker'").get(username);
    const task = user ? db.prepare("SELECT id FROM tasks WHERE id = ? AND worker = ? AND status = 'PUBLISHED'").get(Number(acceptMatch[1]), user.name) : null;
    if (!task) return sendJson(res, 403, { message: '此任務無法接收或不屬於你' });
    db.prepare("UPDATE tasks SET status = 'ACCEPTED', accepted_at = datetime('now') WHERE id = ? AND status = 'PUBLISHED'").run(task.id);
    return sendJson(res, 200, { message: '任務已接收' });
  }
  const pickupMatch = url.pathname.match(/^\/api\/tasks\/(\d+)\/pickup$/);
  if (req.method === 'POST' && pickupMatch) {
    const { username } = await readBody(req);
    const user = db.prepare("SELECT name FROM users WHERE username = ? AND role = 'worker'").get(username);
    const result = user ? db.prepare("UPDATE tasks SET status = 'PICKED_UP', picked_up_at = datetime('now') WHERE id = ? AND worker = ? AND status = 'ACCEPTED'").run(Number(pickupMatch[1]), user.name) : { changes: 0 };
    return result.changes ? sendJson(res, 200, { message: '已確認取車，開始配送' }) : sendJson(res, 409, { message: '任務狀態不允許取車' });
  }
  const deferMatch = url.pathname.match(/^\/api\/tasks\/(\d+)\/defer$/);
  if (req.method === 'POST' && deferMatch) {
    const { username } = await readBody(req);
    const user = db.prepare("SELECT name FROM users WHERE username = ? AND role = 'worker'").get(username);
    const result = user ? db.prepare("UPDATE tasks SET deferred_at = datetime('now') WHERE id = ? AND worker = ? AND status = 'PUBLISHED'").run(Number(deferMatch[1]), user.name) : { changes: 0 };
    return result.changes ? sendJson(res, 200, { message: '任務已保留在待接收頂端' }) : sendJson(res, 409, { message: '任務無法稍後處理' });
  }
  const remindMatch = url.pathname.match(/^\/api\/tasks\/(\d+)\/remind$/);
  if (req.method === 'POST' && remindMatch) {
    const result = db.prepare("UPDATE tasks SET reminder_count = reminder_count + 1 WHERE id = ? AND status = 'PUBLISHED'").run(Number(remindMatch[1]));
    return result.changes ? sendJson(res, 200, { message: '提醒紀錄已保存' }) : sendJson(res, 409, { message: '任務已接受或不存在' });
  }
  const completeMatch = url.pathname.match(/^\/api\/tasks\/(\d+)\/complete$/);
  if (req.method === 'POST' && completeMatch) {
    const { username, actual, note, faultyBikeCount = 0, faultNote = '' } = await readBody(req);
    const user = db.prepare("SELECT name FROM users WHERE username = ? AND role = 'worker'").get(username);
    const task = user ? db.prepare('SELECT * FROM tasks WHERE id = ? AND worker = ?').get(Number(completeMatch[1]), user.name) : null;
    const actualNumber = Number(actual);
    if (!task) return sendJson(res, 403, { message: '你沒有更新此任務的權限' });
    const faulty = Number(faultyBikeCount);
    if (task.status !== 'PICKED_UP') return sendJson(res, 409, { message: '必須先接受並確認取車才能完成' });
    const originStation = db.prepare('SELECT bikes FROM stations WHERE district = ? AND name = ?').get(task.district, task.origin);
    const destinationNames = db.prepare('SELECT station_name FROM task_destinations WHERE task_id = ? ORDER BY stop_order').all(task.id).map(item => item.station_name);
    if (!destinationNames.length) destinationNames.push(task.destination);
    const getDestination = db.prepare('SELECT bikes, slots FROM stations WHERE district = ? AND name = ?');
    const destinationCapacity = destinationNames.reduce((total, name) => {
      const station = getDestination.get(task.district, name);
      return total + (station ? Math.max(0, station.slots - station.bikes) : 0);
    }, 0);
    const actualLimit = Math.min(originStation?.bikes ?? 0, destinationCapacity);
    if (!Number.isInteger(actualNumber) || actualNumber < 0 || actualNumber > actualLimit) return sendJson(res, 400, { message: `實際調度數量須介於 0～${actualLimit} 台（依起點現有車輛與目的站空位計算）` });
    if (!Number.isInteger(faulty) || faulty < 0) return sendJson(res, 400, { message: '故障車數量格式不正確' });
    db.prepare("UPDATE tasks SET actual = ?, status = 'COMPLETED', note = ?, faulty_bike_count = ?, fault_note = ?, completed_at = datetime('now') WHERE id = ? AND status = 'PICKED_UP'").run(actualNumber, String(note || '依建議數量完成。'), faulty, String(faultNote), task.id);
    return sendJson(res, 200, { message: '任務已完成回報' });
  }
  if (req.method === 'POST' && url.pathname === '/api/tasks') {
    const body = await readBody(req);
    const { username, district, worker, origin, destination, suggested, staffId, originCode, destinationCode } = body;
    const admin = db.prepare("SELECT role FROM users WHERE username = ? AND role = 'admin'").get(username);
    if (!admin) return sendJson(res, 403, { message: '沒有派工權限' });
    const amount = Number(suggested);

    // Round 3 contract (manual admin dispatch by canonical station_code): the admin
    // explicitly picks the responsible staff_id from the district's responsibility
    // zones, plus origin/destination by station_code. We honour the admin's chosen
    // staff_id (never silently re-assign from the origin zone), but validate that
    // the staff_id is an in-service owner of a responsibility zone in the origin's
    // district.
    if (staffId || originCode || destinationCode) {
      if (!staffId) return sendJson(res, 400, { message: '請選擇負責人' });
      if (!originCode || !destinationCode) return sendJson(res, 400, { message: '請提供起點與目的站的 station_code' });
      if (originCode === destinationCode) return sendJson(res, 400, { message: '起點與目的站不可相同' });
      const originState = db.prepare('SELECT station_name, district, available_bikes AS bikes, total_slots AS slots FROM integration_station_state WHERE official_station_id = ?').get(String(originCode));
      const destinationState = db.prepare('SELECT station_name, district, available_bikes AS bikes, total_slots AS slots FROM integration_station_state WHERE official_station_id = ?').get(String(destinationCode));
      if (!originState || !destinationState) return sendJson(res, 400, { message: '找不到對應的即時站點資料' });
      // Validate the chosen staff_id is an in-service responsibility-zone owner in
      // the origin station's district.
      const staffRow = db.prepare("SELECT staff_id, name, status FROM staff_members WHERE staff_id = ?").get(String(staffId));
      if (!staffRow) return sendJson(res, 400, { message: '負責人不存在' });
      if (staffRow.status !== '在職') return sendJson(res, 400, { message: '所選負責人目前不在職' });
      const ownsZoneInDistrict = db.prepare(`SELECT wsa.zone_label AS zone FROM worker_station_assignments wsa
        JOIN stations s ON s.id = wsa.station_id
        WHERE wsa.staff_id = ? AND s.district = ? LIMIT 1`).get(String(staffId), originState.district);
      if (!ownsZoneInDistrict) return sendJson(res, 400, { message: '所選負責人不是該行政區的責任區負責人' });
      const destinationCapacity = Math.max(0, destinationState.slots - destinationState.bikes);
      if (!Number.isInteger(amount) || amount < 1 || amount > destinationCapacity) return sendJson(res, 400, { message: `建議數量須介於 1～${destinationCapacity} 台，不可超過目的站空位` });
      const taskDistrict = originState.district;
      const nextId = db.prepare('SELECT COALESCE(MAX(id), 100) + 1 AS id FROM tasks').get().id;
      const codePrefix = taskDistrict === '板橋區' ? 'BQ' : taskDistrict === '新莊區' ? 'XZ' : 'NT';
      const taskCode = `${codePrefix}-${String(nextId - 100).padStart(3, '0')}`;
      db.prepare("INSERT INTO tasks (id, district, worker, origin, destination, suggested, actual, status, note, task_code, priority, light_status, vehicle_capacity, published_at) VALUES (?, ?, ?, ?, ?, ?, NULL, 'PUBLISHED', NULL, ?, 'HIGH', 'ORANGE', ?, datetime('now'))").run(nextId, taskDistrict, staffRow.name, originState.station_name, destinationState.station_name, amount, taskCode, SYSTEM_CONFIG.defaultVehicleCapacity);
      db.prepare('INSERT INTO task_destinations (task_id, station_name, planned_quantity, stop_order) VALUES (?, ?, ?, 1)').run(nextId, destinationState.station_name, amount);
      return sendJson(res, 201, { id: nextId, task_code: taskCode, worker: staffRow.name, zone: ownsZoneInDistrict.zone, staff_id: staffRow.staff_id, origin_code: String(originCode), destination_code: String(destinationCode), message: '派工完成，工作人員將收到通知' });
    }

    // Legacy district + station-name path. Under active integration this is
    // rejected because station identity is now station_code-based; callers must
    // use the station_code contract above.
    if (loadIntegrationSnapshot()) {
      return sendJson(res, 409, { message: '正式調度需使用 station_code 派工，請改用 station_code 候選任務流程。' });
    }
    const assignment = db.prepare("SELECT name AS worker FROM staff_members WHERE name = ? AND status = '在職'").get(worker);
    const originStation = db.prepare('SELECT id, bikes, slots, status FROM stations WHERE district = ? AND name = ?').get(district, origin);
    const destinationStation = db.prepare('SELECT id, bikes, slots FROM stations WHERE district = ? AND name = ?').get(district, destination);
    if (!assignment || !originStation || !destinationStation || origin === destination) return sendJson(res, 400, { message: '請確認行政區、負責人與起訖站' });
    const destinationCapacity = destinationStation.slots - destinationStation.bikes;
    if (!Number.isInteger(amount) || amount < 1 || amount > destinationCapacity) return sendJson(res, 400, { message: `建議數量須介於 1～${destinationCapacity} 台，不可超過目的站空位` });
    const nextId = db.prepare('SELECT COALESCE(MAX(id), 100) + 1 AS id FROM tasks').get().id;
    const codePrefix = district === '板橋區' ? 'BQ' : district === '新莊區' ? 'XZ' : 'NT';
    const taskCode = `${codePrefix}-${String(nextId - 100).padStart(3, '0')}`;
    db.prepare("INSERT INTO tasks (id, district, worker, origin, destination, suggested, actual, status, note, task_code, priority, light_status, vehicle_capacity, published_at) VALUES (?, ?, ?, ?, ?, ?, NULL, 'PUBLISHED', NULL, ?, 'HIGH', 'ORANGE', ?, datetime('now'))").run(nextId, district, worker, origin, destination, amount, taskCode, SYSTEM_CONFIG.defaultVehicleCapacity);
    db.prepare('INSERT INTO task_destinations (task_id, station_name, planned_quantity, stop_order) VALUES (?, ?, ?, 1)').run(nextId, destination, amount);
    return sendJson(res, 201, { id: nextId, message: '派工完成，工作人員將收到通知' });
  }
  if (req.method === 'GET' && url.pathname === '/api/dashboard') {
    const district = url.searchParams.get('district') || '全部行政區';
    const filter = district === '全部行政區' ? '' : ' WHERE district = ?';
    const values = district === '全部行政區' ? [] : [district];
    let stations = values.length === 0 ? loadIntegrationStations() : loadIntegrationStations('WHERE district = ?', [district]);
    if (!stations.length) {
      stations = values.length === 0 ? db.prepare('SELECT * FROM stations WHERE active = 1').all() : db.prepare('SELECT * FROM stations WHERE district = ? AND active = 1').all(district);
    }
    const tasks = withDestinations(db.prepare(`SELECT * FROM tasks${filter} ORDER BY CASE status WHEN 'PUBLISHED' THEN 1 WHEN 'ACCEPTED' THEN 2 WHEN 'PICKED_UP' THEN 3 ELSE 4 END, id`).all(...values));
    const assignmentFilter = district === '全部行政區' ? '' : ' WHERE d.district = ?';
    const assignments = db.prepare(`
      SELECT REPLACE(GROUP_CONCAT(DISTINCT s.district),',','、') AS district, wsa.zone_label AS zone, m.name AS worker, m.staff_id, m.role, m.phone, m.status AS staffStatus,
        COUNT(*) AS stationCount,
        GROUP_CONCAT(s.station_code) AS stationCodesRaw,
        (SELECT COUNT(*) FROM tasks t WHERE t.worker=m.name AND t.status='PUBLISHED') AS todoCount,
        (SELECT COUNT(*) FROM tasks t WHERE t.worker=m.name AND t.status IN ('ACCEPTED','PICKED_UP')) AS doingCount,
        (SELECT COUNT(*) FROM tasks t WHERE t.worker=m.name AND t.status='COMPLETED') AS doneCount
      FROM worker_station_assignments wsa JOIN stations s ON s.id=wsa.station_id
      JOIN staff_members m ON m.staff_id=wsa.staff_id
      ${district === '全部行政區' ? '' : 'WHERE s.district = ?'}
      GROUP BY wsa.zone_label,m.staff_id,m.name,m.role,m.phone,m.status
      ORDER BY MIN(s.district),wsa.zone_label
    `).all(...values).map(row => {
      const { stationCodesRaw, ...rest } = row;
      const stationCodes = String(stationCodesRaw || '').split(',').map(code => code.trim()).filter(Boolean);
      return { ...rest, zoneOrder: extractZoneOrder(rest.zone), stationCodes };
    }).sort((a, b) => a.district.localeCompare(b.district, 'zh-Hant') || a.zoneOrder - b.zoneOrder || a.zone.localeCompare(b.zone, 'zh-Hant'));
    const staff = db.prepare("SELECT staff_id, name, role, phone, status FROM staff_members m WHERE status='在職' AND EXISTS (SELECT 1 FROM worker_station_assignments w WHERE w.staff_id=m.staff_id) ORDER BY staff_id").all();
    return sendJson(res, 200, { stations, tasks, assignments: enrichAssignments(assignments, stations), staff, meta: integrationMeta(stations) });
  }
  const asset = staticFiles.get(url.pathname);
  if (req.method !== 'GET' || !asset) {
    res.writeHead(404);
    return res.end('Not found');
  }
  serveFile(res, path.join(__dirname, asset));
}).listen(port, () => console.log(`YouBike Demo: http://localhost:${server.address().port}`));
