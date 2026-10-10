"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# Index Initialization
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes idempotently on startup."""
    # employees collection indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs collection indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("late_minutes", 1), ("date", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        # Ignore startup connection errors if Mongo is offline during static checks
        pass
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int) -> float:
    """R8: Rounding half-up (not banker's / round half-to-even)."""
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer milliseconds since Unix epoch."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def now_utc_truncated() -> datetime:
    """Current UTC datetime truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_time_str = punch_in_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start

    if is_overnight and ist_time_str < shift_end:
        attendance_day = punch_in_ist.date() - timedelta(days=1)
    else:
        attendance_day = punch_in_ist.date()

    return attendance_day.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace ended.
    """
    punch_in_ist = punch_in_utc.astimezone(IST).replace(microsecond=0)
    sh, sm = map(int, shift_start.split(":"))
    d = date.fromisoformat(attendance_date)
    shift_start_dt = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = sec / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(
    punch_out_utc: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out, counted only
    if that is at least 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on next calendar day (R1).
    """
    punch_out_ist = punch_out_utc.astimezone(IST).replace(microsecond=0)
    eh, em = map(int, shift_end.split(":"))
    d = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        d = d + timedelta(days=1)
    shift_end_dt = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)

    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 30 * 60:
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def doc_to_employee(doc: dict) -> dict:
    """Format employee document for HTTP response (epoch ms created_at, no _id)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def doc_to_attendance_record(doc: dict) -> dict:
    """Format attendance document for HTTP response (all instants in ms, no id/_id)."""
    history_out = []
    for h in doc.get("history") or []:
        entry = {
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": {},
        }
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = dt_to_epoch_ms(f_val)
                t_val = dt_to_epoch_ms(t_val)
            entry["changes"][k] = {"from": f_val, "to": t_val}
        history_out.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


def clean_explain(obj: Any) -> Any:
    """Ensure MongoDB explain output is safely JSON serializable."""
    return json.loads(json_util.dumps(obj))


# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Pipelines for Analytics
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[dict]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$addFields": {
                "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[dict]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "dow": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dow", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> List[dict]:
    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[dict] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
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

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[dict]:
    f_d = date.fromisoformat(from_date)
    t_d = date.fromisoformat(to_date)
    from_dt = datetime(f_d.year, f_d.month, f_d.day, tzinfo=timezone.utc)
    to_dt = datetime(t_d.year, t_d.month, t_d.day, tzinfo=timezone.utc)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
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
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}}
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date_dt": "$_id",
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
                ],
                "as": "dept_employees",
            }
        },
        {
            "$addFields": {
                "dow": {"$dayOfWeek": "$date_dt"},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$dow", [2, 3, 4, 5, 6]]}
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
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
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


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB unavailable",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="emp_code already exists",
        )
    return doc_to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [doc_to_employee(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    ts = (
        epoch_ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )

    attendance_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return doc_to_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Employee not found",
        )

    punched_at_dt = (
        epoch_ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc_truncated()
    )

    # Find employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at_dt},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record already punched out",
        )

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punched_at_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly greater than punch_in",
        )

    if (punched_at_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_dt, punched_at_dt)
    overtime_minutes = compute_overtime(
        punched_at_dt,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )
    half_day = compute_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record already punched out",
        )

    return doc_to_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and not DATE_PATTERN.match(date_from):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date_from")
    if date_to and not DATE_PATTERN.match(date_to):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "date_from cannot be greater than date_to",
        )

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status_filter and status_filter not in valid_statuses:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid status")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [doc_to_attendance_record(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., description="The record's attendance date, YYYY-MM-DD."),
    body: RegularizeRequest = ...,
):
    record_date = date
    if not DATE_PATTERN.match(record_date):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendance record not found")

    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Presence status requires a punch_in",
            )

        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != record_date:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"punch_in attendance date ({calc_date}) does not match record date ({record_date})",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "punch_out must be strictly greater than punch_in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], record_date)
        if new_punch_out is None:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False
        else:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out,
                emp["shift_start"],
                emp["shift_end"],
                record_date,
            )
            new_half_day = compute_half_day(new_work_hours)

    # Detect changes
    changes_in_db: Dict[str, dict] = {}

    def normalize_dt(val: Any) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.replace(tzinfo=timezone.utc) if val.tzinfo is None else val
        return val

    # status
    if record.get("status") != target_status:
        changes_in_db["status"] = {"from": record.get("status"), "to": target_status}

    # punch_in
    old_pin = normalize_dt(record.get("punch_in"))
    cur_pin = normalize_dt(new_punch_in)
    if old_pin != cur_pin:
        changes_in_db["punch_in"] = {"from": old_pin, "to": cur_pin}

    # punch_out
    old_pout = normalize_dt(record.get("punch_out"))
    cur_pout = normalize_dt(new_punch_out)
    if old_pout != cur_pout:
        changes_in_db["punch_out"] = {"from": old_pout, "to": cur_pout}

    # work_hours
    if record.get("work_hours") != new_work_hours:
        changes_in_db["work_hours"] = {
            "from": record.get("work_hours"),
            "to": new_work_hours,
        }

    # late_minutes
    old_late = record.get("late_minutes", 0)
    if old_late != new_late_minutes:
        changes_in_db["late_minutes"] = {"from": old_late, "to": new_late_minutes}

    # overtime_minutes
    old_ot = record.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        changes_in_db["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    # half_day
    old_hd = bool(record.get("half_day", False))
    if old_hd != new_half_day:
        changes_in_db["half_day"] = {"from": old_hd, "to": new_half_day}

    if not changes_in_db:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Request does not change any values",
        )

    now_utc = now_utc_truncated()
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_in_db,
    }

    # Concurrency safe update: ensure history array length matches to prevent lost updates
    existing_history = record.get("history") or []
    filter_q: Dict[str, Any] = {"_id": record["_id"]}
    if "history" in record:
        filter_q["history"] = {"$size": len(existing_history)}
    else:
        filter_q["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        filter_q,
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Concurrent modification detected",
        )

    return doc_to_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

    year, m = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # Calculate working days (Mon-Fri from joined_on, R7)
    joined_on = emp.get("joined_on", "1970-01-01")
    effective_start_str = max(month_start, joined_on)

    working_days = 0
    if joined_on <= month_end:
        start_date = date.fromisoformat(effective_start_str)
        end_date = date.fromisoformat(month_end)
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5:  # Monday to Friday
                working_days += 1
            cur += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    rows = list(db.employees.aggregate(pipeline))

    items = []
    for r in rows:
        work_hours_cnt = r.get("work_hours_count", 0)
        work_hours_sum = r.get("work_hours_sum", 0.0)
        avg_work_hours = (
            round_half_up(work_hours_sum / work_hours_cnt, 2)
            if work_hours_cnt > 0
            else None
        )

        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(float(r.get("present_days", 0.0)), 2),
            "avg_work_hours": avg_work_hours,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    formatted_items = []
    for item in items:
        formatted_items.append({
            "rank": int(item["rank"]),
            "emp_code": item["emp_code"],
            "name": item["name"],
            "department": item["department"],
            "total_late_minutes": int(item["total_late_minutes"]),
            "late_count": int(item["late_count"]),
        })

    return {"month": month, "items": formatted_items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    if not DATE_PATTERN.match(from_date) or not DATE_PATTERN.match(to_date):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date format")

    f_d = date.fromisoformat(from_date)
    t_d = date.fromisoformat(to_date)
    if t_d < f_d:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "'to' date cannot be before 'from' date",
        )

    num_days = (t_d - f_d).days + 1
    if num_days > 92:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Date range exceeds 92 days",
        )

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in rows:
        att_rate = r.get("attendance_rate")
        mov_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(float(r["present_count"]), 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": (
                round_half_up(float(att_rate), 4) if att_rate is not None else None
            ),
            "moving_avg_7d": (
                round_half_up(float(mov_avg), 4) if mov_avg is not None else None
            ),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    endpoint_name = endpoint
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint_name not in valid_endpoints:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid endpoint")

    if endpoint_name == "attendance_list":
        if date_from and not DATE_PATTERN.match(date_from):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date_from")
        if date_to and not DATE_PATTERN.match(date_to):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from > date_to")

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        exp = cursor.explain("executionStats")
        return {
            "endpoint": "attendance_list",
            "collection": "attendance_logs",
            "explain": clean_explain(exp),
        }

    elif endpoint_name == "employee_monthly":
        if not emp_code or not month or not MONTH_PATTERN.match(month):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "emp_code and valid month are required",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": "employee_monthly",
            "collection": "attendance_logs",
            "explain": clean_explain(exp),
        }

    elif endpoint_name == "department_summary":
        if not month or not MONTH_PATTERN.match(month):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Valid month is required",
            )
        pipeline = build_department_summary_pipeline(month, department)
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": "department_summary",
            "collection": "employees",
            "explain": clean_explain(exp),
        }

    elif endpoint_name == "late_leaderboard":
        if not month or not MONTH_PATTERN.match(month):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Valid month is required",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": "late_leaderboard",
            "collection": "attendance_logs",
            "explain": clean_explain(exp),
        }

    elif endpoint_name == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "department, from, and to are required",
            )
        if not DATE_PATTERN.match(from_date) or not DATE_PATTERN.match(to_date):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date format")

        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
        if t_d < f_d or (t_d - f_d).days + 1 > 92:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date range")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": "department_trend",
            "collection": "attendance_logs",
            "explain": clean_explain(exp),
        }
