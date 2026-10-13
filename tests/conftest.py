"""Pytest hooks: force an isolated MongoDB database before app import in each worker."""
import os

# Must be set before any test module imports app.main (which binds `db` once).
os.environ.setdefault("MONGO_DB", "attendance_pytest_isolated")
