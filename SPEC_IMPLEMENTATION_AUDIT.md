# 規格整合結果

本文件依 `UBIKE_SYSTEM_SPEC.md` 與 `CODEX_REPO_AUDIT_PROMPT.md` 比對目前 Demo。狀態以實際程式與 API 驗證為準。

## A. Repository 現況

- 技術棧：Node.js 內建 HTTP、SQLite、HTML、CSS、JavaScript、Leaflet。
- 啟動方式：在 `調度系統Demo` 執行 `npm start`，開啟 `http://localhost:3000`。
- 主要資料流：SQLite → Node API → 管理員／調度人員頁面。
- 登入狀態使用 `sessionStorage.dispatchUser`；站點與任務皆由 API 讀取，完成任務不會清除站點資料。

## B. 規格對照

| 規格項目 | 狀態 | 程式證據 | 缺漏／衝突 | 建議位置 | 優先級 |
| --- | --- | --- | --- | --- | --- |
| 30%～70% 安全水位 | 已完成 | `public/staff.js` 的 `riskOf`、`SYSTEM_CONFIG` | 無 | 既有設定 | P0 |
| 任務後端保存 | 已完成 | `server.js` 的 SQLite `tasks` | 本機 SQLite，尚非 DynamoDB | AWS adapter | P0 |
| 四階段狀態 | 已完成 | accept、pickup、complete API | 無 | `server.js` | P0 |
| 狀態時間 | 已完成 | published/accepted/picked_up/completed 欄位 | 無 | `tasks` | P0 |
| 完成回報與故障車 | 已完成 | complete API、`fault-report` | 無車輛編號 | 完成回報頁 | P0 |
| 站點完成後不歸零 | 已完成 | complete API 只更新 tasks | 無 | API 測試 | P0 |
| 多站 8＋6 | 已完成 | `task_destinations`、BQ-009 | 管理端尚無任意新增多站表單 | 派工表單 | P1 |
| 最後更新與站點數 | 已完成 | dashboard `meta`、前端 data-meta | Demo 只顯示責任區 10 站 | realtime adapter | P0 |
| 提醒紀錄 | 部分完成 | `/remind`、`reminder_count` | 真實電話服務未串接 | notification service | P1 |
| LightGBM 推論 | 無法確認 | Repository 無 `.pkl`、訓練碼及 metadata | 無法驗證 feature list | inference service | P0 |
| 每 10 分鐘即時資料 | 未完成 | 目前 `DEMO_MOCK` | 無來源 CSV/API | realtime adapter | P0 |
| 道路最佳化 | 部分完成 | `routeEstimate` | 仍為示意，非道路服務／OR-Tools | optimization service | P1 |
| AWS | 未完成 | 無 Amplify、Lambda、DynamoDB、S3、EventBridge 設定 | 權限與帳號待確認 | infra | P0/P1 |

## C. 首頁歸零問題

目前完成 API 僅更新 `tasks` 的狀態與回報欄位，不修改 `stations`，前端完成後重新呼叫 staff dashboard。實測完成 BQ-009 前後責任區站點皆為 10 筆，因此未重現歸零。

## D. 即時資料與模型

目前站點欄位為 `id/district/name/bikes/slots/forecast30/forecast60/status/lat/lng`。尚需 adapter 映射規格的 `observed_at/station_id/station_name/address/total_slots/available_bikes/available_docks`。Repository 沒有可信 `.pkl`、feature list、類別編碼或 lag 資料，不能聲稱已完成模型推論。

## E. 任務與最佳化

後端可保存一個來源站、多個目的站、每站計畫量、14 台容量、四種狀態與時間。BQ-009 保存兩站 8＋6。尚缺車上剩餘量、25 台車、警示去重鎖定及真實道路時間。

## F. AWS 差距

Amplify、API Gateway、Lambda、DynamoDB、S3、EventBridge、推論服務與 Bedrock 目前皆未出現在 Repository。最小順序為：Amplify → API Gateway/Lambda/DynamoDB → S3 即時快照 → EventBridge → 模型推論 → 最佳化 → Bedrock 摘要。

## G. 後續計畫

- P0：接入即時資料 adapter、資料品質檢查、LightGBM 相容層、API 失敗保留最後成功資料。
- P1：候選警示去重、多站派工編輯器、車上載量、OR-Tools／道路服務、管理端任務詳細頁。
- P2：Bedrock 說明、真實 AI 電話、25 台車與跨區支援。
