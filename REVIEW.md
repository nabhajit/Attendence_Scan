# REVIEW.md

Starter defects found in the original `app/main.py` (helpers and the five provided endpoints). Each row is a **confirmed** contract deviation in the starter, not later feature work.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace used `minutes > 10` **after** `int(seconds / 60)`. At 09:40:01 (601s past 09:30) that is `int(10.016) = 10`, which is not `> 10`, so it returned `0`. R2 requires late when punch-in is **strictly more than** 10:00, then `floor` minutes **from shift start** (`09:40:01` → `10`). | Helper test: punch 09:40:01 IST with shift `09:30` returned `0`. Contract example: `09:40:00` → `0`, `09:40:01` → `10`, `10:15:59` → `45`. | Compare raw seconds with `> 600` first, then `int(seconds // 60)`. |
| 2 | `compute_late_minutes` | Built shift start with `punch_in.replace(hour=h, minute=m)` on the punch-in's calendar day. Overnight shifts that punch after midnight (shift `22:00–06:00`, punch `00:30`) treated start as **22:00 the same night**, so late was negative/`0` instead of minutes since the previous day's 22:00. | Punch EMP0005 at `2026-07-21 00:30` IST. Old code: date `2026-07-21`, `late_minutes = 0`. Correct: date `2026-07-20`, `late_minutes = 150`. | Compute attendance `date` with R1, then measure late against `shift_start` on **that** IST date. |
| 3 | Helpers / punch-in | R1 says truncate instants to **whole seconds** before calc/storage/response. `09:40:00.900` must count as `09:40:00` (still inside grace). Starter kept microseconds. | Punch at `09:40:00.900` IST: stored/returned sub-second fraction; grace math wrong at boundary. | `truncate_to_seconds()` on inbound punches; API returns `ms % 1000 == 0`. |
| 4 | `compute_work_hours` | Used Python `round(..., 2)` (banker's / round-half-even). `8.125` hours → `8.12`. R4 requires half-up → `8.13`. | `compute_work_hours` for a 29250s span returned `8.12`. | `Decimal` + `ROUND_HALF_UP` to 2 places. |
| 5 | `compute_overtime` | Returned every positive minute after shift end. R3: overtime is `0` unless floor minutes **≥ 30**. | Punch-out 18:35 with shift end 18:30 → `5` instead of `0`. | `minutes = floor(delta); return minutes if minutes >= 30 else 0`. |
| 6 | `compute_overtime` | Built shift end on `date_str` with no overnight bump. For `22:00–06:00` on `2026-07-06`, end must be **06:00 on the 7th**. | Sample EMP0005: out `06:40` IST next day should be `40` OT, not ~24h. | If `shift_end <= shift_start`, add one day to shift-end datetime. |
| 7 | `EmployeeIn` | No contract validation: `emp_code` `^EMP\d{4,6}$`, email pattern, lengths, `HH:MM` shifts, `joined_on` date, **shift_start ≠ shift_end**. | Invalid bodies got `201`. | Pydantic `Field`/`pattern`/`min_length` plus model validator → `422`. |
| 8 | `PunchInIn` | `punched_at` accepted floats, second-scale epochs, and out-of-range ints. `status` accepted `ABSENT`/`LEAVE`. | Seconds/float epoch or `status: ABSENT` → `201`/`500`. | `StrictInt` bounds; `Literal` presence statuses only. |
| 9 | `GET /health` | Always `{"status":"ok"}` with no Mongo ping. Contract: `503` if Mongo does not respond. | Mongo stopped; endpoint still `200`. | `admin.command("ping")`; `PyMongoError` → `503`. |
| 10 | `create_employee` | `created_at = datetime.now()` naive local time, not UTC. | Skew vs UTC on non-UTC hosts. | `datetime.now(timezone.utc)`. |
| 11 | `create_employee` response | `created_at` as ISO datetime, not epoch ms. | Response string instead of integer ms. | `serialize_employee()`. |
| 12 | `create_employee` | Check-then-insert without unique index; concurrent duplicate `emp_code` both `201`. | 8 parallel creates with same code. | Unique `uniq_emp_code`; `DuplicateKeyError` → `409`. |
| 13 | `list_employees` | `skip = page * page_size` (off-by-one on page 1). | Page 1 skipped first 20 rows. | `skip = (page - 1) * page_size`. |
| 14 | `list_employees` | `total` ignored department filter. | Filtered list but `total` = whole collection. | `count_documents(q)`. |
| 15 | `list_employees` | No sort by `emp_code`. | Unstable order. | `.sort([("emp_code", ASCENDING)])`. |
| 16 | `list_employees` | `created_at` not epoch ms. | ISO string in JSON. | Shared employee serializer. |
| 17 | `punch_in` | Unknown `emp_code` → `TypeError` / `500`. | `EMP9999` punch-in. | `404 employee not found`. |
| 18 | `punch_in` | Local-time `fromtimestamp` / `now()`. | Host TZ affects stored instants. | UTC-aware client and timestamps. |
| 19 | `punch_in` | Wrong `date` (no IST / overnight R1). | Overnight punch wrong calendar day. | `attendance_date()`. |
| 20 | `punch_in` | Check-then-insert on `(emp_code, date)` without unique index. | Concurrent duplicate punch-in. | Unique `uniq_emp_date`; `409`. |
| 21 | `punch_in` | Exposed Mongo `id` in response. | Extra `id` field. | AttendanceRecord schema only. |
| 22 | `punch_in` | `punch_in` as ISO datetime. | Not epoch ms. | `serialize_attendance()`. |
| 23 | `list_attendance` | `list(find(q))` then sort/slice in Python. | OOM/latency at ~100k rows. | Mongo sort + skip + limit. |
| 24 | `list_attendance` | Sort `date` only. | Missing `emp_code` tie-break. | Compound sort date↓, emp_code↑. |
| 25 | `list_attendance` | Exposed `_id` as `id`. | Extra `id` field. | `{_id:0}` + serializer. |
| 26 | `list_attendance` | No `date_from > date_to` check. | Invalid range → `200`. | `422`. |
| 27 | `list_attendance` | History instants not epoch ms in API. | Nested ISO datetimes. | `serialize_history_entry`. |
| 28 | `list_attendance` | Legacy missing `history` / `half_day`. | Schema gaps on old rows. | Defaults in serializer. |
| 29 | Startup | No indexes. | COLLSCAN; races not enforced. | `ensure_indexes()` (see `tests/SPRINT4.md`). |
| 30 | Pagination query params | No `page`/`page_size` bounds. | `page=0` or `page_size=101`. | FastAPI `Query` limits. |
| 31 | `list_attendance` `status` | Any string accepted. | `?status=LATE` → `200`. | `Literal` status enum. |

## Investigated, not starter defects

- **`GET /attendance` skip** was already correct; only **`GET /employees`** was off-by-one.
- **Punch-in defaults** (presence status, null punch-out/work_hours, empty history) matched the contract in the starter happy path.
- **`load_dotenv()`** does not override real environment variables.
- **Collection names and BSON shapes** in the starter matched `sample_data/DATA_MODEL.md`.
- **Department filter** was already exact and case-sensitive.
- **Banker's `round`** on sample 9.12/9.08 values matched half-up by luck; defect #4 still matters for `8.125` → `8.13`.

## Post-starter QA (full application)

Automated suites use **dedicated `MONGO_DB` names** per module (`attendance_sprint2_test`, `attendance_s3_analytics`, etc.) so a local `attendance_db` seed is not modified.

| Suite | Focus |
|---|---|
| `tests/test_sprint2.py` | Starter fixes: helpers, health, employees, punch-in, list attendance, concurrency, indexes |
| `tests/test_sprint2_attendance.py` | Punch-out, regularize, R1–R5 boundaries, concurrent punch-out/PATCH, audit history |
| `tests/test_sprint3_analytics.py` | Monthly/dept/leaderboard/trend vs independent Python expectations |
| `tests/test_sprint4_admin.py` | Index idempotency, explain for all five endpoint ids, IXSCAN on list/leaderboard |
| `tests/test_qa_health.py` | Health `200` live; `503` with mocked `PyMongoError` |

**Last full run (this environment):**

```text
python -m unittest discover -s tests -p "test_*.py"
Ran 113 tests — OK

python -m pytest -c tests/pytest.ini tests/ -q
113 passed
```

## OpenAPI alignment audit (post-implementation)

| Area | Finding |
|------|---------|
| **Paths / methods** | All 13 operations in `openapi.yaml` are implemented; admin explain enum matches the five analytics/list ids. |
| **Status codes** | 404 (unknown employee, no punch-in, no attendance row, unknown department); 409 (duplicate emp, punch-in/out, concurrent regularize); 422 (validation, date range, no-op PATCH, punch windows) match contract tests in `test_sprint2*.py`, `test_sprint3_analytics.py`, `test_openapi_contract.py`. |
| **Epoch ms** | `StrictInt` + min/max on punch/regularize; responses use `dt_to_epoch_ms`; history nested times serialized as ms; BSON dates never leak to JSON. |
| **No Mongo `_id`** | List/find projections use `{"_id": 0}`; serializers omit `_id`; `test_openapi_contract` walks API payloads (not raw explain plans, which may mention projection `_id: 0`). |
| **Defaults** | `page=1`, `page_size=20`, leaderboard `limit=10`, punch `status=PRESENT`, shift defaults on create. |
| **Analytics** | Leaderboard competition ranking + limit-after-rank; department summary R9 headcount; trend gap-fill via `$documents`; stored derived fields only. |
| **Gaps / risks** | Lock TTL (60s) not time-tested in CI; MongoDB **7.0** explain not run locally (no Docker here); grader hidden data still unknown. |

**Debugging note:** `test_zero_late_excluded` originally asserted EMP0007 was absent, but the synthetic fixture assigns `late_minutes=5` on `2026-08-03`; the test was corrected to match the fixture (root cause: test expectation, not API code).

**Pytest / HTTPX:** `TestClient` (Starlette/httpx) drives HTTP integration tests; no live uvicorn process. Health `503` is asserted by calling `health()` with a mocked `client` (`test_qa_health.py`).

## Unresolved limitations

- **100k explain (manual):** `devtools/explain_at_scale.py` (local only, gitignored) seeds **100k** rows across 100 employees and runs all five `run_admin_explain` paths with production params (`month=2024-06`, `emp_code=EMP0001`, `department=Engineering`, `from`/`to` trend window, list pagination). On MongoDB 8.0.12: **IXSCAN** / `indexesUsed`, **no COLLSCAN**; `totalDocsExamined` capped at **20** (list), **30** (monthly), **100** (summary employees), **3000** (leaderboard month), trend lookups use `dept_joined_on` / date indexes (not a full `attendance_logs` scan). Automated CI still uses **5k** for `late_leaderboard` only (`LargeDatasetExplainTests`).
- **`department_trend` explain** uses database-level `aggregate` (required for `$documents`); plan shape differs from collection explains but reports `indexesUsed` on `$lookup` sub-pipelines.
- **Unique index creation** aborts if duplicates already exist in the DB (by design; data is never rewritten).
- **Regularize concurrency** uses a lock collection (`attendance_locks`), not multi-document transactions (standalone MongoDB cannot use transactions).
- **Shutdown hooks** are FastAPI defaults only; no custom teardown beyond releasing PATCH locks in `finally`.
