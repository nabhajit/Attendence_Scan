"""
Sprint 2 tests for the two attendance-lifecycle endpoints:

  * POST   /attendance/punch-out
  * PATCH  /attendance/{emp_code}/{date}   (manual regularization)

Uses a dedicated MongoDB database. Pure helper math (R1-R5) is exercised through
the endpoints so the whole lifecycle is covered end to end.
"""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MONGO_DB"] = "attendance_s2_punch_test"

from bson import json_util  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    IST,
    app,
    compute_overtime,
    compute_work_hours,
    db,
    ensure_indexes,
)


def ms_ist(y, mo, d, h, mi, s=0):
    """Epoch milliseconds for an IST wall-clock instant."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


class PunchOutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        ensure_indexes()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        opts = json_util.JSONOptions(tz_aware=True)
        here = ROOT / "sample_data"
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
            if docs:
                db[name].insert_many(docs)

    # ---- helpers --------------------------------------------------------- #
    def punch_in(self, emp, punched_at):
        return self.client.post(
            "/attendance/punch-in", json={"emp_code": emp, "punched_at": punched_at}
        )

    def punch_out(self, emp, punched_at):
        return self.client.post(
            "/attendance/punch-out", json={"emp_code": emp, "punched_at": punched_at}
        )

    # ---- tests ----------------------------------------------------------- #
    def test_normal_punch_out(self):
        # EMP0006 2026-07-07 is open in the seed.
        r = self.punch_out("EMP0006", ms_ist(2026, 7, 7, 18, 35))
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["emp_code"], "EMP0006")
        self.assertEqual(d["date"], "2026-07-07")
        self.assertEqual(d["status"], "PRESENT")
        self.assertEqual(d["punch_in"], ms_ist(2026, 7, 7, 9, 35))
        self.assertEqual(d["punch_out"], ms_ist(2026, 7, 7, 18, 35))
        self.assertEqual(d["work_hours"], 9.0)
        self.assertEqual(d["overtime_minutes"], 0)     # 5 min < 30
        self.assertEqual(d["half_day"], False)
        self.assertEqual(d["late_minutes"], 0)          # within grace, preserved
        self.assertEqual(d["history"], [])
        self.assertNotIn("id", d)
        self.assertNotIn("_id", d)

    def test_punch_out_persists_bson_and_truncates_seconds(self):
        self.assertEqual(self.punch_in("EMP0001", ms_ist(2026, 8, 9, 9, 0)).status_code, 201)
        punched_at = ms_ist(2026, 8, 9, 18, 0, 0) + 999  # sub-second part
        r = self.punch_out("EMP0001", punched_at)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["punch_out"] % 1000, 0)                 # truncated to whole seconds
        self.assertEqual(d["punch_out"], ms_ist(2026, 8, 9, 18, 0, 0))
        self.assertEqual(d["work_hours"], 9.0)                    # computed on truncated value
        # stored as a real BSON date
        raw = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-08-09"})
        self.assertIsInstance(raw["punch_out"], datetime)

    def test_punch_out_without_valid_punch_in_is_404(self):
        # Employee exists, has only an ABSENT record -> no open punch-in.
        db.employees.insert_one({
            "emp_code": "EMP8001", "name": "No Punch", "email": "np@example.com",
            "department": "QA", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        })
        db.attendance_logs.insert_one({
            "emp_code": "EMP8001", "date": "2026-08-01", "status": "ABSENT",
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        })
        r = self.punch_out("EMP8001", ms_ist(2026, 8, 1, 18, 0))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "no punch-in found")

    def test_punch_out_no_records_is_404(self):
        db.employees.insert_one({
            "emp_code": "EMP8002", "name": "Nobody", "email": "nb@example.com",
            "department": "QA", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        })
        r = self.punch_out("EMP8002", ms_ist(2026, 8, 1, 18, 0))
        self.assertEqual(r.status_code, 404)

    def test_punch_out_unknown_employee_is_404(self):
        r = self.punch_out("EMP9999", ms_ist(2026, 8, 1, 18, 0))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "employee not found")

    def test_duplicate_punch_out_is_409(self):
        self.assertEqual(self.punch_out("EMP0006", ms_ist(2026, 7, 7, 18, 35)).status_code, 200)
        r = self.punch_out("EMP0006", ms_ist(2026, 7, 7, 19, 0))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], "already punched out")

    def test_punch_out_before_punch_in_is_rejected(self):
        # Dedicated employee so no seeded record interferes with the selection.
        db.employees.insert_one({
            "emp_code": "EMP8031", "name": "Edge", "email": "edge@example.com",
            "department": "QA", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc),
        })
        self.assertEqual(self.punch_in("EMP8031", ms_ist(2026, 8, 2, 9, 0)).status_code, 201)
        # punched_at == punch_in: the record IS selected (punch_in <= punched_at) and the
        # window check rejects equality -> 422.
        self.assertEqual(self.punch_out("EMP8031", ms_ist(2026, 8, 2, 9, 0)).status_code, 422)
        # punched_at < punch_in: no record has punch_in <= punched_at -> "no punch-in found" 404.
        r = self.punch_out("EMP8031", ms_ist(2026, 8, 2, 8, 0))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "no punch-in found")
        # Rejections must leave the record open (no partial write).
        raw = db.attendance_logs.find_one({"emp_code": "EMP8031", "date": "2026-08-02"})
        self.assertIsNone(raw["punch_out"])

    def test_punch_out_more_than_24h_is_422(self):
        self.assertEqual(self.punch_in("EMP0003", ms_ist(2026, 8, 10, 9, 0)).status_code, 201)
        r = self.punch_out("EMP0003", ms_ist(2026, 8, 11, 9, 0, 1))  # 24h + 1s
        self.assertEqual(r.status_code, 422)

    def test_punch_out_exactly_24h_is_ok(self):
        self.assertEqual(self.punch_in("EMP0001", ms_ist(2026, 8, 10, 9, 0)).status_code, 201)
        r = self.punch_out("EMP0001", ms_ist(2026, 8, 11, 9, 0, 0))  # exactly 24h
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["work_hours"], 24.0)

    def test_overnight_punch_out_across_midnight(self):
        # EMP0005 overnight shift 22:00-06:00. Punch in 21:55 IST Sep 1 (date Sep 1).
        pr = self.punch_in("EMP0005", ms_ist(2026, 9, 1, 21, 55))
        self.assertEqual(pr.status_code, 201, pr.text)
        self.assertEqual(pr.json()["date"], "2026-09-01")
        # Punch out 06:40 IST Sep 2 -> crosses midnight.
        r = self.punch_out("EMP0005", ms_ist(2026, 9, 2, 6, 40))
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["date"], "2026-09-01")           # stays on start day
        self.assertEqual(d["work_hours"], 8.75)
        self.assertEqual(d["overtime_minutes"], 40)         # 40 min past 06:00 next day
        self.assertEqual(d["half_day"], False)

    def test_overnight_punch_in_after_midnight_previous_date(self):
        # Punch in 00:30 IST Sep 5 -> belongs to Sep 4 shift (before shift_end 06:00).
        pr = self.punch_in("EMP0005", ms_ist(2026, 9, 5, 0, 30))
        self.assertEqual(pr.status_code, 201, pr.text)
        self.assertEqual(pr.json()["date"], "2026-09-04")
        self.assertEqual(pr.json()["late_minutes"], 150)    # 00:30 - 22:00 = 2h30m
        r = self.punch_out("EMP0005", ms_ist(2026, 9, 5, 6, 0))
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["date"], "2026-09-04")
        self.assertEqual(d["work_hours"], 5.5)              # 00:30 -> 06:00
        self.assertEqual(d["overtime_minutes"], 0)          # exactly at shift end

    def test_overtime_below_30_vs_at_30(self):
        self.assertEqual(self.punch_in("EMP0001", ms_ist(2026, 8, 3, 9, 0)).status_code, 201)
        self.assertEqual(self.punch_in("EMP0003", ms_ist(2026, 8, 3, 9, 0)).status_code, 201)
        # 29 minutes past 18:30 -> 0
        r29 = self.punch_out("EMP0001", ms_ist(2026, 8, 3, 18, 59))
        self.assertEqual(r29.status_code, 200, r29.text)
        self.assertEqual(r29.json()["overtime_minutes"], 0)
        # 30 minutes past 18:30 -> 30
        r30 = self.punch_out("EMP0003", ms_ist(2026, 8, 3, 19, 0))
        self.assertEqual(r30.status_code, 200, r30.text)
        self.assertEqual(r30.json()["overtime_minutes"], 30)

    def test_half_day_boundary_below_at_above(self):
        # All four employees use a 09:30-18:30 shift; punch in at 10:00.
        cases = [
            ("EMP0001", ms_ist(2026, 8, 5, 14, 29, 24), 4.49, True),   # 4h29m24s -> 4.49
            ("EMP0003", ms_ist(2026, 8, 5, 14, 30, 0), 4.5, False),    # 4h30m    -> 4.50
            ("EMP0006", ms_ist(2026, 8, 5, 14, 30, 36), 4.51, False),  # 4h30m36s -> 4.51
            ("EMP0004", ms_ist(2026, 8, 5, 14, 29, 57), 4.50, False),  # rounds up to 4.50
        ]
        for emp, out_at, exp_hours, exp_half in cases:
            self.assertEqual(self.punch_in(emp, ms_ist(2026, 8, 5, 10, 0)).status_code, 201)
            r = self.punch_out(emp, out_at)
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()
            self.assertAlmostEqual(d["work_hours"], exp_hours, places=2, msg=emp)
            self.assertEqual(d["half_day"], exp_half, msg=emp)

    def test_punch_out_invalid_timestamp_float_seconds_range(self):
        self.assertEqual(self.punch_in("EMP0001", ms_ist(2026, 8, 1, 9, 0)).status_code, 201)
        float_r = self.client.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP0001", "punched_at": 1783312500000.5},
        )
        self.assertEqual(float_r.status_code, 422)
        seconds_r = self.client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 1783312500}
        )
        self.assertEqual(seconds_r.status_code, 422)
        small_r = self.client.post(
            "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": 999}
        )
        self.assertEqual(small_r.status_code, 422)

    def test_concurrent_punch_out_one_200_rest_409(self):
        self.assertEqual(self.punch_in("EMP0001", ms_ist(2026, 8, 12, 9, 0)).status_code, 201)
        payload = {"emp_code": "EMP0001", "punched_at": ms_ist(2026, 8, 12, 18, 0)}

        def do():
            with TestClient(app) as c:
                return c.post("/attendance/punch-out", json=payload).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = [f.result() for f in as_completed([pool.submit(do) for _ in range(8)])]
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(codes.count(409), 7, codes)
        self.assertEqual(db.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-08-12", "punch_out": {"$ne": None}}), 1)


class RegularizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        ensure_indexes()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        opts = json_util.JSONOptions(tz_aware=True)
        here = ROOT / "sample_data"
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
            if docs:
                db[name].insert_many(docs)

    def patch(self, emp, date, body):
        return self.client.patch(f"/attendance/{emp}/{date}", json=body)

    # ---- tests ----------------------------------------------------------- #
    def test_change_single_field_punch_in(self):
        # EMP0001 2026-07-06 : punch_in 09:28 -> 10:05 (35 late), punch_out 18:35 unchanged.
        r = self.patch("EMP0001", "2026-07-06", {
            "reason": "biometric glitch", "regularized_by": "hr.admin",
            "punch_in": ms_ist(2026, 7, 6, 10, 5),
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["status"], "PRESENT")               # unchanged
        self.assertEqual(d["punch_out"], ms_ist(2026, 7, 6, 18, 35))  # unchanged
        self.assertEqual(d["late_minutes"], 35)
        self.assertEqual(d["work_hours"], 8.5)                 # 18:35 - 10:05
        self.assertEqual(d["overtime_minutes"], 0)
        self.assertEqual(d["half_day"], False)
        entry = d["history"][0]
        self.assertEqual(set(entry["changes"].keys()), {"punch_in", "late_minutes", "work_hours"})
        self.assertEqual(entry["by"], "hr.admin")
        self.assertEqual(entry["reason"], "biometric glitch")
        self.assertIsInstance(entry["at"], int)
        self.assertEqual(entry["changes"]["punch_in"]["from"], ms_ist(2026, 7, 6, 9, 28))
        self.assertEqual(entry["changes"]["punch_in"]["to"], ms_ist(2026, 7, 6, 10, 5))

    def test_change_multiple_fields(self):
        # EMP0001 2026-07-06 : status -> WFH and punch_out -> 19:10 (OT 40).
        r = self.patch("EMP0001", "2026-07-06", {
            "reason": "manual correction", "regularized_by": "admin",
            "status": "WFH", "punch_out": ms_ist(2026, 7, 6, 19, 10),
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["status"], "WFH")
        self.assertEqual(d["punch_in"], ms_ist(2026, 7, 6, 9, 28))  # unchanged
        self.assertEqual(d["work_hours"], 9.7)                 # 19:10 - 09:28
        self.assertEqual(d["overtime_minutes"], 40)
        self.assertEqual(d["late_minutes"], 0)
        entry = d["history"][0]
        self.assertEqual(set(entry["changes"].keys()), {"status", "punch_out", "overtime_minutes", "work_hours"})
        # punch_out change serialized to epoch ms on both sides
        self.assertEqual(entry["changes"]["punch_out"]["from"], ms_ist(2026, 7, 6, 18, 35))
        self.assertEqual(entry["changes"]["punch_out"]["to"], ms_ist(2026, 7, 6, 19, 10))

    def test_no_actual_changes_is_422(self):
        # EMP0004 2026-07-07 WFH 09:30->13:00 ; resend identical punch_in.
        r = self.patch("EMP0004", "2026-07-07", {
            "reason": "duplicate request", "regularized_by": "admin",
            "punch_in": ms_ist(2026, 7, 7, 9, 30),
        })
        self.assertEqual(r.status_code, 422)
        # history must remain untouched
        raw = db.attendance_logs.find_one({"emp_code": "EMP0004", "date": "2026-07-07"})
        self.assertEqual(raw.get("history", []), [])

    def test_missing_legacy_history_is_created(self):
        # EMP0002 2026-07-14 is a legacy record with NO history field.
        before = db.attendance_logs.find_one({"emp_code": "EMP0002", "date": "2026-07-14"})
        self.assertNotIn("history", before)
        r = self.patch("EMP0002", "2026-07-14", {
            "reason": "fix punch time", "regularized_by": "admin",
            "punch_in": ms_ist(2026, 7, 14, 9, 0),
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(len(d["history"]), 1)
        self.assertEqual(d["work_hours"], 9.52)               # 18:31 - 09:00
        self.assertEqual(set(d["history"][0]["changes"].keys()), {"punch_in", "work_hours"})
        after = db.attendance_logs.find_one({"emp_code": "EMP0002", "date": "2026-07-14"})
        self.assertIn("history", after)
        self.assertEqual(len(after["history"]), 1)

    def test_absent_with_punch_times_is_422(self):
        self.assertEqual(
            self.patch("EMP0001", "2026-07-06", {
                "reason": "combined fields", "regularized_by": "admin",
                "status": "ABSENT", "punch_in": ms_ist(2026, 7, 6, 9, 28),
            }).status_code,
            422,
        )
        self.assertEqual(
            self.patch("EMP0001", "2026-07-06", {
                "reason": "combined fields", "regularized_by": "admin",
                "status": "LEAVE", "punch_out": ms_ist(2026, 7, 6, 18, 35),
            }).status_code,
            422,
        )

    def test_set_absent_clears_punches_and_recomputes(self):
        # EMP0001 2026-07-07 already has one history entry.
        r = self.patch("EMP0001", "2026-07-07", {
            "reason": "marked absent", "regularized_by": "admin", "status": "ABSENT",
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["status"], "ABSENT")
        self.assertIsNone(d["punch_in"])
        self.assertIsNone(d["punch_out"])
        self.assertIsNone(d["work_hours"])
        self.assertEqual(d["late_minutes"], 0)
        self.assertEqual(d["overtime_minutes"], 0)
        self.assertEqual(d["half_day"], False)
        entry = d["history"][-1]
        self.assertEqual(set(entry["changes"].keys()), {"status", "punch_in", "punch_out", "work_hours"})
        self.assertEqual(len(d["history"]), 2)                # original + new

    def test_presence_without_punch_in_is_422(self):
        # EMP0004 2026-07-08 is ABSENT (no punch_in). PRESENT w/o punch_in => 422.
        r = self.patch("EMP0004", "2026-07-08", {
            "reason": "needs punch in", "regularized_by": "admin", "status": "PRESENT",
        })
        self.assertEqual(r.status_code, 422)

    def test_punch_in_off_attendance_date_is_422(self):
        r = self.patch("EMP0001", "2026-07-06", {
            "reason": "wrong day punch", "regularized_by": "admin",
            "punch_in": ms_ist(2026, 7, 7, 10, 0),   # July 7, record date is July 6
        })
        self.assertEqual(r.status_code, 422)

    def test_punch_out_before_punch_in_is_422(self):
        r = self.patch("EMP0001", "2026-07-06", {
            "reason": "swap times", "regularized_by": "admin",
            "punch_out": ms_ist(2026, 7, 6, 8, 0),   # before 09:28 punch_in
        })
        self.assertEqual(r.status_code, 422)

    def test_punch_out_over_24h_is_422(self):
        r = self.patch("EMP0001", "2026-07-06", {
            "reason": "too far out", "regularized_by": "admin",
            "punch_out": ms_ist(2026, 7, 7, 10, 28),  # 25h after 09:28
        })
        self.assertEqual(r.status_code, 422)

    def test_record_not_found_is_404(self):
        r = self.patch("EMP0001", "2026-07-20", {
            "reason": "no such record", "regularized_by": "admin",
            "punch_in": ms_ist(2026, 7, 20, 9, 30),
        })
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "attendance record not found")

    def test_employee_not_found_is_404(self):
        r = self.patch("EMP9999", "2026-07-06", {
            "reason": "no such person", "regularized_by": "admin",
        })
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "employee not found")

    def test_punch_in_punch_out_history_bson_and_epoch_ms(self):
        # Change both punch times on a record that already has one history entry.
        r = self.patch("EMP0001", "2026-07-07", {
            "reason": "both times wrong", "regularized_by": "admin",
            "punch_in": ms_ist(2026, 7, 7, 9, 20), "punch_out": ms_ist(2026, 7, 7, 19, 10),
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        entry = d["history"][-1]
        self.assertEqual(set(entry["changes"].keys()),
                         {"punch_in", "punch_out", "work_hours", "overtime_minutes"})
        # API side: epoch ms integers, including nested from/to
        self.assertIsInstance(entry["at"], int)
        self.assertIsInstance(entry["changes"]["punch_in"]["from"], int)
        self.assertIsInstance(entry["changes"]["punch_in"]["to"], int)
        self.assertIsInstance(entry["changes"]["punch_out"]["from"], int)
        self.assertIsInstance(entry["changes"]["punch_out"]["to"], int)
        # Mongo side: real BSON dates nested in history
        raw = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
        raw_entry = raw["history"][-1]
        self.assertIsInstance(raw_entry["at"], datetime)
        self.assertIsInstance(raw_entry["changes"]["punch_in"]["from"], datetime)
        self.assertIsInstance(raw_entry["changes"]["punch_in"]["to"], datetime)
        self.assertIsInstance(raw_entry["changes"]["punch_out"]["from"], datetime)
        self.assertIsInstance(raw_entry["changes"]["punch_out"]["to"], datetime)

    def test_existing_history_preserved_one_entry_appended(self):
        # EMP0001 2026-07-07 has one history entry ("biometric glitch").
        r = self.patch("EMP0001", "2026-07-07", {
            "reason": "adjust punch in", "regularized_by": "shift.lead",
            "punch_in": ms_ist(2026, 7, 7, 9, 20),
        })
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(len(d["history"]), 2)
        self.assertEqual(d["history"][0]["reason"], "biometric glitch")   # untouched
        self.assertEqual(d["history"][0]["by"], "hr.admin")
        self.assertEqual(d["history"][1]["reason"], "adjust punch in")    # appended
        self.assertEqual(d["history"][1]["by"], "shift.lead")

    def test_concurrent_regularize_no_lost_history(self):
        # Record starts with one history entry. Fire two DIFFERENT corrections at once.
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        opts = json_util.JSONOptions(tz_aware=True)
        here = ROOT / "sample_data"
        for name in ("employees", "attendance_logs"):
            docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
            if docs:
                db[name].insert_many(docs)

        start_len = 1  # EMP0001 2026-07-07 seed has exactly one history entry.

        req_a = {"reason": "routeA correction", "regularized_by": "userA",
                 "punch_in": ms_ist(2026, 7, 7, 9, 20)}          # changes punch_in + work_hours
        req_b = {"reason": "routeB correction", "regularized_by": "userB",
                 "status": "WFH"}                                 # changes status only

        def do(body):
            with TestClient(app) as c:
                return c.patch("/attendance/EMP0001/2026-07-07", json=body).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(do, req_a), pool.submit(do, req_b)]
            codes = [f.result() for f in as_completed(futures)]

        num_200 = codes.count(200)
        num_409 = codes.count(409)

        # Contract: losers may get 409; nothing may be silently lost. Exactly one holds the lock
        # at a time, so a staggered request may also succeed. The invariant we can rely on is:
        #   * every accepted correction (200) appended exactly one history entry,
        #   * no correction was clobbered (stored fields match the set of applied changes).
        self.assertGreaterEqual(num_200, 1)
        self.assertLessEqual(num_200, 2)
        self.assertEqual(num_200 + num_409, 2, codes)

        final = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-07"})
        new_entries = final["history"][start_len:]
        self.assertEqual(len(new_entries), num_200)                 # no history lost, none fabricated
        self.assertEqual(final["history"][0]["reason"], "biometric glitch")  # original preserved

        reasons = {e["reason"] for e in new_entries}
        a_applied = "routeA correction" in reasons
        b_applied = "routeB correction" in reasons

        # No clobber: stored fields must be consistent with the union of applied changes.
        if a_applied:
            self.assertEqual(final["punch_in"], datetime(2026, 7, 7, 3, 50, tzinfo=timezone.utc))  # 09:20 IST
            self.assertAlmostEqual(final["work_hours"], 9.17, places=2)
        else:
            self.assertEqual(final["punch_in"], datetime(2026, 7, 7, 3, 58, tzinfo=timezone.utc))  # 09:28 unchanged
        self.assertEqual(final["status"], "WFH" if b_applied else "PRESENT")


class HelperRuleTests(unittest.TestCase):
    """A couple of direct rule checks (R3 threshold, R4 half-up) for completeness."""

    def test_overtime_threshold(self):
        from datetime import datetime as dt, timezone as tz
        out_29 = dt(2026, 8, 5, 13, 29, tzinfo=tz.utc)   # 18:59 IST
        out_30 = dt(2026, 8, 5, 13, 30, tzinfo=tz.utc)   # 19:00 IST
        self.assertEqual(compute_overtime(out_29, "09:30", "18:30", "2026-08-05"), 0)
        self.assertEqual(compute_overtime(out_30, "09:30", "18:30", "2026-08-05"), 30)

    def test_work_hours_half_up(self):
        from datetime import datetime as dt, timezone as tz
        p1 = dt(2026, 8, 5, 0, 0, 0, tzinfo=tz.utc)
        p2 = dt(2026, 8, 5, 8, 7, 30, tzinfo=tz.utc)     # 29250s = 8.125h
        self.assertEqual(compute_work_hours(p1, p2), 8.13)   # half-up, not 8.12


if __name__ == "__main__":
    unittest.main()
