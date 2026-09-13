const user = JSON.parse(sessionStorage.getItem('dispatchUser') || 'null');
if (!user || user.role !== 'admin') location.href = '/login.html';
document.querySelector('#identity').textContent = `${user.name}｜全行政區管理權限`;
document.querySelector('#signout').onclick = () => { sessionStorage.clear(); location.href = '/login.html'; };

const map = L.map('map').setView([25.04, 121.49], 11);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(map);
let markers = L.layerGroup().addTo(map);
let dashboardData = null;

const statusLabel = { shortage: '缺車', full: '滿站', safe: '安全' };
const taskDisplay = {
  PUBLISHED: { label: '待接收', className: 'todo', active: true, done: false },
  ACCEPTED: { label: '進行中', className: 'doing', active: true, done: false },
  PICKED_UP: { label: '進行中', className: 'doing', active: true, done: false },
  COMPLETED: { label: '已完成', className: 'done', active: false, done: true },
};

function setText(id, value) { document.querySelector(id).textContent = value; }
function esc(value) { return String(value ?? '—').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c])); }
function taskInfo(task) { return taskDisplay[task.status] || { label: String(task.status || '未知'), className: 'todo', active: true, done: false }; }
function hasCoordinate(station) { return Number.isFinite(Number(station.lat)) && Number.isFinite(Number(station.lng)); }
function currentDocks(station) { return Number.isInteger(station.available_docks) ? station.available_docks : Math.max(0, Number(station.slots || 0) - Number(station.bikes || 0)); }
function stationRisk(station) {
  const type = ['shortage', 'full', 'safe'].includes(station.status) ? station.status : 'safe';
  const severity = station.alert_severity === 'critical' ? 30 : station.alert_severity === 'warning' ? 15 : 0;
  return { type, score: severity };
}
function forecastText(data) {
  return data.meta?.prediction?.status === 'BLOCKED' ? '預測暫不可用' : '預測資料暫不可用';
}
function districtOptions(data) {
  return data.meta?.districts?.length ? data.meta.districts : [...new Set(data.assignments.map(item => item.district).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-Hant'));
}
// Round 3 contract helpers: resolve owner through station_code -> zone -> worker,
// never by collapsing a whole district to a single (last/first-wins) worker.
function zoneOrderOf(assignment) {
  if (Number.isFinite(Number(assignment.zoneOrder))) return Number(assignment.zoneOrder);
  const match = String(assignment.zone || '').match(/第\s*(\d+)/);
  return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
}
function sortZonesNumerically(assignments) {
  return [...assignments].sort((a, b) =>
    String(a.district || '').localeCompare(String(b.district || ''), 'zh-Hant')
    || zoneOrderOf(a) - zoneOrderOf(b)
    || String(a.zone || '').localeCompare(String(b.zone || ''), 'zh-Hant'));
}
function buildOwnerByStationCode(assignments) {
  const map = new Map();
  for (const assignment of assignments) {
    for (const code of assignment.stationCodes || []) {
      map.set(code, { worker: assignment.worker, zone: assignment.zone, staffId: assignment.staff_id });
    }
  }
  return map;
}
function zonesForDistrict(assignments, district) {
  return sortZonesNumerically(assignments.filter(a => String(a.district || '').split('、').includes(district)));
}

async function render() {
  const district = document.querySelector('#district').value;
  const data = await fetch(`/api/dashboard?district=${encodeURIComponent(district)}`).then(r => r.json());
  dashboardData = data;
  const shortage = data.stations.filter(s => s.status === 'shortage');
  const full = data.stations.filter(s => s.status === 'full');
  setText('#districtCount', districtOptions(data).length);
  setText('#shortageCount', shortage.length);
  setText('#fullCount', full.length);
  setText('#activeCount', data.tasks.filter(t => taskInfo(t).active).length);
  setText('#doneCount', data.tasks.filter(t => taskInfo(t).done).length);

  const districtSelect = document.querySelector('#district');
  if (districtSelect.options.length === 1) {
    districtOptions(data).forEach(districtName => districtSelect.add(new Option(districtName, districtName)));
  }

  const ownerByStationCode = buildOwnerByStationCode(data.assignments);
  markers.clearLayers();
  data.stations.filter(hasCoordinate).forEach(s => {
    const risk = stationRisk(s);
    const color = risk.type === 'shortage' ? '#e67e22' : risk.type === 'full' ? '#c0392b' : '#15803d';
    const radius = 7 + Math.min(18, risk.score * .45);
    const owner = ownerByStationCode.get(s.station_code);
    const ownerText = owner ? `${esc(owner.worker)}（${esc(owner.zone)}）` : '未分配責任區';
    L.circleMarker([Number(s.lat), Number(s.lng)], { radius, color, fillColor: color, fillOpacity: .82, weight: 2 })
      .bindPopup(`<b>${esc(s.name)}</b><br>${esc(s.district)}｜負責人：${ownerText}<br>現有 ${s.bikes} 台／空位 ${currentDocks(s)}<br>${forecastText(data)}<br><b>${statusLabel[risk.type]}</b>`)
      .addTo(markers);
  });

  document.querySelector('#districtRows').innerHTML = sortZonesNumerically(data.assignments).map(a => {
    const hasProblem = Number(a.shortageCount || 0) + Number(a.fullCount || 0) > 0;
    return `<tr><td><b>${esc(a.district)}</b></td><td>${esc(a.zone)}</td><td><span class="owner">${esc(a.worker)}</span></td><td>${a.stationCount || '—'}</td><td class="number-danger">${a.shortageCount || '—'}</td><td class="number-warning">${a.fullCount || '—'}</td><td>${a.todoCount || '—'}</td><td>${a.doingCount || '—'}</td><td>${a.doneCount || '—'}${hasProblem ? '' : ''}</td></tr>`;
  }).join('');
  document.querySelector('#taskRows').innerHTML = data.tasks.map(t => {
    const info = taskInfo(t);
    return `<tr><td>${esc(t.district)}</td><td>${esc(t.worker)}</td><td>${esc(t.origin)} → ${esc(t.destination)}</td><td>${t.suggested} 台</td><td>${t.actual ?? '—'} </td><td><span class="badge ${info.className}">${esc(info.label)}</span></td><td>${esc(t.note)}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="empty">此行政區目前沒有任務</td></tr>';
  const reports = data.tasks.filter(t => taskInfo(t).done).map(t => {
    const diff = t.actual - t.suggested;
    return `<div class="report-item"><div class="report-title">${esc(t.worker)}｜${esc(t.district)}</div><div>${esc(t.origin)} → ${esc(t.destination)}</div><div class="difference">建議 ${t.suggested} 台，實際 ${t.actual} 台${diff === 0 ? '，數量一致。' : `，差異 ${Math.abs(diff)} 台。`}</div>${t.note ? `<div class="muted">${esc(t.note)}</div>` : ''}</div>`;
  }).join('');
  document.querySelector('#reportsList').innerHTML = reports || '<div class="empty">尚無已完成任務回報</div>';
  setupDispatch(data);
}

function setupDispatch(data) {
  const districtSelect = document.querySelector('#dispatchDistrict');
  if (districtSelect.options.length === 1) {
    districtOptions(data).forEach(districtName => districtSelect.add(new Option(districtName, districtName)));
  }
  updateDispatchFields();
}

// Option values are canonical station_code (not station name), so dispatch flows
// through the station_code -> zone -> worker contract end to end.
function updateDispatchFields() {
  if (!dashboardData) return;
  const district = document.querySelector('#dispatchDistrict').value;
  const stations = dashboardData.stations.filter(s => s.district === district);
  const origin = document.querySelector('#dispatchOrigin');
  const destination = document.querySelector('#dispatchDestination');
  const stationOptions = stations.map(s => `<option value="${esc(s.station_code)}">${esc(s.name)}｜${statusLabel[stationRisk(s).type]} ${s.bikes}/${s.slots}</option>`).join('');
  origin.innerHTML = `<option value="">請選擇起始站</option>${stationOptions}`;
  destination.innerHTML = `<option value="">請選擇目的站</option>${stationOptions}`;
  // Selecting a district loads that district's responsibility-zone owners into
  // the 負責人 dropdown (one option per zone, value = canonical staff_id, zones
  // numerically sorted). It does NOT pre-select any owner or station.
  const worker = document.querySelector('#dispatchWorker');
  const zones = zonesForDistrict(dashboardData.assignments, district);
  const workerOptions = zones.map(a => `<option value="${esc(a.staff_id)}">${esc(a.worker)}｜${esc(a.zone)}</option>`).join('');
  worker.innerHTML = `<option value="">請選擇負責人</option>${workerOptions}`;
  // Do not auto-pick anything: owner and both stations stay on their placeholders.
  worker.value = '';
  origin.value = '';
  destination.value = '';
}

document.querySelector('#dispatchForm').addEventListener('submit', async event => {
  event.preventDefault();
  const message = document.querySelector('#dispatchMessage');
  const staffId = document.querySelector('#dispatchWorker').value;
  const originCode = document.querySelector('#dispatchOrigin').value;
  const destinationCode = document.querySelector('#dispatchDestination').value;
  const suggested = Number(document.querySelector('#dispatchAmount').value);
  message.classList.remove('error');
  if (!staffId) {
    message.classList.add('error');
    message.textContent = '請選擇負責人。';
    return;
  }
  if (!originCode || !destinationCode) {
    message.classList.add('error');
    message.textContent = '請選擇起點站與目的站。';
    return;
  }
  const response = await fetch('/api/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user.username, staffId, originCode, destinationCode, suggested }),
  });
  const result = await response.json().catch(() => ({}));
  if (response.ok) {
    message.textContent = `派工完成：${result.task_code || ''}｜負責人 ${result.worker || ''}（${result.zone || ''}）`;
    await render();
  } else {
    message.classList.add('error');
    message.textContent = result.message || '派工失敗，請稍後再試。';
  }
});
document.querySelector('#dispatchDistrict').addEventListener('change', updateDispatchFields);
document.querySelector('#district').addEventListener('change', render);
render();
