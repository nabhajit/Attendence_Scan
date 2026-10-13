"""Indexes + GET /admin/explain/{endpoint} tests."""
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MONGO_DB", "attendance_s4_admin_test")

from bson import json_util  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import (  # noqa: E402
    app,
    db,
    ensure_indexes,
    explain_plan_contains,
    find_duplicate_natural_keys,
)

EXPECTED_INDEX_NAMES = {
    "employees": {
        "uniq_emp_code",
        "dept_emp_code",
        "emp_joined_on",
        "dept_joined_on",
    },
    "attendance_logs": {
        "uniq_emp_date",
        "date_emp_list",
        "status_date_emp",
        "emp_punch_in",
        "date_emp_asc",
    },
}


def load_sample():
    opts = json_util.JSONOptions(tz_aware=True)
    here = ROOT / "sample_data"
    for name in ("employees", "attendance_logs"):
        db[name].delete_many({})
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        if docs:
            db[name].insert_many(docs)


class AdminExplainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        db.attendance_locks.delete_many({})
        load_sample()
        import app.main as main_mod

        main_mod._indexes_ready = False
        ensure_indexes()

    def test_indexes_created_idempotent(self):
        names = {idx["name"] for idx in db.employees.list_indexes()}
        self.assertTrue(EXPECTED_INDEX_NAMES["employees"].issubset(names))
        names = {idx["name"] for idx in db.attendance_logs.list_indexes()}
        self.assertTrue(EXPECTED_INDEX_NAMES["attendance_logs"].issubset(names))
        ensure_indexes()
        ensure_indexes()

    def test_no_duplicate_natural_keys_in_sample(self):
        self.assertEqual(find_duplicate_natural_keys(), [])

    def _explain(self, endpoint: str, **params):
        return self.client.get(f"/admin/explain/{endpoint}", params=params)

    def test_explain_attendance_list_ixscan(self):
        r = self._explain(
            "attendance_list",
            date_from="2026-07-01",
            date_to="2026-07-31",
            page=1,
            page_size=20,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["endpoint"], "attendance_list")
        self.assertEqual(body["collection"], "attendance_logs")
        self.assertIn("explain", body)
        self.assertTrue(explain_plan_contains("IXSCAN", body["explain"]))
        self.assertFalse(explain_plan_contains("COLLSCAN", body["explain"]))

    def test_explain_employee_monthly(self):
        r = self._explain("employee_monthly", emp_code="EMP0001", month="2026-07")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["collection"], "attendance_logs")
        self.assertTrue(explain_plan_contains("IXSCAN", body["explain"]))

    def test_explain_department_summary(self):
        r = self._explain("department_summary", month="2026-07")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["collection"], "employees")
        self.assertTrue(explain_plan_contains("IXSCAN", body["explain"]))

    def test_explain_late_leaderboard(self):
        r = self._explain("late_leaderboard", month="2026-07", limit=10)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(explain_plan_contains("IXSCAN", body["explain"]))

    def test_explain_department_trend(self):
        r = self._explain(
            "department_trend",
            department="Engineering",
            **{"from": "2026-07-01", "to": "2026-07-07"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["collection"], "attendance_logs")
        explain_blob = json.dumps(body["explain"])
        self.assertTrue(
            "executionStats" in explain_blob
            or "indexesUsed" in explain_blob
            or explain_plan_contains("IXSCAN", body["explain"])
        )

    def test_explain_missing_params_422(self):
        self.assertEqual(self._explain("employee_monthly").status_code, 422)
        self.assertEqual(
            self._explain("department_trend", **{"from": "2026-07-01", "to": "2026-07-02"}).status_code,
            422,
        )

    def test_explain_matches_list_filters(self):
        r = self._explain(
            "attendance_list",
            emp_code="EMP0001",
            status="PRESENT",
            date_from="2026-07-06",
            date_to="2026-07-08",
            page=1,
            page_size=5,
        )
        self.assertEqual(r.status_code, 200)
        plan = json.dumps(r.json()["explain"])
        self.assertIn("EMP0001", plan)


class LargeDatasetExplainTests(unittest.TestCase):
    """Optional scale check (~5k rows) — verifies IXSCAN on month scan, not full COLLSCAN."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        db.employees.insert_one(
            {
                "emp_code": "EMP9000",
                "name": "Scale",
                "email": "scale@example.com",
                "department": "Eng",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2020-01-01",
                "created_at": datetime.now(timezone.utc),
            }
        )
        bulk = []
        base = date(2020, 1, 1)
        for i in range(5000):
            d = (base + timedelta(days=i)).isoformat()
            bulk.append(
                {
                    "emp_code": "EMP9000",
                    "date": d,
                    "status": "PRESENT",
                    "punch_in": datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc),
                    "punch_out": datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc),
                    "work_hours": 8.0,
                    "late_minutes": i % 3,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }
            )
        db.attendance_logs.insert_many(bulk, ordered=False)
        import app.main as main_mod

        main_mod._indexes_ready = False
        ensure_indexes()

    def test_leaderboard_explain_ixscan_on_large_month(self):
        r = self.client.get(
            "/admin/explain/late_leaderboard",
            params={"month": "2024-06", "limit": 5},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(explain_plan_contains("IXSCAN", r.json()["explain"]))
        self.assertFalse(explain_plan_contains("COLLSCAN", r.json()["explain"]))


if __name__ == "__main__":
    unittest.main()
