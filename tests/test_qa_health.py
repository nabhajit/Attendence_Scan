"""QA: health probe and MongoDB failure path."""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("MONGO_DB", "attendance_qa_health_test")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402

from app.main import app, health  # noqa: E402


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_ok_when_mongo_up(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_health_503_when_ping_fails(self):
        """Call the endpoint function directly so the Mongo client patch applies reliably."""
        mock_client = MagicMock()
        mock_client.admin.command.side_effect = PyMongoError("connection refused")
        with patch("app.main.client", mock_client):
            with self.assertRaises(HTTPException) as ctx:
                health()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "mongodb unavailable")


if __name__ == "__main__":
    unittest.main()
