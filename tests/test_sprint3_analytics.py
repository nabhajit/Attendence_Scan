"""
Sprint 3 analytics tests: monthly summary, department summary, late leaderboard,
department trend.

Each test seeds a small synthetic dataset into a dedicated database, computes the
contract's expected result INDEPENDENTLY in plain Python from the raw stored
derived fields (late_minutes, overtime_minutes, work_hours, half_day, status),
and compares against the API's aggregation output.
"""
import math
import os
import sys
import unittest
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["MONGO_DB"] = "attendance_s3_analytics"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, db, ensure_indexes  # noqa: E402

MONTH = "2026-08"
M_START, M_END = "2026-08-01", "2026-08-31"
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

# --------------------------------------------------------------------------- #
# Synthetic dataset (all stored derived values are explicit - analytics must READ them)
# --------------------------------------------------------------------------- #
EMPLOYEES = [
    {"emp_code": "EMP0001", "name": "Asha", "email": "a@x.com", "department": "Engineering",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"},
    {"emp_code": "EMP0002", "name": "Vikram", "email": "v@x.com", "department": "HR",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"},  # no logs
    {"emp_code": "EMP0003", "name": "Neha", "email": "n@x.com", "department": "Engineering",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-12"},  # mid-month
    {"emp_code": "EMP0004", "name": "Rohit", "email": "r@x.com", "department": "HR",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-09-01"},  # joins after
    {"emp_code": "EMP0005", "name": "Meera", "email": "m@x.com", "department": "Engineering",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"},  # weekend+late
    {"emp_code": "EMP0006", "name": "Karan", "email": "k@x.com", "department": "HR",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"},  # leave/absent
    {"emp_code": "EMP0007", "name": "Case", "email": "c@x.com", "department": "engineering",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},  # lowercase dept
    {"emp_code": "EMP0009", "name": "Legacy", "email": "l@x.com", "department": "Engineering",
     "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},  # missing half_day
]

def rec(emp, d, status, late=0, ot=0, work=None, half=False, *, omit_half=False, omit_late=False):
    r = {"emp_code": emp, "date": d, "status": status, "punch_in": None, "punch_out": None,
         "work_hours": work, "overtime_minutes": ot, "history": []}
    if not omit_late:
        r["late_minutes"] = late
    if not omit_half:
        r["half_day"] = half
    return r

ATTENDANCE = [
    # EMP0001
    rec("EMP0001", "2026-08-03", "PRESENT", late=0, ot=0, work=8.0, half=False),
    rec("EMP0001", "2026-08-04", "PRESENT", late=25, ot=40, work=9.0, half=False),
    rec("EMP0001", "2026-08-05", "WFH", late=0, ot=0, work=4.0, half=True),
    rec("EMP0001", "2026-08-06", "LEAVE"),
    rec("EMP0001", "2026-08-07", "ABSENT"),
    # EMP0003 (joined 08-12) - late=25 ties EMP0001 for leaderboard
    rec("EMP0003", "2026-08-12", "PRESENT", late=25, ot=0, work=8.0, half=False),
    rec("EMP0003", "2026-08-13", "PRESENT", late=0, ot=0, work=8.0, half=False),
    # EMP0005 - weekend record + a weekday record
    rec("EMP0005", "2026-08-08", "PRESENT", late=10, ot=30, work=6.0, half=False),  # Sat
    rec("EMP0005", "2026-08-10", "PRESENT", late=5, ot=0, work=7.0, half=False),   # Mon
    # EMP0006 - late=25 ties; leave + absent
    rec("EMP0006", "2026-08-03", "LEAVE"),
    rec("EMP0006", "2026-08-04", "ABSENT"),
    rec("EMP0006", "2026-08-05", "PRESENT", late=25, ot=0, work=8.0, half=False),
    # EMP0007 lowercase dept (late=5 -> appears on the leaderboard at the bottom)
    rec("EMP0007", "2026-08-03", "PRESENT", late=5, ot=0, work=8.0, half=False),
    # EMP0009 legacy: missing half_day on both records; one record missing late_minutes
    rec("EMP0009", "2026-08-03", "PRESENT", late=15, ot=0, work=6.0, omit_half=True),
    rec("EMP0009", "2026-08-05", "PRESENT", late=0, ot=0, work=6.0, omit_half=True, omit_late=True),
]

# --------------------------------------------------------------------------- #
# Independent expected calculators (plain Python, read stored derived fields only)
# --------------------------------------------------------------------------- #
def is_weekday(dstr):
    return date_cls.fromisoformat(dstr).weekday() < 5  # Mon-Fri

def weekdays_between(start_iso, end_iso):
    a = date_cls.fromisoformat(start_iso)
    b = date_cls.fromisoformat(end_iso)
    if a > b:
        return 0
    n = 0
    d = a
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n

def hu(value, places):
    """half-up for non-negative floats (mirrors the in-pipeline arithmetic)."""
    scale = 10 ** places
    return math.floor(value * scale + 0.5) / scale

def pct_round(numerator, denominator):
    """attendance_pct: Decimal half-up 4 places (mirrors round_half_up_float)."""
    q = (Decimal(str(numerator)) / Decimal(str(denominator)) * Decimal(100)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(q)

def avg_round(value):
    q = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(q)

def working_days_for(joined_on):
    start = max(joined_on, M_START)
    return weekdays_between(start, M_END)

def expected_monthly(emp):
    joined = emp["joined_on"]
    wd = working_days_for(joined)
    rows = [r for r in ATTENDANCE if r["emp_code"] == emp["emp_code"]
            and M_START <= r["date"] <= M_END]
    present = 0.0
    leave = 0
    late_count = 0
    total_late = 0
    total_ot = 0
    for r in rows:
        if r["status"] in PRESENCE and is_weekday(r["date"]):
            present += 0.5 if r.get("half_day", False) else 1.0
        if r["status"] == "LEAVE":
            leave += 1
        lm = r.get("late_minutes", 0)
        if lm and lm > 0:
            late_count += 1
        total_late += lm
        total_ot += r.get("overtime_minutes", 0)
    pct = None if wd == 0 else pct_round(present, wd)
    return {
        "emp_code": emp["emp_code"], "month": MONTH, "working_days": wd,
        "present_days": present, "leave_days": leave, "late_count": late_count,
        "total_late_minutes": total_late, "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }

def expected_dept_summary(department=None):
    emp_by_code = {e["emp_code"]: e for e in EMPLOYEES}
    depts = {}
    for e in EMPLOYEES:
        if e["joined_on"] > M_END:            # R9: joined by period end
            continue
        if department is not None and e["department"] != department:
            continue
        depts.setdefault(e["department"], []).append(e)
    items = []
    for dept, emps in depts.items():
        headcount = len(emps)
        present = leave = late_count = total_late = total_ot = on_duty = 0
        wh_values = []
        for e in emps:
            rows = [r for r in ATTENDANCE if r["emp_code"] == e["emp_code"]
                    and M_START <= r["date"] <= M_END]
            for r in rows:
                if r["status"] in PRESENCE and is_weekday(r["date"]):
                    present += 0.5 if r.get("half_day", False) else 1.0
                if r["status"] == "LEAVE":
                    leave += 1
                if r["status"] == "ON_DUTY":
                    on_duty += 1
                lm = r.get("late_minutes", 0)
                if lm and lm > 0:
                    late_count += 1
                total_late += lm
                total_ot += r.get("overtime_minutes", 0)
                if r["status"] in PRESENCE and r.get("work_hours") is not None:
                    wh_values.append(r["work_hours"])
        avg = None if not wh_values else avg_round(sum(wh_values) / len(wh_values))
        items.append({
            "department": dept, "headcount": headcount,
            "present_days": float(present), "avg_work_hours": avg,
            "late_count": late_count, "total_late_minutes": total_late,
            "leave_count": leave, "on_duty_count": on_duty,
        })
    items.sort(key=lambda x: x["department"])
    return items

def expected_leaderboard(limit, department=None):
    emp_by_code = {e["emp_code"]: e for e in EMPLOYEES}
    totals = {}
    for r in ATTENDANCE:
        if M_START <= r["date"] <= M_END:
            t = totals.setdefault(r["emp_code"], {"total": 0, "late_count": 0})
            t["total"] += r.get("late_minutes", 0)
            if r.get("late_minutes", 0) > 0:
                t["late_count"] += 1
    rows = []
    for ec, t in totals.items():
        if t["total"] <= 0:
            continue
        emp = emp_by_code.get(ec)
        if emp is None:
            continue
        if department is not None and emp["department"] != department:
            continue
        rows.append({"emp_code": ec, "total_late_minutes": t["total"], "late_count": t["late_count"]})
    # standard competition ranking: rank = 1 + #(people with strictly greater total)
    for row in rows:
        row["rank"] = 1 + sum(1 for o in rows if o["total_late_minutes"] > row["total_late_minutes"])
    kept = [r for r in rows if r["rank"] <= limit]
    kept.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
    return kept

def expected_trend(department, from_iso, to_iso):
    emps = [e for e in EMPLOYEES if e["department"] == department]
    codes = {e["emp_code"] for e in emps}
    from_d = date_cls.fromisoformat(from_iso)
    to_d = date_cls.fromisoformat(to_iso)
    days = [from_d + timedelta(days=i) for i in range((to_d - from_d).days + 1)]
    rows = []
    for d in days:
        dstr = d.isoformat()
        weekday = d.weekday() < 5
        headcount = sum(1 for e in emps if e["joined_on"] <= dstr)
        present = 0.0
        late_count = 0
        for r in ATTENDANCE:
            if r["date"] == dstr and r["emp_code"] in codes:
                if r["status"] in PRESENCE:
                    present += 0.5 if r.get("half_day", False) else 1.0
                if r.get("late_minutes", 0) > 0:
                    late_count += 1
        rate = hu(present / headcount, 4) if (weekday and headcount > 0) else None
        rows.append({"date": dstr, "is_working_day": weekday, "headcount": headcount,
                     "present_count": present, "late_count": late_count, "attendance_rate": rate})
    # moving avg over current + up to 6 preceding rows, non-null rates only
    for i, row in enumerate(rows):
        window = [rows[j]["attendance_rate"] for j in range(max(0, i - 6), i + 1)
                  if rows[j]["attendance_rate"] is not None]
        row["moving_avg_7d"] = hu(sum(window) / len(window), 4) if window else None
    return rows


class AnalyticsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        ensure_indexes()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        db.employees.insert_many([dict(e, created_at=None) for e in EMPLOYEES])
        db.attendance_logs.insert_many([dict(r) for r in ATTENDANCE])

    # print the weekday of the dates used once, for human sanity
    @classmethod
    def tearDownClass(cls):
        pass


class MonthlyTests(AnalyticsBase):
    def test_monthly_matches_independent(self):
        for emp in EMPLOYEES:
            exp = expected_monthly(emp)
            got = self.client.get(
                f"/analytics/employees/{emp['emp_code']}/monthly",
                params={"month": MONTH}).json()
            self.assertEqual(got["emp_code"], exp["emp_code"])
            self.assertEqual(got["month"], exp["month"])
            self.assertEqual(got["working_days"], exp["working_days"], emp["emp_code"])
            self.assertAlmostEqual(got["present_days"], exp["present_days"], msg=emp["emp_code"])
            self.assertEqual(got["leave_days"], exp["leave_days"], emp["emp_code"])
            self.assertEqual(got["late_count"], exp["late_count"], emp["emp_code"])
            self.assertEqual(got["total_late_minutes"], exp["total_late_minutes"], emp["emp_code"])
            self.assertEqual(got["total_overtime_minutes"], exp["total_overtime_minutes"], emp["emp_code"])
            if exp["attendance_pct"] is None:
                self.assertIsNone(got["attendance_pct"], emp["emp_code"])
            else:
                self.assertAlmostEqual(got["attendance_pct"], exp["attendance_pct"], places=4,
                                       msg=emp["emp_code"])

    def test_monthly_specific_values(self):
        r = self.client.get("/analytics/employees/EMP0001/monthly", params={"month": MONTH}).json()
        self.assertEqual(r["total_late_minutes"], 25)     # only Aug4 late
        self.assertEqual(r["total_overtime_minutes"], 40)  # only Aug4 OT
        self.assertEqual(r["leave_days"], 1)              # Aug6 LEAVE
        self.assertEqual(r["late_count"], 1)

    def test_midmonth_joiner_working_days(self):
        r = self.client.get("/analytics/employees/EMP0003/monthly", params={"month": MONTH}).json()
        exp_wd = weekdays_between("2026-08-12", M_END)
        self.assertEqual(r["working_days"], exp_wd)

    def test_joined_after_period_zero_working_days_null_pct(self):
        r = self.client.get("/analytics/employees/EMP0004/monthly", params={"month": MONTH}).json()
        self.assertEqual(r["working_days"], 0)
        self.assertIsNone(r["attendance_pct"])

    def test_no_logs_employee_present_zero(self):
        r = self.client.get("/analytics/employees/EMP0002/monthly", params={"month": MONTH}).json()
        self.assertEqual(r["present_days"], 0.0)
        self.assertEqual(r["attendance_pct"], 0.0)

    def test_weekend_excluded_from_present_but_counts_late_ot(self):
        r = self.client.get("/analytics/employees/EMP0005/monthly", params={"month": MONTH}).json()
        # Aug8 is a weekend: should NOT add present_days, SHOULD add late/ot.
        self.assertGreaterEqual(r["total_late_minutes"], 10)
        self.assertGreaterEqual(r["total_overtime_minutes"], 30)

    def test_legacy_missing_half_day_counts_full(self):
        r = self.client.get("/analytics/employees/EMP0009/monthly", params={"month": MONTH}).json()
        exp = expected_monthly({"emp_code": "EMP0009", "joined_on": "2026-01-01"})
        self.assertAlmostEqual(r["present_days"], exp["present_days"])

    def test_unknown_employee_404(self):
        r = self.client.get("/analytics/employees/EMP9999/monthly", params={"month": MONTH})
        self.assertEqual(r.status_code, 404)

    def test_bad_month_422(self):
        r = self.client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"})
        self.assertEqual(r.status_code, 422)


class DepartmentSummaryTests(AnalyticsBase):
    def test_full_summary_matches(self):
        exp = expected_dept_summary()
        got = self.client.get("/analytics/departments/summary", params={"month": MONTH}).json()
        self.assertEqual(got["month"], MONTH)
        self.assertEqual([i["department"] for i in got["items"]],
                         [i["department"] for i in exp])
        for g, e in zip(got["items"], exp):
            self.assertEqual(g["department"], e["department"])
            self.assertEqual(g["headcount"], e["headcount"], e["department"])
            self.assertAlmostEqual(g["present_days"], e["present_days"], msg=e["department"])
            self.assertEqual(g["late_count"], e["late_count"], e["department"])
            self.assertEqual(g["total_late_minutes"], e["total_late_minutes"], e["department"])
            self.assertEqual(g["leave_count"], e["leave_count"], e["department"])
            self.assertEqual(g["on_duty_count"], e["on_duty_count"], e["department"])
            if e["avg_work_hours"] is None:
                self.assertIsNone(g["avg_work_hours"])
            else:
                self.assertAlmostEqual(g["avg_work_hours"], e["avg_work_hours"], places=2,
                                       msg=e["department"])

    def test_case_sensitive_departments(self):
        got = self.client.get("/analytics/departments/summary", params={"month": MONTH}).json()
        depts = {i["department"] for i in got["items"]}
        self.assertIn("Engineering", depts)
        self.assertIn("engineering", depts)   # distinct, case-sensitive

    def test_joined_after_excluded_from_headcount(self):
        # EMP0004 (HR) joined 2026-09-01 -> excluded from August headcount.
        got = self.client.get("/analytics/departments/summary", params={"month": MONTH,
                            "department": "HR"}).json()
        self.assertEqual(got["items"][0]["headcount"], 2)  # EMP0002 + EMP0006 only

    def test_filter_single_department_matches(self):
        exp = expected_dept_summary("Engineering")
        got = self.client.get("/analytics/departments/summary",
                              params={"month": MONTH, "department": "Engineering"}).json()
        self.assertEqual(len(got["items"]), 1)
        e = exp[0]
        self.assertEqual(got["items"][0]["headcount"], e["headcount"])
        self.assertAlmostEqual(got["items"][0]["present_days"], e["present_days"])

    def test_bad_month_422(self):
        r = self.client.get("/analytics/departments/summary", params={"month": "08-2026"})
        self.assertEqual(r.status_code, 422)


class LeaderboardTests(AnalyticsBase):
    def test_leaderboard_ranks_and_ties(self):
        got = self.client.get("/analytics/leaderboard/late",
                              params={"month": MONTH, "limit": 10}).json()
        items = got["items"]
        exp = expected_leaderboard(10)
        self.assertEqual([i["emp_code"] for i in items], [i["emp_code"] for i in exp])
        self.assertEqual([i["rank"] for i in items], [i["rank"] for i in exp])
        self.assertEqual([i["total_late_minutes"] for i in items],
                         [i["total_late_minutes"] for i in exp])
        # three-way tie at rank 1, skip 2-3 to rank 4 (two-way tie), then skip 5 to rank 6.
        self.assertEqual([i["rank"] for i in items], [1, 1, 1, 4, 4, 6])
        self.assertEqual([i["emp_code"] for i in items],
                         ["EMP0001", "EMP0003", "EMP0006", "EMP0005", "EMP0009", "EMP0007"])

    def test_tie_at_limit_returns_more_than_limit(self):
        got = self.client.get("/analytics/leaderboard/late",
                              params={"month": MONTH, "limit": 1}).json()
        # 3 employees tie at rank 1 -> ranked <= 1 returns 3 rows even though limit=1.
        self.assertEqual(len(got["items"]), 3)
        self.assertTrue(all(i["rank"] == 1 for i in got["items"]))

    def test_ties_ordered_by_emp_code(self):
        got = self.client.get("/analytics/leaderboard/late",
                              params={"month": MONTH, "limit": 1}).json()
        codes = [i["emp_code"] for i in got["items"]]
        self.assertEqual(codes, sorted(codes))

    def test_limit_at_cutoff_includes_boundary_tier(self):
        # limit=4 -> rank<=4 returns the two rank-4 employees too (5 rows > limit).
        exp = expected_leaderboard(4)
        got = self.client.get("/analytics/leaderboard/late",
                              params={"month": MONTH, "limit": 4}).json()
        self.assertEqual([i["emp_code"] for i in got["items"]],
                         [i["emp_code"] for i in exp])
        self.assertEqual(len(got["items"]), 5)
        self.assertTrue(all(i["rank"] <= 4 for i in got["items"]))

    def test_department_scoped_ranking(self):
        got = self.client.get("/analytics/leaderboard/late",
                              params={"month": MONTH, "limit": 10, "department": "Engineering"}).json()
        exp = expected_leaderboard(10, "Engineering")
        self.assertEqual([i["emp_code"] for i in got["items"]], [i["emp_code"] for i in exp])
        self.assertTrue(all(i["department"] == "Engineering" for i in got["items"]))

    def test_zero_late_excluded(self):
        got = self.client.get("/analytics/leaderboard/late", params={"month": MONTH}).json()
        codes = [i["emp_code"] for i in got["items"]]
        self.assertNotIn("EMP0002", codes)   # no logs at all
        self.assertNotIn("EMP0004", codes)   # joined after August
        # EMP0007 has late=5 on 2026-08-03 in the fixture -> must appear (rank 6).
        self.assertIn("EMP0007", codes)


class TrendTests(AnalyticsBase):
    def test_full_trend_matches(self):
        dept = "Engineering"
        exp = expected_trend(dept, "2026-08-01", "2026-08-14")
        got = self.client.get(f"/analytics/departments/{dept}/trend",
                              params={"from": "2026-08-01", "to": "2026-08-14"}).json()
        self.assertEqual(got["department"], dept)
        self.assertEqual(len(got["items"]), 14)      # one row per calendar day
        for g, e in zip(got["items"], exp):
            self.assertEqual(g["date"], e["date"])
            self.assertEqual(g["is_working_day"], e["is_working_day"], e["date"])
            self.assertEqual(g["headcount"], e["headcount"], e["date"])
            self.assertAlmostEqual(g["present_count"], e["present_count"], msg=e["date"])
            self.assertEqual(g["late_count"], e["late_count"], e["date"])
            if e["attendance_rate"] is None:
                self.assertIsNone(g["attendance_rate"], e["date"])
            else:
                self.assertAlmostEqual(g["attendance_rate"], e["attendance_rate"], places=4,
                                       msg=e["date"])
            if e["moving_avg_7d"] is None:
                self.assertIsNone(g["moving_avg_7d"], e["date"])
            else:
                self.assertAlmostEqual(g["moving_avg_7d"], e["moving_avg_7d"], places=4,
                                       msg=e["date"])

    def test_weekend_rate_null_moving_avg_ignores_null(self):
        got = self.client.get("/analytics/departments/Engineering/trend",
                              params={"from": "2026-08-01", "to": "2026-08-09"}).json()
        for row in got["items"]:
            if not row["is_working_day"]:
                self.assertIsNone(row["attendance_rate"])

    def test_empty_range_after_period_zero_headcount(self):
        # A range entirely before any Engineering employee's join still lists days, headcount stays
        # and rates are handled. Use HR joined before; instead test a dept with no records.
        got = self.client.get("/analytics/departments/HR/trend",
                              params={"from": "2026-08-17", "to": "2026-08-21"}).json()
        self.assertEqual(len(got["items"]), 5)
        # HR has working-day headcount>0 and some records; rates must be numbers on weekdays.
        # (Independent check:)
        exp = expected_trend("HR", "2026-08-17", "2026-08-21")
        for g, e in zip(got["items"], exp):
            self.assertEqual(g["headcount"], e["headcount"], e["date"])

    def test_range_with_no_records_all_days_present(self):
        # 2026-08-17..21 has NO attendance records at all for anyone, but days must exist.
        got = self.client.get("/analytics/departments/Engineering/trend",
                              params={"from": "2026-08-17", "to": "2026-08-21"}).json()
        self.assertEqual([r["date"] for r in got["items"]],
                         ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"])
        for r in got["items"]:
            self.assertEqual(r["present_count"], 0.0)
            self.assertEqual(r["late_count"], 0)
            # weekdays have headcount>0 -> rate 0.0 (working day, no records)
            if r["is_working_day"]:
                self.assertEqual(r["attendance_rate"], 0.0)
                self.assertEqual(r["moving_avg_7d"], 0.0)
            else:
                self.assertIsNone(r["attendance_rate"])
                self.assertIsNone(r["moving_avg_7d"])

    def test_moving_avg_shorter_window_at_start(self):
        got = self.client.get("/analytics/departments/Engineering/trend",
                              params={"from": "2026-08-03", "to": "2026-08-03"}).json()
        self.assertEqual(len(got["items"]), 1)
        # first row uses only itself
        exp = expected_trend("Engineering", "2026-08-03", "2026-08-03")
        self.assertAlmostEqual(got["items"][0]["moving_avg_7d"], exp[0]["moving_avg_7d"], places=4)

    def test_unknown_department_404(self):
        r = self.client.get("/analytics/departments/Nope/trend",
                            params={"from": "2026-08-01", "to": "2026-08-05"})
        self.assertEqual(r.status_code, 404)

    def test_range_too_large_422(self):
        r = self.client.get("/analytics/departments/Engineering/trend",
                            params={"from": "2026-01-01", "to": "2026-06-30"})
        self.assertEqual(r.status_code, 422)

    def test_range_reversed_422(self):
        r = self.client.get("/analytics/departments/Engineering/trend",
                            params={"from": "2026-08-10", "to": "2026-08-01"})
        self.assertEqual(r.status_code, 422)

    def test_case_sensitive_department(self):
        # "engineering" (lowercase) dept is distinct from "Engineering".
        r1 = self.client.get("/analytics/departments/Engineering/trend",
                             params={"from": "2026-08-01", "to": "2026-08-03"})
        r2 = self.client.get("/analytics/departments/engineering/trend",
                             params={"from": "2026-08-01", "to": "2026-08-03"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertNotEqual(r1.json(), r2.json())


if __name__ == "__main__":
    unittest.main()
