# Sprint 4 — Indexes and `GET /admin/explain/{endpoint}`

## Index inventory

All indexes are created idempotently in `ensure_indexes()` at startup (and on first successful `/health` / write).
Before unique indexes are built, `find_duplicate_natural_keys()` scans for duplicate `emp_code` or
`(emp_code, date)` and **aborts** index creation if any exist (data is never deleted or rewritten).

| Collection | Name | Keys | Unique | Serves |
|---|---|---|---|---|
| `employees` | `uniq_emp_code` | `emp_code` ↑ | yes | Identity, POST employee / punch-in races, leaderboard `$lookup` |
| `employees` | `dept_emp_code` | `department` ↑, `emp_code` ↑ | no | GET `/employees?department=`, trend headcount by department |
| `employees` | `emp_joined_on` | `joined_on` ↑ | no | Department summary R9 (`joined_on <= month_end`) |
| `employees` | `dept_joined_on` | `department` ↑, `joined_on` ↑ | no | Summary + trend with department + join date bound |
| `attendance_logs` | `uniq_emp_date` | `emp_code` ↑, `date` ↑ | yes | One row per day; punch-in race; PATCH by natural key |
| `attendance_logs` | `date_emp_list` | `date` ↓, `emp_code` ↑ | no | GET `/attendance` sort + pagination; month scans (leaderboard) |
| `attendance_logs` | `status_date_emp` | `status` ↑, `date` ↓, `emp_code` ↑ | no | List with `status` filter + same sort |
| `attendance_logs` | `emp_punch_in` | `emp_code` ↑, `punch_in` ↓ | no | Punch-out: latest open `punch_in` |
| `attendance_logs` | `date_emp_asc` | `date` ↑, `emp_code` ↑ | no | Monthly per-employee pipeline; ascending month walks |
| `attendance_locks` | `ttl_created_at` | `created_at` ↑ (TTL 60s) | no | PATCH regularize mutex cleanup |

**Rejected / not added**

- Extra single-field `{date:1}` — redundant with `date_emp_list` / `date_emp_asc`.
- Text index on `name` — not used by any contract query.

**Trade-offs**

- Two date sort indexes (`date_emp_list` desc vs `date_emp_asc` asc) cost write amplification but match the two
  access patterns (list newest-first vs analytics range).
- `status_date_emp` is only worth it when status-filtered list queries are common; kept because the contract
  explicitly supports `?status=`.

## Explain endpoint

`GET /admin/explain/{endpoint}` builds the **same** filter/pipeline as the real handler, then runs MongoDB
`explain` with `verbosity=executionStats`:

| `endpoint` | Mechanism | `collection` in response |
|---|---|---|
| `attendance_list` | `explain` command on `find` + sort + skip + limit | `attendance_logs` |
| `employee_monthly` | `explain` on `attendance_logs.aggregate(pipeline)` | `attendance_logs` |
| `department_summary` | `explain` on `employees.aggregate(pipeline)` | `employees` |
| `late_leaderboard` | `explain` on `attendance_logs.aggregate(pipeline)` | `attendance_logs` |
| `department_trend` | `explain` on database `aggregate` (pipeline uses `$documents`) | `attendance_logs` |

Shared builders: `attendance_list_filter`, `build_employee_monthly_pipeline`, `build_department_summary_pipeline`,
`build_late_leaderboard_pipeline`, `build_department_trend_pipeline`.

Response shape: `{ "endpoint", "collection", "explain": <raw MongoDB explain JSON> }`.

## Verified plans (local MongoDB 8.0.12)

On sample + synthetic data, main driving stages use **IXSCAN** (e.g. `date_emp_list` for month
`$match` on leaderboard; `uniq_emp_date` / `date_emp_asc` for employee monthly). Department trend explain
shows `indexesUsed: ["dept_joined_on", "date_emp_list", "uniq_emp_code"]` on `$lookup` sub-pipelines.

At ~5k attendance rows, `late_leaderboard` explain still reports IXSCAN on `date_emp_list` with bounded
`indexBounds` for the month (see `tests/test_sprint4_admin.py`).

## Tests

```text
python -m unittest tests.test_sprint4_admin tests.test_sprint2 tests.test_sprint2_attendance
Ran 74 tests … OK
```

`tests/test_sprint4_admin.py`: index presence, idempotent `ensure_indexes`, duplicate-key probe, all five explain
identifiers, required-parameter 422s, IXSCAN / no top-level COLLSCAN on list + leaderboard (sample and 5k-row case).
