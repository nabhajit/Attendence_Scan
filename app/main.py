"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and sample_data/DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from bson import json_util
from pymongo import ASCENDING, DESCENDING, ReturnDocument, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
PRESENCE_STATUSES_LIST = ["PRESENT", "WFH", "ON_DUTY"]

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_indexes_ready = False
logger = logging.getLogger(__name__)

# Index purposes documented in tests/SPRINT4.md and DECISIONS.md.
EMPLOYEE_INDEX_SPECS: list[dict] = [
    {
        "keys": [("emp_code", ASCENDING)],
        "name": "uniq_emp_code",
        "unique": True,
        "purpose": "Employee identity, create-employee race safety, leaderboard $lookup on emp_code.",
    },
    {
        "keys": [("department", ASCENDING), ("emp_code", ASCENDING)],
        "name": "dept_emp_code",
        "purpose": "List employees by department sorted by emp_code; trend headcount by department.",
    },
    {
        "keys": [("joined_on", ASCENDING)],
        "name": "emp_joined_on",
        "purpose": "Department summary headcount filter joined_on <= month_end (R9).",
    },
    {
        "keys": [("department", ASCENDING), ("joined_on", ASCENDING)],
        "name": "dept_joined_on",
        "purpose": "Department summary with optional department filter + joined_on bound.",
    },
]
ATTENDANCE_INDEX_SPECS: list[dict] = [
    {
        "keys": [("emp_code", ASCENDING), ("date", ASCENDING)],
        "name": "uniq_emp_date",
        "unique": True,
        "purpose": "One log per (emp_code, date); punch-in race safety; point lookups for PATCH.",
    },
    {
        "keys": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "name": "date_emp_list",
        "purpose": "GET /attendance list sort + pagination; date-range scans newest-first.",
    },
    {
        "keys": [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        "name": "status_date_emp",
        "purpose": "Attendance list with status filter, same sort order.",
    },
    {
        "keys": [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        "name": "emp_punch_in",
        "purpose": "Punch-out: latest open punch_in for an employee.",
    },
    {
        "keys": [("date", ASCENDING), ("emp_code", ASCENDING)],
        "name": "date_emp_asc",
        "purpose": "Month/date-range analytics on attendance_logs (monthly, leaderboard, trend day lookup).",
    },
]


def find_duplicate_natural_keys() -> list[str]:
    """Report duplicate emp_code or (emp_code, date) before unique indexes are applied."""
    problems: list[str] = []
    for row in db.employees.aggregate(
        [
            {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
        ]
    ):
        problems.append(f"employees: duplicate emp_code={row['_id']} (count={row['n']})")
    for row in db.attendance_logs.aggregate(
        [
            {"$group": {"_id": {"emp_code": "$emp_code", "date": "$date"}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
        ]
    ):
        key = row["_id"]
        problems.append(
            f"attendance_logs: duplicate ({key['emp_code']}, {key['date']}) count={row['n']}"
        )
    return problems


def ensure_indexes() -> None:
    """Create required indexes idempotently. Unique indexes enforce race-safe writes."""
    global _indexes_ready
    if _indexes_ready:
        return

    dupes = find_duplicate_natural_keys()
    if dupes:
        logger.error(
            "Refusing to create unique indexes while duplicates exist: %s",
            "; ".join(dupes),
        )
        raise PyMongoError("duplicate natural keys block unique index creation")

    for spec in EMPLOYEE_INDEX_SPECS:
        db.employees.create_index(
            spec["keys"],
            unique=spec.get("unique", False),
            name=spec["name"],
        )
    for spec in ATTENDANCE_INDEX_SPECS:
        db.attendance_logs.create_index(
            spec["keys"],
            unique=spec.get("unique", False),
            name=spec["name"],
        )
    # Internal mutex for PATCH regularize (see acquire_edit_lock).
    db.attendance_locks.create_index(
        [("created_at", ASCENDING)], expireAfterSeconds=60, name="ttl_created_at"
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # App must still boot so GET /health can return 503 when Mongo is down.
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before storage and calculation."""
    return ensure_utc(dt).replace(microsecond=0)


def to_ist(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(IST)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = map(int, value.split(":"))
    return hour, minute


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def combine_ist(date_str: str, hhmm: str) -> datetime:
    year, month, day = map(int, date_str.split("-"))
    hour, minute = parse_hhmm(hhmm)
    return datetime(year, month, day, hour, minute, 0, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in, with overnight-shift exception."""
    punch_ist = to_ist(truncate_to_seconds(punch_in))
    day = punch_ist.date()
    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = parse_hhmm(shift_end)
        shift_end_today = punch_ist.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if punch_ist < shift_end_today:
            day = day - timedelta(days=1)
    return day.isoformat()


def shift_end_datetime(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = combine_ist(date_str, shift_end)
    if is_overnight_shift(shift_start, shift_end):
        end = end + timedelta(days=1)
    return end


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(ensure_utc(dt).timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    return truncate_to_seconds(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def round_half_up(value: Decimal, places: int) -> Decimal:
    quant = Decimal("1").scaleb(-places)
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then floor minutes since start."""
    punch_in = truncate_to_seconds(punch_in)
    start = combine_ist(date_str, shift_start)
    seconds = (punch_in - start).total_seconds()
    if seconds > 600:
        return int(seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours rounded to 2 decimals with half-up rounding (not banker's round)."""
    punch_in = truncate_to_seconds(punch_in)
    punch_out = truncate_to_seconds(punch_out)
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    return float(round_half_up(hours, 2))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift_end; 0 unless that is >= 30."""
    punch_out = truncate_to_seconds(punch_out)
    end = shift_end_datetime(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    if minutes >= 30:
        return minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when the rounded work_hours is strictly below 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def as_utc_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    return None


def punch_out_window_ok(punch_in: datetime, punch_out: datetime) -> bool:
    """True when punch_out is after punch_in and not more than 24 hours later."""
    punch_in = truncate_to_seconds(punch_in)
    punch_out = truncate_to_seconds(punch_out)
    if punch_out <= punch_in:
        return False
    if (punch_out - punch_in) > timedelta(hours=24):
        return False
    return True


def compute_derived(
    emp: dict,
    date_str: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> tuple[Optional[float], int, int, bool]:
    if status in {"ABSENT", "LEAVE"} or punch_in is None:
        return None, 0, 0, False
    late = compute_late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return None, late, 0, False
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, emp["shift_start"], emp["shift_end"], date_str)
    return hours, late, overtime, compute_half_day(hours)


def canonical_log(doc: dict) -> dict:
    punch_in = as_utc_dt(doc.get("punch_in"))
    punch_out = as_utc_dt(doc.get("punch_out"))
    return {
        "status": doc["status"],
        "punch_in": truncate_to_seconds(punch_in) if punch_in is not None else None,
        "punch_out": truncate_to_seconds(punch_out) if punch_out is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc["late_minutes"]) if doc.get("late_minutes") is not None else 0,
        "overtime_minutes": int(doc["overtime_minutes"]) if doc.get("overtime_minutes") is not None else 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def field_changes(before: dict, after: dict) -> dict:
    changes = {}
    for key in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if before[key] != after[key]:
            changes[key] = {"from": before[key], "to": after[key]}
    return changes


def require_indexes() -> None:
    try:
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="mongodb unavailable") from exc


def parse_month_bounds(month: str) -> tuple[str, str]:
    if not MONTH_PATTERN.match(month):
        raise HTTPException(status_code=422, detail="invalid month")
    year = int(month[:4])
    mon = int(month[5:7])
    start = date_cls(year, mon, 1)
    last = calendar.monthrange(year, mon)[1]
    end = date_cls(year, mon, last)
    return start.isoformat(), end.isoformat()


def count_weekdays_inclusive(start_iso: str, end_iso: str) -> int:
    """R7: Mon–Fri calendar days between two YYYY-MM-DD strings (inclusive)."""
    start = date_cls.fromisoformat(start_iso)
    end = date_cls.fromisoformat(end_iso)
    if start > end:
        return 0
    count = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def employee_working_days(joined_on: str, month_start: str, month_end: str) -> int:
    effective_start = max(joined_on, month_start)
    return count_weekdays_inclusive(effective_start, month_end)


def round_half_up_float(value: float, places: int) -> float:
    return float(round_half_up(Decimal(str(value)), places))


def _round_half_up_expr(value_expr: Any, places: int) -> dict:
    """R8: round-half-up a NON-NEGATIVE aggregation value to ``places`` decimals.

    MongoDB ``$round`` is round-half-to-EVEN (banker's): 0.5 -> 0, 2.5 -> 2, 0.06255 -> 0.0626
    only by luck. R8 needs half-up. ``floor(x * 10**places + 0.5) / 10**places`` is exact half-up
    for every value here, which are all >= 0 (rates and hour averages).
    """
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [value_expr, scale]}, 0.5]}}, scale]}


def _dow_expr(date_string_expr: Any) -> dict:
    return {"$dayOfWeek": {"$dateFromString": {"dateString": date_string_expr}}}


def _is_weekday_expr(dow_var: str = "$$dow") -> dict:
    return {"$and": [{"$gte": [dow_var, 2]}, {"$lte": [dow_var, 6]}]}


def log_present_day_weight() -> dict:
    """Present-day contribution for one attendance log (half_day → 0.5 on weekdays only)."""
    return {
        "$let": {
            "vars": {
                "dow": _dow_expr("$$log.date"),
                "is_presence": {"$in": ["$$log.status", PRESENCE_STATUSES_LIST]},
                "half": {"$ifNull": ["$$log.half_day", False]},
            },
            "in": {
                "$cond": [
                    {"$and": ["$$is_presence", _is_weekday_expr("$$dow")]},
                    {"$cond": ["$$half", 0.5, 1.0]},
                    0,
                ]
            },
        }
    }


def sum_logs_map(logs_field: str, inner_expr: dict) -> dict:
    return {
        "$sum": {
            "$map": {
                "input": {"$ifNull": [logs_field, []]},
                "as": "log",
                "in": inner_expr,
            }
        }
    }


def month_log_lookup_pipeline(month_start: str, month_end: str) -> list[dict]:
    return [
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", month_start]},
                        {"$lte": ["$date", month_end]},
                    ]
                }
            }
        }
    ]


def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$let": {
                            "vars": {
                                "dow": _dow_expr("$date"),
                                "is_presence": {"$in": ["$status", PRESENCE_STATUSES_LIST]},
                                "half": {"$ifNull": ["$half_day", False]},
                            },
                            "in": {
                                "$cond": [
                                    {"$and": ["$$is_presence", _is_weekday_expr("$$dow")]},
                                    {"$cond": ["$$half", 0.5, 1.0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def aggregate_employee_monthly(emp_code: str, month: str, joined_on: str) -> dict:
    month_start, month_end = parse_month_bounds(month)
    working_days = employee_working_days(joined_on, month_start, month_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        row = rows[0]
        present_days = float(row["present_days"])
        leave_days = int(row["leave_days"])
        late_count = int(row["late_count"])
        total_late = int(row["total_late_minutes"])
        total_ot = int(row["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = late_count = total_late = total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        # attendance_pct is a rate -> R8 rounds it to 4 decimals half-up (not 2).
        attendance_pct = round_half_up_float(present_days / working_days * 100, 4)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]) -> list[dict]:
    emp_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    wh_record_expr = {
        "$let": {
            "vars": {
                "is_presence": {"$in": ["$$log.status", PRESENCE_STATUSES_LIST]},
                "wh": "$$log.work_hours",
            },
            "in": {
                "$cond": [
                    {
                        "$and": [
                            "$$is_presence",
                            {"$ne": ["$$wh", None]},
                            {"$ne": [{"$type": "$$wh"}, "missing"]},
                        ]
                    },
                    "$$wh",
                    None,
                ]
            },
        }
    }

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": month_log_lookup_pipeline(month_start, month_end),
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "present_days_emp": sum_logs_map("$logs", log_present_day_weight()),
                "leave_emp": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$logs", []]},
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "late_count_emp": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$logs", []]},
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_emp": sum_logs_map(
                    "$logs",
                    {"$ifNull": ["$$log.late_minutes", 0]},
                ),
                "on_duty_emp": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$logs", []]},
                            "as": "log",
                            "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "wh_records": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": {"$ifNull": ["$logs", []]},
                                "as": "log",
                                "in": wh_record_expr,
                            }
                        },
                        "as": "v",
                        "cond": {"$ne": ["$$v", None]},
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days_emp"},
                "leave_count": {"$sum": "$leave_emp"},
                "late_count": {"$sum": "$late_count_emp"},
                "total_late_minutes": {"$sum": "$total_late_emp"},
                "on_duty_count": {"$sum": "$on_duty_emp"},
                "wh_nested": {"$push": "$wh_records"},
            }
        },
        {
            "$addFields": {
                "wh_flat": {
                    "$reduce": {
                        "input": "$wh_nested",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                }
            }
        },
        {
            "$addFields": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$wh_flat"}, 0]},
                        {"$avg": "$wh_flat"},
                        None,
                    ]
                }
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]
    return pipeline


def aggregate_department_summary(month: str, department: Optional[str]) -> dict:
    month_start, month_end = parse_month_bounds(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = float(item["present_days"])
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up_float(float(item["avg_work_hours"]), 2)
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month_start: str, month_end: str, limit: int, department: Optional[str]
) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ]
    )
    return pipeline


def aggregate_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    month_start, month_end = parse_month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_iso: str, to_iso: str) -> list[dict]:
    from_d = date_cls.fromisoformat(from_iso)
    to_d = date_cls.fromisoformat(to_iso)
    num_days = (to_d - from_d).days + 1
    start_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)

    return [
        {
            "$documents": {
                "$map": {
                    "input": {"$range": [0, num_days]},
                    "as": "offset",
                    "in": {"offset": "$$offset"},
                }
            }
        },
        {
            "$addFields": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {
                            "$dateAdd": {
                                "startDate": start_dt,
                                "unit": "day",
                                "amount": "$offset",
                            }
                        },
                    }
                },
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": start_dt,
                                            "unit": "day",
                                            "amount": "$offset",
                                        }
                                    },
                                }
                            }
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$let": {
                                        "vars": {
                                            "is_presence": {
                                                "$in": ["$status", PRESENCE_STATUSES_LIST]
                                            },
                                            "half": {"$ifNull": ["$half_day", False]},
                                        },
                                        "in": {
                                            "$cond": [
                                                "$$is_presence",
                                                {"$cond": ["$$half", 0.5, 1.0]},
                                                0,
                                            ]
                                        },
                                    }
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "day_logs",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$ifNull": [{"$first": "$day_logs.present_count"}, 0.0]
                },
                "late_count": {"$ifNull": [{"$first": "$day_logs.late_count"}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        _round_half_up_expr(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        _round_half_up_expr("$moving_avg_7d", 4),
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


def run_department_trend_pipeline(pipeline: list[dict]) -> list[dict]:
    # $documents requires a database-level aggregate command (MongoDB 5.0+).
    result = db.command("aggregate", 1, pipeline=pipeline, cursor={})
    return list(result["cursor"]["firstBatch"])


def aggregate_department_trend(department: str, from_iso: str, to_iso: str) -> list[dict]:
    pipeline = build_department_trend_pipeline(department, from_iso, to_iso)
    return run_department_trend_pipeline(pipeline)


ATTENDANCE_LIST_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
ExplainEndpointName = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


def attendance_list_filter(
    emp_code: Optional[str],
    date_from: Optional[date_cls],
    date_to: Optional[date_cls],
    status: Optional[str],
) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    return q


def normalize_explain_doc(doc: dict) -> dict:
    """Convert BSON types in an explain document to JSON-friendly values."""
    return json_util.loads(json_util.dumps(doc))


def explain_find_attendance_list(q: dict, page: int, page_size: int) -> dict:
    skip = (page - 1) * page_size
    sort_doc = {field: direction for field, direction in ATTENDANCE_LIST_SORT}
    return db.command(
        "explain",
        {
            "find": "attendance_logs",
            "filter": q,
            "sort": sort_doc,
            "skip": skip,
            "limit": page_size,
        },
        verbosity="executionStats",
    )


def explain_collection_aggregate(collection: str, pipeline: list[dict]) -> dict:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def explain_database_aggregate(pipeline: list[dict]) -> dict:
    return db.command(
        "explain",
        {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def explain_plan_contains(stage_name: str, explain_doc: dict) -> bool:
    """Return True if any stage tree in the explain output uses ``stage_name``."""
    found = False

    def walk(node: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(node, dict):
            if node.get("stage") == stage_name:
                found = True
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(explain_doc)
    return found


def run_admin_explain(
    endpoint: ExplainEndpointName,
    *,
    emp_code: Optional[str],
    month: Optional[str],
    department: Optional[str],
    limit: int,
    date_from: Optional[date_cls],
    date_to: Optional[date_cls],
    status: Optional[str],
    trend_from: Optional[date_cls],
    trend_to: Optional[date_cls],
    page: int,
    page_size: int,
) -> dict:
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = attendance_list_filter(emp_code, date_from, date_to, status)
        explain = explain_find_attendance_list(q, page, page_size)
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        month_start, month_end = parse_month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        explain = explain_collection_aggregate("attendance_logs", pipeline)
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = parse_month_bounds(month)
        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        explain = explain_collection_aggregate("employees", pipeline)
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, month_end = parse_month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
        explain = explain_collection_aggregate("attendance_logs", pipeline)
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or trend_from is None or trend_to is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if trend_to < trend_from:
            raise HTTPException(status_code=422, detail="to must be >= from")
        if (trend_to - trend_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range exceeds 92 days")
        pipeline = build_department_trend_pipeline(
            department, trend_from.isoformat(), trend_to.isoformat()
        )
        explain = explain_database_aggregate(pipeline)
        collection = "attendance_logs"
    else:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": normalize_explain_doc(explain),
    }


# ---------------------------------------------------------------------------
# Serialization of manual corrections (see regularize_attendance).
#
# find_one_and_update predicate guards are NOT reliable as an exactly-one-winner
# CAS on a standalone MongoDB (a pre-scalar/$size guard lets several concurrent
# writers through). The primitive that IS reliable everywhere is a unique-index
# INSERT, which is what punch-in / employee creation already use. We take a
# per-record lock that way: the first request inserts a claim document whose _id
# is unique; every concurrent request hits DuplicateKeyError and returns 409.
# The holder re-reads fresh state under the lock, writes in a single update, and
# releases the claim in a finally block. The TTL index (created in ensure_indexes)
# reclaims a lock if the process crashes before it can release it.
# ---------------------------------------------------------------------------
def lock_key(emp_code: str, date_str: str) -> str:
    return f"LOCK/{emp_code}/{date_str}"


def acquire_edit_lock(emp_code: str, date_str: str) -> None:
    """Acquire the exclusive write lock for a record, or raise 409 for the loser."""
    try:
        db.attendance_locks.insert_one(
            {"_id": lock_key(emp_code, date_str), "created_at": datetime.now(timezone.utc)}
        )
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="concurrent update") from exc


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


def _serialize_change_value(field: str, value: Any) -> Any:
    if field in {"punch_in", "punch_out"} and isinstance(value, datetime):
        return dt_to_epoch_ms(value)
    return value


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        changes[key] = {
            "from": _serialize_change_value(key, change.get("from")),
            "to": _serialize_change_value(key, change.get("to")),
        }
    at = entry.get("at")
    return {
        "at": dt_to_epoch_ms(at) if isinstance(at, datetime) else at,
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance(doc: dict) -> dict:
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(punch_in) if isinstance(punch_in, datetime) else punch_in,
        "punch_out": dt_to_epoch_ms(punch_out) if isinstance(punch_out, datetime) else punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc["late_minutes"]) if doc.get("late_minutes") is not None else 0,
        "overtime_minutes": int(doc["overtime_minutes"]) if doc.get("overtime_minutes") is not None else 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(item) for item in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="mongodb unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="mongodb unavailable") from exc

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="mongodb unavailable") from exc

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_to_seconds(
        datetime.now(timezone.utc)
    )
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = attendance_list_filter(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_LIST_SORT)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(item) for item in page_docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    require_indexes()

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else truncate_to_seconds(
        datetime.now(timezone.utc)
    )

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_ts = as_utc_dt(rec["punch_in"])
    if punch_in_ts is None or not punch_out_window_ok(punch_in_ts, ts):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    work_hours, _late, overtime_minutes, half_day = compute_derived(
        emp, rec["date"], rec["status"], punch_in_ts, ts
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_in": rec["punch_in"],
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date_cls, body: RegularizeIn):
    require_indexes()

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    date_str = date.isoformat()
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if rec is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    # Serialize corrections to this record. The unique-insert lock guarantees that while one
    # request is editing, every concurrent request for the same record gets 409 (never a silent
    # overwrite that drops a history entry). No transaction is required - this works on a
    # standalone server (see acquire_edit_lock).
    acquire_edit_lock(emp_code, date_str)
    try:
        # Re-read under the lock: the baseline must reflect any correction a just-finished
        # sequential edit applied, so before/after diffs and recomputed fields stay truthful.
        rec = db.attendance_logs.find_one({"_id": rec["_id"]})
        if rec is None:
            raise HTTPException(status_code=404, detail="attendance record not found")

        incoming = body.model_dump(exclude_unset=True)
        new_status = incoming.get("status", rec["status"])
        supplied_punch_in = "punch_in" in incoming
        supplied_punch_out = "punch_out" in incoming

        if new_status in {"ABSENT", "LEAVE"} and (supplied_punch_in or supplied_punch_out):
            raise HTTPException(
                status_code=422,
                detail="ABSENT/LEAVE cannot be combined with punch times",
            )

        if new_status in {"ABSENT", "LEAVE"}:
            new_punch_in = None
            new_punch_out = None
        else:
            new_punch_in = epoch_ms_to_dt(incoming["punch_in"]) if supplied_punch_in else as_utc_dt(rec.get("punch_in"))
            new_punch_out = epoch_ms_to_dt(incoming["punch_out"]) if supplied_punch_out else as_utc_dt(rec.get("punch_out"))
            if new_punch_in is None:
                raise HTTPException(status_code=422, detail="presence status requires punch_in")
            if attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"]) != date_str:
                raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
            if new_punch_out is not None and not punch_out_window_ok(new_punch_in, new_punch_out):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

        work_hours, late_minutes, overtime_minutes, half_day = compute_derived(
            emp, date_str, new_status, new_punch_in, new_punch_out
        )
        after = {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": work_hours,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
        changes = field_changes(canonical_log(rec), after)
        if not changes:
            raise HTTPException(status_code=422, detail="no fields changed")

        history_entry = {
            "at": truncate_to_seconds(datetime.now(timezone.utc)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }
        # $push creates ``history`` for legacy records that never had the field, and appends
        # exactly one entry to existing history (existing entries are preserved untouched).
        updated = db.attendance_logs.find_one_and_update(
            {"_id": rec["_id"]},
            {
                "$set": after,
                "$push": {"history": history_entry},
            },
            return_document=ReturnDocument.AFTER,
        )
    finally:
        db.attendance_locks.delete_one({"_id": lock_key(emp_code, date_str)})

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    require_indexes()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return aggregate_employee_monthly(emp_code, month, emp["joined_on"])


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    require_indexes()
    return aggregate_department_summary(month, department)


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    require_indexes()
    return aggregate_late_leaderboard(month, limit, department)


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date_cls = Query(..., alias="from"),
    to_date: date_cls = Query(..., alias="to"),
):
    require_indexes()
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    items = aggregate_department_trend(
        department, from_date.isoformat(), to_date.isoformat()
    )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain_endpoint(
    endpoint: ExplainEndpointName,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    trend_from: Optional[date_cls] = Query(default=None, alias="from"),
    trend_to: Optional[date_cls] = Query(default=None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    require_indexes()
    return run_admin_explain(
        endpoint,
        emp_code=emp_code,
        month=month,
        department=department,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        status=status,
        trend_from=trend_from,
        trend_to=trend_to,
        page=page,
        page_size=page_size,
    )
