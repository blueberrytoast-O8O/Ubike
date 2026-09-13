# YouBike AI 智慧調度系統 — Integration Handoff
Date: 2026-09-13

## 1. Repo / Branch

Repo:
C:\Users\gkjk0\Documents\Ubike

GitHub:
https://github.com/blueberrytoast-O8O/Ubike.git

Current branch:
backend-integration

Important:
- Do NOT add `Microsoft/`
- Do NOT commit/push/merge unless explicitly requested
- Python model/data stay excluded from Git
- Current local merge commit:
  e75ceb4
- Python backend commit:
  db44284

---

# 2. Python Backend Status

Python algorithm core is COMPLETE.

Core modules:

1. backend/realtime_normalizer.py
2. backend/feature_engineering.py
3. backend/model_service.py
4. backend/alert_engine.py
5. backend/optimizer.py
6. backend/pipeline.py

Supporting modules:

- backend/history_adapter.py
- backend/station_reference.py
- backend/demo.py

Baseline:

192 tests passed
0 failures
0 errors

DO NOT rewrite Python core unless a proven integration bug requires it.

System flow:

Realtime CSV
→ realtime_normalizer
→ coordinate enrichment
→ feature engineering
→ model_service
→ alert_engine
→ optimizer
→ pipeline result

---

# 3. Model / Prediction Truth Constraints

30m / 60m model exists and runs.

Model:
- LightGBM
- 38 features
- 1577 station_name categories
- model feature called `station_id` actually contains station_name categorical value

Historical replay current official demo metrics:

Evaluated stations:
1422

30m:
MAE 0.8839
RMSE 1.3441

60m:
MAE 1.4853
RMSE 2.2169

Current September realtime prediction is BLOCKED because:
- no Jul/Aug/Sep continuous model history
- holiday calendar only covers through 2026-06-30

Therefore:
DO NOT claim current Sept data has real 30/60 prediction.

Realtime pipeline still works in degraded mode.

---

# 4. Current Sept Realtime Result

Latest realtime:
2026-09-12 22:30:29 +08:00

Normalized stations:
1532

Coordinate enrichment:
1532 / 1532 successful

Realtime alerts:
888

Prediction:
BLOCKED

Forecast alerts:
0

Optimizer candidate plans:
0 before vehicle state is supplied

Reason:
no_usable_vehicle

This is expected behavior.

---

# 5. Station Identity — CONFIRMED

Canonical official station identity:

Python:
official_station_id

SQLite:
station_code

Audit comparison:

1532 / 1532 current realtime station IDs matched exactly.

Therefore:

official_station_id ↔ station_code

DO NOT use:

- SQLite stations.id as official station ID
- Python model feature station_id as official station ID

SQLite stations.id is only a local surrogate key.

Python model station_id is actually station_name category.

Station names can differ and can be duplicated.

All formal cross-system station joins should use official_station_id / station_code.

---

# 6. Frontend / Node / SQLite Architecture

Frontend:
- login.html / login.js
- admin.html / admin.js
- staff.html / staff.js
- CSS / assets

Node:
server.js

SQLite:
dispatch-demo.sqlite

Original conceptual structure:

Browser
→ Node server.js
→ SQLite

Python backend is separate and not yet connected.

---

# 7. Data Contract Audit Completed

Major findings:

- Python core does not need rewriting.
- Python pipeline result is not yet ingested by Node.
- Existing SQLite forecast30 / forecast60 values are placeholders.
- Official station data currently has forecast30=forecast60=bikes.
- These MUST NOT be described as AI predictions.
- Existing frontend also recalculates risk / route logic.
- Formal risk/dispatch should eventually come from Python.
- Node task lifecycle and Python optimizer contract are not yet fully aligned.
- VehicleState table does not exist.
- Existing tasks.vehicle_capacity is NOT vehicle state.
- Frontend/task code still uses station names in many places.
- Some station names are duplicated.
- official_station_id must become canonical cross-system identity.

---

# 8. Integration Round 1

Goal:

Frontend ↔ Node ↔ SQLite

WITHOUT Python pipeline integration yet.

Before Round 1:

- `node --experimental-sqlite server.js` worked
- Node server:
  http://localhost:3000
- `/` originally returned `Not found`
- Live Server could visually open login page but login failed because API origin differed
- logo was broken
- Node 22.12 requires experimental sqlite flag

Codex Round 1 work was assigned to:

- properly serve frontend from Node
- `/` → login
- same-origin `/api/login`
- static CSS / JS / logo
- package.json start command with `--experimental-sqlite`
- avoid exposing entire repo
- prevent server startup from mutating existing business data
- admin/staff dashboard smoke checks
- preserve Python core
- preserve SQLite

Latest Codex result before quota interruption:

26 Node smoke checks PASSED.

Confirmed:
- both demo account logins passed
- dashboard checks passed
- static assets passed
- forbidden download paths passed
- SQLite SHA hash unchanged
- `npm.cmd start` successfully launches:
  http://localhost:3000
- Python regression still:
  192 passed

Codex was about to perform actual browser login/render check.

Therefore Round 1 is effectively near-complete.

Need next:
1. inspect `git diff`
2. inspect `git status`
3. manually verify browser:
   - http://localhost:3000
   - logo
   - admin login
   - worker login
   - dashboard rendering
4. record final Round 1 changed files

---

# 9. Demo Accounts

Worker:
worker01
demo1234

Admin:
admin01
demo1234

---

# 10. Node / SQLite Known Issues for Later Rounds

Do NOT solve all at once.

Known issues:

- task response `{id}` vs frontend expecting result.task.id
- frontend status labels vs DB status:
  PUBLISHED
  ACCEPTED
  PICKED_UP
  COMPLETED
- staff UI does not currently call pickup endpoint
- complete endpoint expects PICKED_UP
- task_destinations supports multi-stop, but frontend/API does not fully support it
- task name-based matching is ambiguous
- available_docks must not be blindly recalculated as slots-bikes
- importer mishandles legitimate zero with `||`
- no vehicle table
- no candidate-plan persistence
- no admin-confirmation persistence
- no Python pipeline ingestion API yet

---

# 11. Next Integration Rounds

## Round 1
Frontend ↔ Node ↔ SQLite

Status:
Almost complete.

## Round 2
Python pipeline ↔ Node

Goal:

Python pipeline
→ Node integration adapter
→ SQLite/API projection
→ Admin/Staff frontend

First priority:
- realtime stations
- pipeline_status
- stale
- prediction.status
- alerts

Do NOT initially auto-publish optimizer tasks.

Must correctly display:

prediction BLOCKED

instead of fake forecast values.

## Round 3
Optimizer candidate → task workflow

Need:
- Demo VehicleState
- candidate plan persistence
- admin confirmation
- task publish
- worker accept
- pickup
- complete
- multi-stop route handling

## Round 4
AWS integration / deployment / final demo hardening

---

# 12. Vehicle State Minimum Contract

Future Demo vehicle example:

vehicle_id
capacity
current_load
latitude
longitude
available
status
observed_at
data_source=demo_configuration
assigned_staff_id

Important:

No usable vehicle → zero candidate plans is correct.

Do not fabricate a vehicle from tasks.vehicle_capacity.

---

# 13. AWS Direction

AWS teammate may prepare skeleton infrastructure.

Possible services:

EventBridge
Lambda
S3
DynamoDB
API Gateway
Amplify

But do NOT claim AWS deployment is complete until actually tested.

Provisional DynamoDB entities:

StationState
Tasks
Staff / Assignments
VehicleState

Canonical station PK should use:

official_station_id

---

# 14. Critical Truth Rules

Never claim:

- Sept realtime has real 30/60 prediction
- synthetic data = model accuracy
- 30/70 thresholds are official government rules
- Haversine = real road travel time
- optimizer is globally optimal
- candidate plans auto-publish
- AI reminder is a real phone call unless implemented
- model has been validated for Sept production accuracy

Can claim:

- 30/60 models execute
- model has 38 features
- 1577 station categories verified
- realtime normalization works
- realtime alerts work
- historical replay works
- degraded mode works
- coordinates match 1532/1532
- admin confirmation is required
- Python regression baseline is 192 passed

---

# 15. Immediate Next Step

Because Codex quota may be exhausted, continue manually if necessary.

First inspect:

git --no-pager diff --stat
git status --short

Then manually test:

npm.cmd start

Browser:
http://localhost:3000

Verify:

1. login page loads
2. logo loads
3. admin01 / demo1234 login
4. admin dashboard renders
5. logout / return
6. worker01 / demo1234 login
7. staff dashboard renders

Then proceed to Integration Round 2:
Python → Node adapter.

Do not re-plan Python stages.
They are complete.