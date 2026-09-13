const user = JSON.parse(sessionStorage.getItem('dispatchUser') || 'null');
if (!user || user.role !== 'admin') location.href = '/login.html';
document.querySelector('#identity').textContent = `${user.name}｜全行政區管理權限`;
document.querySelector('#signout').onclick = () => { sessionStorage.clear(); location.href = '/login.html'; };
const map = L.map('map').setView([25.04,121.49], 11);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution:'© OpenStreetMap' }).addTo(map);
let markers = L.layerGroup().addTo(map);
const statusLabel = { shortage:'缺車', full:'滿站', safe:'安全' };
const taskClass = { '待接收':'todo', '進行中':'doing', '已完成':'done' };
let dashboardData = null;
function setText(id, value) { document.querySelector(id).textContent = value; }
function esc(value) { return String(value ?? '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c])); }
async function render() {
  const district = document.querySelector('#district').value;
  const data = await fetch(`/api/dashboard?district=${encodeURIComponent(district)}`).then(r => r.json());
  dashboardData = data;
  const shortage = data.stations.filter(s => s.status === 'shortage'); const full = data.stations.filter(s => s.status === 'full');
  setText('#districtCount', data.assignments.length); setText('#shortageCount', shortage.length); setText('#fullCount', full.length); setText('#activeCount', data.tasks.filter(t => t.status !== '已完成').length); setText('#doneCount', data.tasks.filter(t => t.status === '已完成').length);
  if (document.querySelector('#district').options.length === 1) data.assignments.forEach(a => document.querySelector('#district').add(new Option(a.district, a.district)));
  const owners = Object.fromEntries(data.assignments.map(a => [a.district, a.worker]));
  markers.clearLayers();
  data.stations.forEach(s => { const color = s.status === 'shortage' ? '#e67e22' : s.status === 'full' ? '#c0392b' : '#15803d'; const ratio = Math.max(s.forecast30,s.forecast60) / s.slots; const severity = ratio < .3 ? (.3-ratio)*100 : ratio > .7 ? (ratio-.7)*100 : 0; const radius = 7 + Math.min(18,severity*.45); L.circleMarker([s.lat,s.lng], { radius, color, fillColor:color, fillOpacity:.82, weight:2 }).bindPopup(`<b>${esc(s.name)}</b><br>${esc(s.district)}｜負責人：${esc(owners[s.district])}<br>現有 ${s.bikes} 台／空位 ${s.slots-s.bikes}<br>30 分鐘：${s.forecast30} 台（可借率 ${Math.round(s.forecast30/s.slots*100)}%）；60 分鐘：${s.forecast60} 台（可借率 ${Math.round(s.forecast60/s.slots*100)}%）<br><b>${statusLabel[s.status]}</b>`).addTo(markers); });
  document.querySelector('#districtRows').innerHTML = data.assignments.map(a => { const hasProblem = a.shortageCount + a.fullCount > 0; return `<tr><td><b>${esc(a.district)}</b></td><td>${esc(a.zone)}</td><td><span class="owner">${esc(a.worker)}</span></td><td>${a.stationCount || '—'}</td><td class="number-danger">${a.shortageCount || '—'}</td><td class="number-warning">${a.fullCount || '—'}</td><td>${a.todoCount || '—'}</td><td>${a.doingCount || '—'}</td><td>${a.doneCount || '—'}${hasProblem ? '' : ''}</td></tr>`; }).join('');
  document.querySelector('#taskRows').innerHTML = data.tasks.map(t => `<tr><td>${esc(t.district)}</td><td>${esc(t.worker)}</td><td>${esc(t.origin)} → ${esc(t.destination)}</td><td>${t.suggested} 台</td><td>${t.actual ?? '—'} </td><td><span class="badge ${taskClass[t.status]}">${esc(t.status)}</span></td><td>${esc(t.note)}</td></tr>`).join('') || '<tr><td colspan="7" class="empty">此行政區目前沒有任務</td></tr>';
  const reports = data.tasks.filter(t => t.status === '已完成').map(t => { const diff = t.actual - t.suggested; return `<div class="report-item"><div class="report-title">${esc(t.worker)}｜${esc(t.district)}</div><div>${esc(t.origin)} → ${esc(t.destination)}</div><div class="difference">建議 ${t.suggested} 台，實際 ${t.actual} 台${diff === 0 ? '，數量一致。' : `，差異 ${Math.abs(diff)} 台。`}</div>${t.note ? `<div class="muted">${esc(t.note)}</div>` : ''}</div>`; }).join('');
  document.querySelector('#reportsList').innerHTML = reports || '<div class="empty">尚無已完成任務回報</div>';
  setupDispatch(data);
}

function setupDispatch(data) {
  const districtSelect = document.querySelector('#dispatchDistrict');
  if (districtSelect.options.length === 1) {
    data.assignments.filter(a => a.stationCount >= 2).forEach(a => districtSelect.add(new Option(a.district, a.district)));
  }
  updateDispatchFields();
}

function updateDispatchFields() {
  if (!dashboardData) return;
  const district = document.querySelector('#dispatchDistrict').value;
  const assignment = dashboardData.assignments.find(a => a.district === district);
  const stations = dashboardData.stations.filter(s => s.district === district);
  document.querySelector('#dispatchWorker').value = assignment?.worker || '';
  const origin = document.querySelector('#dispatchOrigin');
  const destination = document.querySelector('#dispatchDestination');
  const options = stations.map(s => `<option value="${esc(s.name)}">${esc(s.name)}｜${statusLabel[s.status]} ${s.bikes}/${s.slots}</option>`).join('');
  origin.innerHTML = `<option value="">選擇取車站</option>${options}`;
  destination.innerHTML = `<option value="">選擇補車站</option>${options}`;
  const fullStation = stations.find(s => s.status === 'full');
  const shortageStation = stations.find(s => s.status === 'shortage');
  if (fullStation) origin.value = fullStation.name;
  if (shortageStation) destination.value = shortageStation.name;
}

document.querySelector('#dispatchForm').addEventListener('submit', async event => {
  event.preventDefault();
  const message = document.querySelector('#dispatchMessage');
  const payload = { username:user.username, district:document.querySelector('#dispatchDistrict').value, worker:document.querySelector('#dispatchWorker').value, origin:document.querySelector('#dispatchOrigin').value, destination:document.querySelector('#dispatchDestination').value, suggested:Number(document.querySelector('#dispatchAmount').value) };
  const response = await fetch('/api/tasks', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
  const result = await response.json();
  message.classList.toggle('error', !response.ok);
  message.textContent = response.ok ? `派工成功：任務 #${result.task.id} 已通知 ${payload.worker}` : result.message;
  if (response.ok) await render();
});
document.querySelector('#dispatchDistrict').addEventListener('change', updateDispatchFields);
document.querySelector('#district').addEventListener('change', render); render();
