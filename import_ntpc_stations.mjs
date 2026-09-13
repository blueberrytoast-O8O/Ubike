import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const projectDir = path.dirname(fileURLToPath(import.meta.url));
const db = new DatabaseSync(path.join(projectDir,'dispatch-demo.sqlite'));
const datasetUrl = 'https://data.ntpc.gov.tw/api/datasets/010E5B15-3823-4B20-B401-B1CF000550C5/json?page=0&size=2000';
const response = await fetch(datasetUrl);
if (!response.ok) throw new Error(`官方資料下載失敗：HTTP ${response.status}`);
const rows = await response.json();
if (!Array.isArray(rows) || rows.length < 1000) throw new Error(`官方資料筆數異常：${rows?.length ?? 0}`);

const columns = db.prepare('PRAGMA table_info(stations)').all().map(column => column.name);
const additions = {station_code:'TEXT',address:'TEXT',observed_at:'TEXT',available_docks:'INTEGER',active:'INTEGER DEFAULT 1',data_source:"TEXT DEFAULT 'DEMO_MOCK'"};
for (const [name,definition] of Object.entries(additions)) if (!columns.includes(name)) db.exec(`ALTER TABLE stations ADD COLUMN ${name} ${definition}`);
db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_stations_station_code ON stations(station_code) WHERE station_code IS NOT NULL');
const nextId = db.prepare('SELECT COALESCE(MAX(id),0)+1 AS id FROM stations');
const existing = db.prepare('SELECT id FROM stations WHERE station_code = ?');
const insert = db.prepare(`INSERT INTO stations (id,district,name,bikes,slots,forecast30,forecast60,status,lat,lng,station_code,address,observed_at,available_docks,active,data_source)
  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
const update = db.prepare(`UPDATE stations SET district=?,name=?,bikes=?,slots=?,forecast30=?,forecast60=?,status=?,lat=?,lng=?,address=?,observed_at=?,available_docks=?,active=?,data_source='NTPC_OPEN_DATA' WHERE station_code=?`);
db.exec('BEGIN');
try {
  for (const item of rows) {
    const code = String(item.sno || '').trim();
    const slots = Number(item.tot_quantity) || 0;
    const bikes = Number(item.sbi_quantity) || 0;
    const docks = Number(item.bemp) || Math.max(0,slots-bikes);
    if (!code || !item.sarea || !item.sna || slots <= 0) continue;
    const ratio = bikes/slots;
    const status = ratio < .3 ? 'shortage' : ratio > .7 ? 'full' : 'safe';
    const name = String(item.sna).replace(/^YouBike2\.0_/,'');
    const current = existing.get(code);
    if (current) update.run(item.sarea,name,bikes,slots,bikes,bikes,status,Number(item.lat),Number(item.lng),item.ar || '',item.mday || '',docks,Number(item.act) === 1 ? 1 : 0,code);
    else insert.run(nextId.get().id,item.sarea,name,bikes,slots,bikes,bikes,status,Number(item.lat),Number(item.lng),code,item.ar || '',item.mday || '',docks,Number(item.act) === 1 ? 1 : 0,'NTPC_OPEN_DATA');
  }
  db.exec('COMMIT');
} catch (error) {
  db.exec('ROLLBACK');
  throw error;
}
const result = db.prepare("SELECT COUNT(*) AS stations, COUNT(DISTINCT district) AS districts, MAX(observed_at) AS updated FROM stations WHERE data_source='NTPC_OPEN_DATA'").get();
db.exec('CREATE TABLE IF NOT EXISTS worker_station_assignments (staff_id TEXT NOT NULL, station_id INTEGER NOT NULL, zone_label TEXT NOT NULL, PRIMARY KEY (staff_id, station_id))');
const assignStation = db.prepare('INSERT OR IGNORE INTO worker_station_assignments (staff_id, station_id, zone_label) VALUES (?, ?, ?)');
db.prepare('DELETE FROM worker_station_assignments').run();
db.prepare("DELETE FROM staff_members WHERE staff_id LIKE 'NT%' OR staff_id LIKE 'RG%' OR staff_id LIKE 'CR%'").run();
const demoSurnames=['陳','林','黃','張','李','王','吳','劉','蔡','楊','許','鄭','謝','洪','郭','邱','曾','廖','賴','周'];
const demoGivenNames=['冠宇','承恩','柏翰','俊傑','家豪','宗翰','彥廷','子軒','品睿','宇翔','雅婷','怡君','佳穎','欣怡','郁晴','詩涵','佩珊','婉婷','思妤','庭瑜'];
const usedStaffNames=new Set(db.prepare('SELECT name FROM staff_members').all().map(item=>item.name));
let demoNameNumber=0;
function nextDemoStaffName(){
  while(demoNameNumber<demoSurnames.length*demoGivenNames.length){const index=demoNameNumber++;const name=demoSurnames[Math.floor(index/demoGivenNames.length)]+demoGivenNames[index%demoGivenNames.length];if(!usedStaffNames.has(name)){usedStaffNames.add(name);return name;}}
  throw new Error('示範工作人員姓名組合不足');
}
const addStaff = db.prepare('INSERT OR IGNORE INTO staff_members (staff_id,name,role,phone,status) VALUES (?,?,?,?,?)');
const legacyByDistrict = new Map(db.prepare('SELECT district,worker FROM district_assignments').all().map(row => [row.district,row.worker]));
const usedLegacyNames = new Set(['王小明','李冠宇']);
let generatedStaffNumber = 1;
const pooledSmallDistrictStations=[];
for (const {district} of db.prepare('SELECT DISTINCT district FROM stations WHERE active=1 ORDER BY district').all()) {
  const remaining = db.prepare('SELECT id,name,lat,lng FROM stations WHERE active=1 AND district=? ORDER BY id').all(district);
  if(remaining.length<8){pooledSmallDistrictStations.push(...remaining.map(station=>({...station,district})));continue;}
  const minimumGroups=Math.ceil(remaining.length/10);
  const maximumGroups=Math.floor(remaining.length/8);
  const canPartitionWholeDistrict=minimumGroups<=maximumGroups;
  const groupCount=canPartitionWholeDistrict?minimumGroups:Math.floor(remaining.length/10);
  const baseSize=canPartitionWholeDistrict?Math.floor(remaining.length/groupCount):10;
  const largerGroups=canPartitionWholeDistrict?remaining.length%groupCount:0;
  for (let groupIndex=0;groupIndex<groupCount;groupIndex+=1) {
    const groupSize=baseSize+(groupIndex<largerGroups?1:0);
    const seed=remaining[0];
    const cluster=remaining.sort((a,b) => {
      const distance=station => ((station.lat-seed.lat)**2)+(((station.lng-seed.lng)*.91)**2);
      return distance(a)-distance(b);
    }).slice(0,groupSize);
    cluster.forEach(station => remaining.splice(remaining.findIndex(item => item.id===station.id),1));
    let staffId;
    let staffName;
    if(district==='新莊區'&&groupIndex===0){staffId='W001';staffName='王小明';}
    else if(district==='板橋區'&&groupIndex===0){staffId='W012';staffName='李冠宇';}
    else {
      const legacyName=groupIndex===0?legacyByDistrict.get(district):null;
      const legacy=legacyName&&!usedLegacyNames.has(legacyName)?db.prepare('SELECT staff_id,name FROM staff_members WHERE name=?').get(legacyName):null;
      if(legacy){staffId=legacy.staff_id;staffName=legacy.name;usedLegacyNames.add(legacyName);}
      else {staffId=`RG${String(generatedStaffNumber).padStart(4,'0')}`;staffName=nextDemoStaffName();generatedStaffNumber+=1;addStaff.run(staffId,staffName,'調度管理人員',`09${String(20000000+generatedStaffNumber*137).slice(-8)}`,'在職');}
    }
    const zoneLabel=`${district}第${groupIndex+1}責任區`;
    cluster.forEach(station => assignStation.run(staffId,station.id,zoneLabel));
  }
  pooledSmallDistrictStations.push(...remaining.map(station=>({...station,district})));
}
const pooledGroupCount=Math.max(1,Math.ceil(pooledSmallDistrictStations.length/10));
const pooledBaseSize=Math.floor(pooledSmallDistrictStations.length/pooledGroupCount);
const pooledLargerGroups=pooledSmallDistrictStations.length%pooledGroupCount;
for(let groupIndex=0;groupIndex<pooledGroupCount;groupIndex+=1){
  const seed=pooledSmallDistrictStations[0];
  const groupSize=pooledBaseSize+(groupIndex<pooledLargerGroups?1:0);
  const cluster=pooledSmallDistrictStations.sort((a,b)=>{const distance=station=>((station.lat-seed.lat)**2)+(((station.lng-seed.lng)*.91)**2);return distance(a)-distance(b);}).slice(0,groupSize);
  cluster.forEach(station=>pooledSmallDistrictStations.splice(pooledSmallDistrictStations.findIndex(item=>item.id===station.id),1));
  const staffId=`CR${String(groupIndex+1).padStart(3,'0')}`;
  const staffName=nextDemoStaffName();
  generatedStaffNumber+=1;
  addStaff.run(staffId,staffName,'調度管理人員',`09${String(20000000+generatedStaffNumber*137).slice(-8)}`,'在職');
  cluster.forEach(station=>assignStation.run(staffId,station.id,`跨區第${groupIndex+1}責任區`));
}
console.log(JSON.stringify(result));
