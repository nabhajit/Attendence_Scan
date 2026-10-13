"""Sprint 2 tests for starter helpers and endpoints. Uses a dedicated MongoDB database."""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MONGO_DB"] = "attendance_sprint2_test"

from bson import json_util  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    IST,
    app,
    attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    db,
    ensure_indexes,
    epoch_ms_to_dt,
)


def ist_epoch_ms(year, month, day, hour, minute, second=0, microsecond=0) -> int:
    dt = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=IST)
    return int(dt.timestamp() * 1000)


class HelperTests(unittest.TestCase):
    def test_late_grace_exactly_ten_minutes_is_not_late(self):
        punch = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 0)

    def test_late_one_second_past_grace_floors_from_shift_start(self):
        punch = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 10)

    def test_late_truncates_fractional_seconds_before_grace_check(self):
        punch = datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc)  # 09:40:00.900 IST
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 0)

    def test_late_example_10_15_59(self):
        punch = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)  # 10:15:59 IST
        self.assertEqual(compute_late_minutes(punch, "09:30", "2026-07-06"), 45)

    def test_overnight_punch_after_midnight_uses_previous_date(self):
        punch = datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc)  # 00:30 IST on 7th
        self.assertEqual(attendance_date(punch, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(compute_late_minutes(punch, "22:00", "2026-07-06"), 150)

    def test_overnight_punch_before_midnight_keeps_same_date(self):
        punch = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)  # 21:55 IST
        self.assertEqual(attendance_date(punch, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(compute_late_minutes(punch, "22:00", "2026-07-06"), 0)

    def test_work_hours_half_up_not_bankers(self):
        punch_in = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
        punch_out = punch_in.replace()  # placeholder
        # 8.125 hours = 29250 seconds
        punch_out = datetime(2026, 7, 6, 8, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(compute_work_hours(punch_in, punch_out), 8.13)
        self.assertEqual(float(Decimal("8.125").quantize(Decimal("0.01"))), 8.12)  # bankers contrast

    def test_overtime_below_30_is_zero(self):
        punch_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)  # 18:35 IST, 5 min
        self.assertEqual(compute_overtime(punch_out, "09:30", "18:30", "2026-07-06"), 0)

    def test_overtime_thirty_minutes_counts(self):
        punch_out = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)  # 19:00 IST
        self.assertEqual(compute_overtime(punch_out, "09:30", "18:30", "2026-07-06"), 30)

    def test_overnight_overtime_uses_next_calendar_day(self):
        punch_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)  # 06:40 IST on 7th
        self.assertEqual(compute_overtime(punch_out, "22:00", "06:00", "2026-07-06"), 40)

    def test_epoch_ms_truncated_to_seconds(self):
        raw = ist_epoch_ms(2026, 7, 6, 9, 40, 0, 900000)
        truncated = epoch_ms_to_dt(raw)
        self.assertEqual(truncated.microsecond, 0)
        self.assertEqual(truncated.second, 0)


class EndpointTests(unittest.TestCase):
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

    def test_health_ok(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_create_employee_created_at_is_epoch_ms(self):
        body = {
            "emp_code": "EMP9001",
            "name": "Test User",
            "email": "test@example.com",
            "department": "QA",
            "joined_on": "2026-07-01",
        }
        res = self.client.post("/employees", json=body)
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertIsInstance(data["created_at"], int)
        self.assertGreaterEqual(data["created_at"], 100000000000)
        self.assertNotIn("id", data)
        self.assertNotIn("_id", data)

    def test_duplicate_employee_409(self):
        body = {
            "emp_code": "EMP0001",
            "name": "Asha Clone",
            "email": "clone@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        }
        res = self.client.post("/employees", json=body)
        self.assertEqual(res.status_code, 409)

    def test_invalid_emp_code_422(self):
        res = self.client.post(
            "/employees",
            json={
                "emp_code": "EMP12",
                "name": "Bad",
                "email": "bad@example.com",
                "department": "X",
                "joined_on": "2026-01-01",
            },
        )
        self.assertEqual(res.status_code, 422)

    def test_same_shift_start_and_end_422(self):
        res = self.client.post(
            "/employees",
            json={
                "emp_code": "EMP9002",
                "name": "Night",
                "email": "night@example.com",
                "department": "Support",
                "shift_start": "22:00",
                "shift_end": "22:00",
                "joined_on": "2026-01-01",
            },
        )
        self.assertEqual(res.status_code, 422)

    def test_employees_pagination_page_one_not_off_by_one(self):
        res = self.client.get("/employees", params={"page": 1, "page_size": 2})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 6)
        codes = [row["emp_code"] for row in data["items"]]
        self.assertEqual(codes, ["EMP0001", "EMP0002"])
        self.assertIsInstance(data["items"][0]["created_at"], int)

    def test_employees_filter_total_and_sort(self):
        res = self.client.get("/employees", params={"department": "Engineering"})
        data = res.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual([r["emp_code"] for r in data["items"]], ["EMP0001", "EMP0002"])

    def test_page_size_over_100_422(self):
        res = self.client.get("/employees", params={"page_size": 101})
        self.assertEqual(res.status_code, 422)

    def test_punch_in_unknown_employee_404(self):
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP9999", "punched_at": ist_epoch_ms(2026, 7, 20, 9, 30)},
        )
        self.assertEqual(res.status_code, 404)

    def test_punch_in_duplicate_date_409(self):
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": ist_epoch_ms(2026, 7, 6, 9, 28)},
        )
        self.assertEqual(res.status_code, 409)

    def test_punch_in_late_and_serialization(self):
        punched_at = ist_epoch_ms(2026, 7, 20, 9, 40, 1)
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": punched_at, "status": "WFH"},
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["date"], "2026-07-20")
        self.assertEqual(data["late_minutes"], 10)
        self.assertEqual(data["status"], "WFH")
        self.assertEqual(data["punch_out"], None)
        self.assertEqual(data["work_hours"], None)
        self.assertEqual(data["overtime_minutes"], 0)
        self.assertEqual(data["half_day"], False)
        self.assertEqual(data["history"], [])
        self.assertNotIn("id", data)
        self.assertIsInstance(data["punch_in"], int)
        # fractional seconds stripped: stored/returned whole seconds
        self.assertEqual(data["punch_in"] % 1000, 0)

    def test_punch_in_rejects_float_and_seconds_epoch(self):
        float_res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1783312500000.5},
        )
        self.assertEqual(float_res.status_code, 422)
        seconds_res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": 1783312500},
        )
        self.assertEqual(seconds_res.status_code, 422)

    def test_punch_in_rejects_absent_status(self):
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0001", "punched_at": ist_epoch_ms(2026, 8, 1, 9, 30), "status": "ABSENT"},
        )
        self.assertEqual(res.status_code, 422)

    def test_overnight_punch_in_after_midnight(self):
        res = self.client.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP0005", "punched_at": ist_epoch_ms(2026, 7, 21, 0, 30)},
        )
        self.assertEqual(res.status_code, 201, res.text)
        data = res.json()
        self.assertEqual(data["date"], "2026-07-20")
        self.assertEqual(data["late_minutes"], 150)

    def test_attendance_list_sort_pagination_and_no_id(self):
        res = self.client.get("/attendance", params={"page": 1, "page_size": 3})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 9)
        pairs = [(row["date"], row["emp_code"]) for row in data["items"]]
        self.assertEqual(pairs, sorted(pairs, key=lambda p: (-int(p[0].replace("-", "")), p[1])))
        self.assertNotIn("id", data["items"][0])
        self.assertIsInstance(data["items"][0]["punch_in"], (int, type(None)))
        legacy = next(row for row in self.client.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"])
        self.assertEqual(legacy["history"], [])
        self.assertEqual(legacy["half_day"], False)

    def test_attendance_date_from_after_date_to_422(self):
        res = self.client.get("/attendance", params={"date_from": "2026-07-10", "date_to": "2026-07-01"})
        self.assertEqual(res.status_code, 422)

    def test_attendance_filters_and_filtered_total(self):
        res = self.client.get(
            "/attendance",
            params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-07", "status": "PRESENT"},
        )
        data = res.json()
        self.assertEqual(data["total"], 2)
        self.assertTrue(all(row["emp_code"] == "EMP0001" for row in data["items"]))

    def test_history_timestamps_are_epoch_ms(self):
        res = self.client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-07"})
        item = res.json()["items"][0]
        self.assertTrue(item["history"])
        self.assertIsInstance(item["history"][0]["at"], int)
        self.assertIsInstance(item["history"][0]["changes"]["punch_in"]["from"], int)

    def test_concurrent_employee_creates_one_201_rest_409(self):
        db.employees.delete_one({"emp_code": "EMP7777"})
        payload = {
            "emp_code": "EMP7777",
            "name": "Race",
            "email": "race@example.com",
            "department": "QA",
            "joined_on": "2026-07-01",
        }

        def create():
            with TestClient(app) as c:
                return c.post("/employees", json=payload).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = [fut.result() for fut in as_completed([pool.submit(create) for _ in range(8)])]
        self.assertEqual(codes.count(201), 1)
        self.assertEqual(codes.count(409), 7)
        self.assertEqual(db.employees.count_documents({"emp_code": "EMP7777"}), 1)

    def test_concurrent_punch_in_one_201_rest_409(self):
        punched_at = ist_epoch_ms(2026, 8, 3, 9, 30)
        payload = {"emp_code": "EMP0001", "punched_at": punched_at}

        def punch():
            with TestClient(app) as c:
                return c.post("/attendance/punch-in", json=payload).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = [fut.result() for fut in as_completed([pool.submit(punch) for _ in range(8)])]
        self.assertEqual(codes.count(201), 1)
        self.assertEqual(codes.count(409), 7)
        self.assertEqual(db.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-08-03"}), 1)

    def test_list_indexes_exist(self):
        emp_names = {idx["name"] for idx in db.employees.list_indexes()}
        log_names = {idx["name"] for idx in db.attendance_logs.list_indexes()}
        self.assertIn("uniq_emp_code", emp_names)
        self.assertIn("uniq_emp_date", log_names)
        self.assertIn("date_emp_list", log_names)


if __name__ == "__main__":
    unittest.main()
