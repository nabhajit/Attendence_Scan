"""OpenAPI contract checks: response shapes, no Mongo _id, status codes, lock cleanup."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["MONGO_DB"] = "attendance_openapi_contract"

from bson import json_util  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, db, ensure_indexes, lock_key  # noqa: E402

EMPLOYEE_KEYS = {
    "emp_code",
    "name",
    "email",
    "department",
    "shift_start",
    "shift_end",
    "joined_on",
    "created_at",
}
ATTENDANCE_KEYS = {
    "emp_code",
    "date",
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
    "history",
}
MONTHLY_KEYS = {
    "emp_code",
    "month",
    "working_days",
    "present_days",
    "leave_days",
    "late_count",
    "total_late_minutes",
    "total_overtime_minutes",
    "attendance_pct",
}


def load_sample():
    opts = json_util.JSONOptions(tz_aware=True)
    here = ROOT / "sample_data"
    for name in ("employees", "attendance_logs"):
        db[name].delete_many({})
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        if docs:
            db[name].insert_many(docs)


def assert_no_mongo_id(testcase, obj, path="root"):
    if isinstance(obj, dict):
        testcase.assertNotIn("_id", obj, msg=path)
        for key, val in obj.items():
            assert_no_mongo_id(testcase, val, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            assert_no_mongo_id(testcase, val, f"{path}[{i}]")


class OpenAPIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        db.attendance_locks.delete_many({})
        load_sample()
        import app.main as main_mod

        main_mod._indexes_ready = False
        ensure_indexes()

    def test_health_ok_shape(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})
        assert_no_mongo_id(self, r.json())

    def test_create_and_list_employee_openapi_shape(self):
        body = {
            "emp_code": "EMP9010",
            "name": "Contract Test",
            "email": "c@example.com",
            "department": "QA",
            "joined_on": "2026-01-01",
        }
        r = self.client.post("/employees", json=body)
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertEqual(set(data.keys()), EMPLOYEE_KEYS)
        self.assertIsInstance(data["created_at"], int)
        assert_no_mongo_id(self, data)

        lst = self.client.get("/employees", params={"page": 1, "page_size": 5}).json()
        self.assertEqual(set(lst.keys()), {"items", "total", "page", "page_size"})
        self.assertEqual(set(lst["items"][0].keys()), EMPLOYEE_KEYS)
        assert_no_mongo_id(self, lst)

    def test_attendance_list_and_record_shapes(self):
        page = self.client.get(
            "/attendance",
            params={"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 5},
        )
        self.assertEqual(page.status_code, 200)
        data = page.json()
        self.assertEqual(set(data.keys()), {"items", "total", "page", "page_size"})
        self.assertGreater(data["total"], 0)
        self.assertEqual(set(data["items"][0].keys()), ATTENDANCE_KEYS)
        assert_no_mongo_id(self, data)

    def test_analytics_response_shapes(self):
        m = self.client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        self.assertEqual(m.status_code, 200)
        self.assertEqual(set(m.json().keys()), MONTHLY_KEYS)
        assert_no_mongo_id(self, m.json())

        ds = self.client.get("/analytics/departments/summary", params={"month": "2026-07"})
        self.assertEqual(ds.status_code, 200)
        self.assertEqual(set(ds.json().keys()), {"month", "items"})
        if ds.json()["items"]:
            item = ds.json()["items"][0]
            self.assertEqual(
                set(item.keys()),
                {
                    "department",
                    "headcount",
                    "present_days",
                    "avg_work_hours",
                    "late_count",
                    "total_late_minutes",
                    "leave_count",
                    "on_duty_count",
                },
            )
        assert_no_mongo_id(self, ds.json())

        lb = self.client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 10})
        self.assertEqual(lb.status_code, 200)
        self.assertEqual(set(lb.json().keys()), {"month", "items"})
        if lb.json()["items"]:
            self.assertEqual(
                set(lb.json()["items"][0].keys()),
                {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"},
            )
        assert_no_mongo_id(self, lb.json())

        tr = self.client.get(
            "/analytics/departments/Engineering/trend",
            params={"from": "2026-07-01", "to": "2026-07-03"},
        )
        self.assertEqual(tr.status_code, 200)
        self.assertEqual(set(tr.json().keys()), {"department", "items"})
        self.assertEqual(len(tr.json()["items"]), 3)
        row = tr.json()["items"][0]
        self.assertEqual(
            set(row.keys()),
            {
                "date",
                "is_working_day",
                "headcount",
                "present_count",
                "late_count",
                "attendance_rate",
                "moving_avg_7d",
            },
        )
        assert_no_mongo_id(self, tr.json())

    def test_error_status_codes_documented(self):
        self.assertEqual(self.client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code, 404)
        self.assertEqual(
            self.client.get("/analytics/departments/NoSuchDept/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code,
            404,
        )
        # Sample already has EMP0001 on 2026-07-06.
        from datetime import datetime, timezone, timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
        ms = int(datetime(2026, 7, 6, 9, 28, tzinfo=ist).timestamp() * 1000)
        dup = self.client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
        self.assertEqual(dup.status_code, 409)
        self.assertIn("detail", dup.json())

    def test_explain_endpoints_match_openapi_envelope(self):
        cases = [
            ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 1, "page_size": 20}),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", {"month": "2026-07"}),
            ("late_leaderboard", {"month": "2026-07", "limit": 10}),
            (
                "department_trend",
                {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"},
            ),
        ]
        for endpoint, params in cases:
            r = self.client.get(f"/admin/explain/{endpoint}", params=params)
            self.assertEqual(r.status_code, 200, (endpoint, r.text))
            body = r.json()
            self.assertEqual(set(body.keys()), {"endpoint", "collection", "explain"})
            self.assertEqual(body["endpoint"], endpoint)
            self.assertIsInstance(body["explain"], dict)
            # Explain documents may contain Mongo planner keys like projection `_id: 0`.
            assert_no_mongo_id(self, {k: body[k] for k in ("endpoint", "collection")})

    def test_lock_released_after_regularize_success_and_noop_422(self):
        from datetime import datetime, timezone, timedelta

        ist = timezone(timedelta(hours=5, minutes=30))

        def ms_ist(y, mo, d, h, mi):
            return int(datetime(y, mo, d, h, mi, tzinfo=ist).timestamp() * 1000)

        db.attendance_locks.delete_many({})
        ok = self.client.patch(
            "/attendance/EMP0001/2026-07-07",
            json={
                "reason": "contract check",
                "regularized_by": "qa",
                "punch_in": ms_ist(2026, 7, 7, 9, 20),
            },
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(db.attendance_locks.count_documents({}), 0)

        db.attendance_locks.delete_many({})
        noop = self.client.patch(
            "/attendance/EMP0001/2026-07-07",
            json={"reason": "noop again", "regularized_by": "qa", "punch_in": ok.json()["punch_in"]},
        )
        self.assertEqual(noop.status_code, 422)
        self.assertEqual(db.attendance_locks.count_documents({}), 0)
        self.assertFalse(
            db.attendance_locks.find_one({"_id": lock_key("EMP0001", "2026-07-07")})
        )

    def test_department_summary_includes_zero_log_headcount(self):
        """R9: EMP0002 has no attendance logs but must count in Engineering headcount."""
        got = self.client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}).json()
        self.assertEqual(len(got["items"]), 1)
        self.assertGreaterEqual(got["items"][0]["headcount"], 2)


if __name__ == "__main__":
    unittest.main()
