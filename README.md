# YouBike 調度系統 Demo

## 啟動方式

在此資料夾執行：

```powershell
npm start
```

開啟 `http://localhost:3000`。

更新新北市政府官方 YouBike 行政區與站點資料：

```powershell
npm run sync:stations
```

同步程式會以文字保存官方站號，並更新行政區、站名、地址、總車位、可借／可還數、營運狀態、更新時間及經緯度。快速派工的行政區、起點站與目的站選項皆讀取 SQLite。

## Demo 帳號

| 角色 | 帳號 | 密碼 | 權限 |
| --- | --- | --- | --- |
| 工作人員 | `worker01` | `demo1234` | 只顯示板橋區、新莊區的地圖、站點預測、調度建議與本人任務 |
| 系統管理員 | `admin01` | `demo1234` | 檢視全部行政區、站點、任務與工作人員回報 |

## 資料庫

首次啟動時，`server.js` 會建立 `dispatch-demo.sqlite` 並寫入 Demo 資料。資料庫保留帳號、站點與任務三類資料，管理員頁透過 `/api/login` 與 `/api/dashboard` 讀取資料。

工作人員任務依 `PUBLISHED → ACCEPTED → PICKED_UP → COMPLETED` 推進。完成視窗可調整實際搬運量、選擇差異原因，並獨立回報故障車數與備註。管理員畫面同步顯示任務編碼、經過時間、提醒次數與回報。

站點與任務預設依嚴重程度排序。安全水位為 30–70%；系統比較 30 與 60 分鐘預測可借率，取偏離安全水位最大的幅度作為嚴重度。可使用上一頁、下一頁查看任務。

目前站點資料標示為 `DEMO_MOCK`。即時 1,532 站、LightGBM 模型、道路服務與 AWS 尚需外部資料／憑證，詳細差距見 `SPEC_IMPLEMENTATION_AUDIT.md`。
