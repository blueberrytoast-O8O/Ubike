const staffUser = JSON.parse(sessionStorage.getItem('dispatchUser') || 'null');
if (!staffUser || staffUser.role !== 'worker') location.href = '/login.html';

document.querySelector('#identity').textContent = `${staffUser.name}｜工作人員`;
document.querySelector('#welcome').textContent = `${staffUser.name}，今天辛苦了`;
document.querySelector('#signout').onclick = () => { sessionStorage.clear(); location.href = '/login.html'; };

const staffMap = L.map('staffMap').setView([25.025, 121.456], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution:'© OpenStreetMap' }).addTo(staffMap);
let staffMarkers = L.layerGroup().addTo(staffMap);
let routeLayer = null;
let currentTasks = [];
let stationLookup = new Map();
let activeTask = null;
let actualCount = 0;
let taskPage = 1;
let taskGroup = 'unfinished';
const tasksPerPage = 2;
const labels = { shortage:'缺車', full:'滿站', safe:'安全' };
const badgeClass = { shortage:'shortage', full:'full', safe:'safe', '待接收':'todo', '進行中':'doing', '已完成':'done' };
const esc = value => String(value ?? '—').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[char]));

function riskOf(station) {
  if (!station || !station.slots) return { score:0, type:'safe', forecastRatio:0 };
  const ratios = [station.forecast30 / station.slots, station.forecast60 / station.slots];
  let result = { score:0, type:'safe', forecastRatio:ratios[1] };
  ratios.forEach(ratio => {
    const shortageDistance = ratio < .3 ? .3 - ratio : 0;
    const fullDistance = ratio > .7 ? ratio - .7 : 0;
    const distance = Math.max(shortageDistance, fullDistance);
    if (distance > result.score) result = { score:distance, type:shortageDistance > fullDistance ? 'shortage' : 'full', forecastRatio:ratio };
  });
  return { ...result, score:Math.round(result.score * 100) };
}

function severityLabel(score) {
  if (score >= 20) return '極高';
  if (score >= 10) return '高';
  if (score > 0) return '注意';
  return '安全';
}

function taskSeverity(task) {
  return Math.max(riskOf(stationLookup.get(task.origin)).score, riskOf(stationLookup.get(task.destination)).score);
}

function stationAdvice(station, tasks) {
  const incoming = tasks.find(task => task.destination === station.name && task.status !== '已完成');
  const outgoing = tasks.find(task => task.origin === station.name && task.status !== '已完成');
  if (incoming) return `建議補入 ${incoming.suggested} 台`;
  if (outgoing) return `建議移出 ${outgoing.suggested} 台`;
  return riskOf(station).type === 'safe' ? '持續觀察' : '等待任務安排';
}

function stationTask(station, tasks) {
  const task = tasks.find(item => item.origin === station.name || item.destination === station.name);
  return task ? task.status : '尚未派工';
}

function sortedTasks() {
  const grouped = currentTasks.filter(task => taskGroup === 'completed' ? task.status === '已完成' : task.status !== '已完成');
  return grouped.sort((a,b) => {
    const severityDiff = taskSeverity(b) - taskSeverity(a);
    return severityDiff || a.id - b.id;
  });
}

function routeEstimate(task) {
  const start = stationLookup.get(task.origin); const end = stationLookup.get(task.destination);
  if (!start || !end) return null;
  const rad = n => n * Math.PI / 180;
  const dLat = rad(end.lat-start.lat), dLng = rad(end.lng-start.lng);
  const a = Math.sin(dLat/2)**2 + Math.cos(rad(start.lat))*Math.cos(rad(end.lat))*Math.sin(dLng/2)**2;
  const distance = 6371 * 2 * Math.atan2(Math.sqrt(a),Math.sqrt(1-a)) * 1.3;
  const hour = new Date().getHours();
  const weekday = new Date().getDay() > 0 && new Date().getDay() < 6;
  const peak = weekday && ((hour >= 7 && hour < 10) || (hour >= 17 && hour < 20));
  const busy = !peak && hour >= 10 && hour < 21;
  const traffic = peak ? '尖峰壅塞' : busy ? '一般車流' : '離峰順暢';
  const speed = peak ? 16 : busy ? 22 : 28;
  return { start, end, distance, minutes:Math.max(3,Math.ceil(distance/speed*60)), traffic };
}

function showRoute(task) {
  const route = routeEstimate(task);
  if (!route) return;
  if (routeLayer) staffMap.removeLayer(routeLayer);
  const points = [[route.start.lat,route.start.lng],[route.end.lat,route.end.lng]];
  routeLayer = L.polyline(points,{color:'#24282d',weight:6,opacity:.8,dashArray:'12 8'}).addTo(staffMap);
  staffMap.fitBounds(routeLayer.getBounds(),{padding:[45,45]});
  document.querySelector('#routeSummary').innerHTML = `<b>任務 #${task.id}｜約 ${route.minutes} 分鐘・${route.distance.toFixed(1)} 公里</b><span>${esc(task.origin)} → ${esc(task.destination)}</span><small>${route.traffic}｜Demo 時段路況估算，虛線為路線示意</small>`;
  document.querySelector('#algorithmResult').textContent = `任務 #${task.id}｜${taskSeverity(task)}% 偏離｜${route.minutes} 分鐘`;
}

function renderTaskList() {
  const tasks = sortedTasks();
  const pageCount = Math.max(1, Math.ceil(tasks.length / tasksPerPage));
  taskPage = Math.min(taskPage, pageCount);
  const pageTasks = tasks.slice((taskPage - 1) * tasksPerPage, taskPage * tasksPerPage);
  const groupLabel = taskGroup === 'completed' ? '已完成' : '未完成';
  document.querySelector('#taskRule').textContent = `${groupLabel}共 ${tasks.length} 筆，每頁 ${tasksPerPage} 筆。安全水位為總車位 30–70%，彩色圓點表示偏離幅度。`;
  document.querySelector('#taskList').innerHTML = pageTasks.map(task => {
    const score = taskSeverity(task);
    const action = task.status === '已完成' ? '<button class="task-action" disabled>已回報</button>' : task.status === '待接收' ? `<button class="task-action accept-action" data-accept-id="${task.id}">接收任務</button>` : `<button class="task-action" data-task-id="${task.id}">回報任務</button>`;
    const severityClass = score >= 20 ? 'critical' : score >= 10 ? 'high' : score > 0 ? 'notice' : 'safe';
    const estimate = routeEstimate(task);
    return `<article class="task-card"><div class="task-card-top"><span class="severity-indicator"><i class="severity-dot ${severityClass}"></i>${severityLabel(score)}・偏離 ${score}%</span><small>任務 #${task.id}</small></div><div class="task-route"><b>${esc(task.origin)}</b><span>→</span><b>${esc(task.destination)}</b></div><div class="task-facts"><span>建議 <b>${task.suggested} 台</b></span><span>預估 <b>${estimate?.minutes || '—'} 分</b></span></div>${task.status === '已完成' ? `<p>實際完成 ${task.actual} 台</p>` : ''}<button class="route-action" data-route-id="${task.id}">查看最快路線</button>${action}</article>`;
  }).join('') || '<div class="empty">目前沒有任務</div>';
  document.querySelector('#pageInfo').textContent = `${taskPage} / ${pageCount} 頁`;
  document.querySelector('#previousPage').disabled = taskPage === 1;
  document.querySelector('#nextPage').disabled = taskPage === pageCount;
  document.querySelectorAll('[data-task-id]').forEach(button => button.onclick = () => openTask(Number(button.dataset.taskId)));
  document.querySelectorAll('[data-accept-id]').forEach(button => button.onclick = () => acceptTask(Number(button.dataset.acceptId)));
  document.querySelectorAll('[data-route-id]').forEach(button => button.onclick = () => showRoute(currentTasks.find(task => task.id === Number(button.dataset.routeId))));
}

async function acceptTask(taskId) {
  const response = await fetch(`/api/tasks/${taskId}/accept`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:staffUser.username})});
  const result = await response.json();
  if (!response.ok) return alert(result.message);
  await loadStaffDashboard();
}

async function loadStaffDashboard() {
  const response = await fetch(`/api/staff-dashboard?username=${encodeURIComponent(staffUser.username)}`);
  if (!response.ok) return location.href = '/login.html';
  const data = await response.json();
  currentTasks = data.tasks;
  stationLookup = new Map(data.stations.map(station => [station.name, station]));
  document.querySelector('#scope').textContent = `負責範圍：${data.assignments.map(item => `${item.zone}（${item.district}）`).join('、')}`;
  document.querySelector('#shortageCount').textContent = data.stations.filter(station => riskOf(station).type === 'shortage').length;
  document.querySelector('#fullCount').textContent = data.stations.filter(station => riskOf(station).type === 'full').length;
  document.querySelector('#todoCount').textContent = data.tasks.filter(task => task.status !== '已完成').length;
  document.querySelector('#doneCount').textContent = data.tasks.filter(task => task.status === '已完成').length;
  document.querySelector('#unfinishedCount').textContent = data.tasks.filter(task => task.status !== '已完成').length;
  document.querySelector('#completedCount').textContent = data.tasks.filter(task => task.status === '已完成').length;
  const newCount = data.tasks.filter(task => task.status === '待接收').length;
  document.querySelector('#noticeTitle').textContent = newCount ? `你有 ${newCount} 筆新任務待接收` : '目前沒有新的派工';
  document.querySelector('#noticeText').textContent = newCount ? '已按嚴重程度排列，接收後即可開始調度。' : '進行中的任務仍可從「我的任務」查看。';
  document.querySelector('#taskNotice').classList.toggle('has-new', newCount > 0);

  staffMarkers.clearLayers();
  data.stations.forEach(station => {
    const risk = riskOf(station);
    const color = risk.type === 'shortage' ? '#e67e22' : risk.type === 'full' ? '#c0392b' : '#15803d';
    // 偏離安全水位 0–30 個百分點，半徑依比例由 2px 放大至 15px。
    const markerRadius = 2 + (Math.min(30, risk.score) / 30) * 13;
    const marker = L.circleMarker([station.lat,station.lng], {radius:markerRadius,color:'#fff',fillColor:color,fillOpacity:.88,weight:2})
      .bindPopup(`<b>${esc(station.name)}</b><br>${esc(station.district)}｜${labels[risk.type]}<br>現有 ${station.bikes} 台／空位 ${station.slots-station.bikes}<br>30 分鐘 ${station.forecast30} 台／60 分鐘 ${station.forecast60} 台<br>偏離安全水位 ${risk.score}%<br><b>${esc(stationAdvice(station,data.tasks))}</b>`);
    marker.addTo(staffMarkers);
  });

  const sortedStations = [...data.stations].sort((a,b) => riskOf(b).score - riskOf(a).score || a.district.localeCompare(b.district, 'zh-Hant'));
  document.querySelector('#stationRows').innerHTML = sortedStations.map(station => {
    const risk = riskOf(station);
    return `<tr><td><small>${esc(station.district)}</small><br><b>${esc(station.name)}</b></td><td>${station.bikes} 台／${station.slots-station.bikes} 格</td><td>${station.forecast30} 台（${Math.round(station.forecast30/station.slots*100)}%）</td><td>${station.forecast60} 台（${Math.round(station.forecast60/station.slots*100)}%）</td><td><span class="badge ${badgeClass[risk.type]}">${labels[risk.type]}</span></td><td><b>${esc(stationAdvice(station,data.tasks))}</b></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty">責任區目前沒有站點資料</td></tr>';
  renderTaskList();
  const suggestedTask = data.tasks.find(task => task.status === '進行中') || data.tasks.find(task => task.status === '待接收');
  if (suggestedTask) showRoute(suggestedTask);
}

function openTask(taskId) {
  activeTask = currentTasks.find(task => task.id === taskId);
  actualCount = activeTask.suggested;
  document.querySelector('#dialogRoute').textContent = `任務 #${activeTask.id}｜${activeTask.district}`;
  document.querySelector('#dialogOrigin').textContent = activeTask.origin;
  document.querySelector('#dialogDestination').textContent = activeTask.destination;
  document.querySelector('#dialogSuggested').textContent = activeTask.suggested;
  document.querySelector('#actualCount').textContent = actualCount;
  document.querySelectorAll('input[name="reason"]').forEach(input => { input.checked = false; });
  document.querySelector('#note').value = '';
  document.querySelector('#completeError').textContent = '';
  document.querySelector('#completeDialog').showModal();
}

document.querySelectorAll('[data-task-group]').forEach(button => button.onclick = () => {
  taskGroup = button.dataset.taskGroup;
  taskPage = 1;
  document.querySelectorAll('[data-task-group]').forEach(tab => tab.classList.toggle('active', tab === button));
  renderTaskList();
});
document.querySelector('#previousPage').onclick = () => { if (taskPage > 1) { taskPage -= 1; renderTaskList(); } };
document.querySelector('#nextPage').onclick = () => { if (taskPage < Math.ceil(sortedTasks().length/tasksPerPage)) { taskPage += 1; renderTaskList(); } };
document.querySelector('#decrease').onclick = () => { actualCount = Math.max(0,actualCount-1); document.querySelector('#actualCount').textContent = actualCount; };
document.querySelector('#increase').onclick = () => { actualCount += 1; document.querySelector('#actualCount').textContent = actualCount; };
document.querySelectorAll('[data-close-dialog]').forEach(button => button.onclick = () => document.querySelector('#completeDialog').close());
document.querySelector('#completeForm').addEventListener('submit', async event => {
  event.preventDefault();
  const reasons = [...document.querySelectorAll('input[name="reason"]:checked')].map(input => input.value);
  const detail = document.querySelector('#note').value.trim();
  if (actualCount !== activeTask.suggested && reasons.length === 0) { document.querySelector('#completeError').textContent = '數量不同時請至少選擇一個原因'; return; }
  const note = [reasons.join('、'),detail].filter(Boolean).join('：');
  const response = await fetch(`/api/tasks/${activeTask.id}/complete`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:staffUser.username,actual:actualCount,note})});
  const result = await response.json();
  if (!response.ok) { document.querySelector('#completeError').textContent = result.message; return; }
  document.querySelector('#completeDialog').close();
  loadStaffDashboard();
});

loadStaffDashboard();
