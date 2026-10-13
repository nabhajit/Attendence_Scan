Here is a structured summary of all findings gathered from inspecting the candidate kit (PROBLEM_STATEMENT.docx, openapi.yaml, DATA_MODEL.md, app/main.py, sample data, and requirements):

1. Assignment Scope & Constraints
Structure:
Part A (Review & Fix): Identify defects in starter code, fix them, document in 

REVIEW.md
.
Part B (Build): Implement the 7 missing endpoints (punch-out, regularize, 4 analytics endpoints, 1 admin explain endpoint).
Part C (Explain): Ensure admin/explain/{endpoint} reports index usage (IXSCAN) and answer the 5 architecture questions in 

DECISIONS.md
.
Architecture Rules:
Single-file implementation: All FastAPI code must live in 

app/main.py
.
No in-memory iteration on ~100k records: All analytics and listings must use MongoDB aggregation pipelines / indexes.
Automatic index creation: Must create required MongoDB indexes idempotently at application startup (lifespan/startup).
2. Starter Code Defect Findings (app/main.py)
A total of 30 distinct defects and contract deviations were identified across helpers, models, and starter endpoints:

Helpers & Calculation Logic
compute_late_minutes — Grace period logic: Checks minutes > 10 after integer division truncating seconds. At 09:40:01 (10m 1s past 09:30), it computes int(601/60) = 10, which fails > 10 and returns 0 instead of 10. Spec requires: strictly > 600s (10:00), then floor(seconds / 60).
compute_late_minutes — Overnight shift handling: Uses .replace(hour=h, minute=m) on punch-in date. Fails when punch-in crosses midnight for overnight shifts starting on the prior calendar day.
compute_work_hours — Rounding mode: Uses Python’s round(), which is banker's rounding (round-half-to-even), e.g. 8.125 becomes 8.12. Contract rule R4 mandates half-up rounding (8.13).
compute_overtime — Minimum threshold missing: Returns raw minutes without applying the 30-minute minimum threshold (Rule R3). Punching out 15 minutes late returns 15 instead of 0.
compute_overtime — Overnight shift date math: Builds shift-end datetime on date_str. In overnight shifts (shift_end <= shift_start), shift end is on the next day.
Instant Truncation missing (R1): Punch timestamps are not truncated to whole seconds before calculation.
Input Models & Schemas
Pydantic validations missing:
emp_code: No regex check (^EMP\d{4,6}$).
email: No email format validation.
punched_at: No range validation for epoch milliseconds (100000000000 to 4102444800000) and no rejection of floats.
status: No enum validation for allowed statuses (PRESENT, WFH, ON_DUTY on punch-in).
Endpoint: GET /health
Blind 200 OK: Always returns {"status": "ok"} without checking if MongoDB is alive. Contract specifies returning 503 if MongoDB does not respond to a ping.
Endpoint: POST /employees
Naive Datetime: Uses datetime.now() (local naive time) instead of UTC datetime.now(timezone.utc).
Response format mismatch: Returns created_at as a Python datetime object / string instead of integer epoch milliseconds.
Race condition (Check-then-insert): Checks find_one() before insert_one(). Concurrent requests can create duplicate emp_code records without unique database index enforcement.
Endpoint: GET /employees
Off-by-one pagination: Calculates skip = page * page_size. On page 1, skips 20 items (starts at item 21). Must be (page - 1) * page_size.
Unfiltered total count: Uses db.employees.count_documents({}), returning the total collection count instead of the filtered count (count_documents(q)).
Missing sort order: Returns documents in insertion order; contract requires sorting by emp_code ascending.
Type conversion: created_at is serialized as ISO string rather than epoch milliseconds.
Endpoint: POST /attendance/punch-in
Missing 404 on unknown employee: If emp_code does not exist, emp is None and accessing emp["shift_start"] raises an unhandled TypeError (HTTP 500) instead of HTTP 404.
Local time vs UTC: Converts epoch milliseconds using datetime.fromtimestamp(ts / 1000) without timezone, which defaults to system local time instead of UTC.
Incorrect calendar date derivation (R1): Uses ts.date().isoformat(). Needs conversion to IST (+05:30) and adjustment for overnight shifts before taking the date.
Check-then-insert concurrency: Can allow duplicate attendance records for the same employee and date.
Response contract leak: Sets doc["id"] = str(res.inserted_id). The OpenAPI contract does not include an id field in AttendanceRecord.
Timestamp serialization: Returns punch_in as datetime rather than epoch milliseconds.
Endpoint: GET /attendance
In-memory data loading: Executes list(db.attendance_logs.find(q)) loading all documents into Python memory before sorting and slicing. On 100k records this causes extreme latency and OOM.
Incomplete sort: Only sorts by date DESC; contract mandates compound sort: date DESC, emp_code ASC.
Exposes id field: Converts and returns _id as id, violating schema.
Date range validation: Missing validation for date_from <= date_to (should return 422 if invalid).
Nested fields serialization: punch_in, punch_out, and history[].at are not converted to epoch milliseconds.
Database Indexes
Zero startup indexes: Currently no indexes are created on startup. Need:
employees: unique { emp_code: 1 }
attendance_logs: unique { emp_code: 1, date: 1 }
attendance_logs: compound { date: -1, emp_code: 1 } and supporting indexes for analytics queries.
3. Core Business Rules Summary (R1–R10)
R1 (Timezone & Date): All times in IST (UTC+05:30). Record date is IST date of punch_in. For overnight shifts (shift_end <= shift_start), if punch-in IST time is before shift_end, date belongs to the previous day. All timestamps truncated to whole seconds.
R2 (Late Minutes): Grace period = 10m 00s. Late if punch-in > shift_start + 10:00. late_minutes = floor((punch_in - shift_start) / 60).
R3 (Overtime): floor((punch_out - shift_end) / 60). Minimum 30 minutes; otherwise 0.
R4 (Work Hours): (punch_out - punch_in) / 3600, rounded to 2 decimal places using half-up rounding. null if unclosed.
R5 (Half Day): work_hours < 4.50 flags half_day = True. Counts as 0.5 present day in analytics.
R6 (Presence Statuses): PRESENT, WFH, ON_DUTY count as present; ABSENT, LEAVE do not.
R7 (Working Days): Mon–Fri calendar days. For mid-month joiners, counted from joined_on. Weekend punches contribute to late/overtime totals, but not to standard working days.
R8 (Rounding): Half-up rounding everywhere (2 decimals for hours/counts, 4 decimals for rates like attendance_pct and moving_avg_7d).
R9 (Headcount): All employees joined on or before target date/month-end, even with zero logs.
R10 (Pagination): 1-indexed page, default page_size = 20, max 100. Total reflects matching filter set.
4. Remaining Endpoints to Implement (Part B & C)
POST /attendance/punch-out: Close open punch, compute hours, OT, half-day.
PATCH /attendance/{emp_code}/{date}: Regularization with audit trail in history and recomputing derived metrics.
GET /analytics/employees/{emp_code}/monthly: Monthly aggregation pipeline (working days, present days, overtime, etc.).
GET /analytics/departments/summary: Department rollup via $lookup between departments and logs.
GET /analytics/leaderboard/late: Dense ranking / standard competition ranking with ties (rank <= limit).
GET /analytics/departments/{department}/trend: Calendar date gap-filling ($densify / $range) and 7-day moving average.
GET /admin/explain/{endpoint}: Mirroring query execution plans to prove index coverage (IXSCAN).
5. Artifacts and Templates Status

sprint0_analysis.md
: Full detailed breakdown generated in the analysis phase.


REVIEW.md
: Blank template ready to be populated with the bug entries above.


DECISIONS.md
: Ready for architecture explanations.