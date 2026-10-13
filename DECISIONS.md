# DECISIONS.md

1. **Indexes.** I create unique `emp_code` and `(emp_code, date)` for race-safe writes; `date_emp_list` (date↓, emp_code↑) for attendance listing and month scans; `status_date_emp` when filtering by status; `date_emp_asc` and `uniq_emp_date` for per-employee month reads; `emp_punch_in` for punch-out; `emp_joined_on` and `dept_joined_on` for department summary/trend headcount; `dept_emp_code` for department employee lists. I rejected a redundant single-field `{date:1}` index. Startup runs idempotent `create_index` after a duplicate-key probe.

2. **Punch-in race.** Both requests call `insert_one`. MongoDB enforces `uniq_emp_date`; one insert wins (`201`), the other gets `DuplicateKeyError` → `409`. No check-then-insert.

3. **Leaderboard ties.** `$rank` on `total_late_minutes` only (competition ranking: 1,2,2,4). Filter `rank <= limit` after ranking, so `limit=1` can return three rows if three employees tie at rank 1. Rows sort by total desc, then `emp_code` asc.

4. **Headcount with zero logs.** Department summary `$match`es employees with `joined_on <= month_end`, `$lookup` month logs, then `$group` counts **employee rows**—empty `logs` still count toward `headcount`.

5. **100× data.** Add a monthly rollup collection updated on write; keep current indexes for operational APIs. Analytics would read rollups instead of scanning all logs each request.
