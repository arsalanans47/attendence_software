"""Employee attendance API. Run with ``uvicorn main:app --port 8000``."""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()


# ======================================================================
# DATABASE
# ======================================================================

client = MongoClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB") or "attendance_db"]

# collection -> [(index name, keys, unique)]
INDEXES = {
    "employees": [
        ("uq_employee_code", [("emp_code", 1)], True),
        ("ix_employee_department_code", [("department", 1), ("emp_code", 1)], False),
        ("ix_employee_department_joined", [("department", 1), ("joined_on", 1)], False),
        ("ix_employee_joined", [("joined_on", 1)], False),
    ],
    "attendance_logs": [
        ("uq_attendance_employee_date", [("emp_code", 1), ("date", 1)], True),
        ("ix_attendance_date_code", [("date", -1), ("emp_code", 1)], False),
        ("ix_attendance_date_status_code", [("date", -1), ("status", 1), ("emp_code", 1)], False),
        ("ix_attendance_employee_date_status", [("emp_code", 1), ("date", 1), ("status", 1)], False),
        ("ix_attendance_late_date", [("date", 1), ("late_minutes", -1), ("emp_code", 1)], False),
    ],
}


def create_indexes() -> None:
    """Create missing indexes; skip any that already exist under a different name."""
    for collection, specs in INDEXES.items():
        existing = db[collection].index_information().values()
        existing_keys = {tuple(map(tuple, info["key"])) for info in existing}
        for name, keys, unique in specs:
            if tuple(keys) in existing_keys:
                continue  # same keys already indexed (maybe from an older version)
            db[collection].create_index(keys, name=name, unique=unique)


# ======================================================================
# TIME HELPERS
# ======================================================================

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0, tzinfo=None)


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def resolve_time(epoch_ms: int | None) -> datetime:
    """Use the given timestamp, or 'now' when the client did not send one."""
    return from_epoch_ms(epoch_ms) if epoch_ms is not None else now_utc()


def shift_datetime(day: str, hhmm: str, next_day: bool = False) -> datetime:
    """Shift time on a given IST date, returned as naive UTC."""
    hour, minute = map(int, hhmm.split(":"))
    result = datetime.combine(date.fromisoformat(day), time(hour, minute), IST)
    if next_day:
        result += timedelta(days=1)
    return result.astimezone(UTC).replace(tzinfo=None)


# ======================================================================
# BUSINESS RULES
# ======================================================================

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
LATE_GRACE_SECONDS = 600
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_SHIFT_LENGTH = timedelta(hours=24)


def round_half_up(value, places: int = 2) -> float:
    exp = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(exp, rounding=ROUND_HALF_UP))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_day(punch_in: datetime, emp: dict) -> str:
    """The date a punch belongs to (overnight shifts count toward the day they started)."""
    local = punch_in.replace(tzinfo=UTC).astimezone(IST)
    if is_overnight(emp["shift_start"], emp["shift_end"]) and local.strftime("%H:%M") < emp["shift_end"]:
        local -= timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, day: str) -> int:
    seconds_late = int((punch_in - shift_datetime(day, shift_start)).total_seconds())
    return seconds_late // 60 if seconds_late > LATE_GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600))


def compute_overtime(punch_out: datetime, emp: dict, day: str) -> int:
    overnight = is_overnight(emp["shift_start"], emp["shift_end"])
    shift_end = shift_datetime(day, emp["shift_end"], next_day=overnight)
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def is_half_day(hours: float | None) -> bool:
    return hours is not None and hours < HALF_DAY_HOURS


# ======================================================================
# SCHEMAS & VALIDATION
# ======================================================================

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
HHMM = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


# ---------- query-parameter validators ----------
def valid_date(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be a valid date in YYYY-MM-DD format")
    return value


def month_bounds(month: str) -> tuple[date, date]:
    """First and last day of a 'YYYY-MM' month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        year, mon = int(month[:4]), int(month[5:])
        return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM")


def valid_trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    start, end = date.fromisoformat(valid_date(date_from)), date.fromisoformat(valid_date(date_to))
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must be ordered and at most 92 days")
    return start, end


# ---------- request bodies ----------
class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(Body):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(Body):
    emp_code: str
    punched_at: EpochMs | None = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(Body):
    emp_code: str
    punched_at: EpochMs | None = None


class RegularizeIn(Body):
    status: Status | None = None
    punch_in: EpochMs | None = None
    punch_out: EpochMs | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ======================================================================
# QUERIES & PIPELINES
# ======================================================================

# ---------- small expression helpers ----------
def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def is_present(status: str) -> dict:
    return {"$in": [status, list(PRESENCE)]}


def is_weekday(day: str) -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": day}}}, 5]}


def present_value(status: str, half_day: str, day: str | None = None) -> dict:
    """1 for a present day, 0.5 for a half day, 0 otherwise (optionally weekdays only)."""
    condition = is_present(status)
    if day is not None:
        condition = {"$and": [condition, is_weekday(day)]}
    return {"$cond": [condition, {"$cond": [half_day, 0.5, 1]}, 0]}


def date_range(start: date, end: date) -> dict:
    return {"$gte": start.isoformat(), "$lte": end.isoformat()}


# ---------- attendance list filter ----------
def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from and date_to and valid_date(date_from) > valid_date(date_to):
        raise HTTPException(422, "date_from must not exceed date_to")
    bounds = {}
    if date_from:
        bounds["$gte"] = valid_date(date_from)
    if date_to:
        bounds["$lte"] = valid_date(date_to)
    if bounds:
        query["date"] = bounds
    return query


# ---------- employee monthly ----------
def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": date_range(start, end)}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value("$status", "$half_day", day="$date")},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if({"$gt": ["$late_minutes", 0]}),
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


# ---------- department summary ----------
def department_summary_pipeline(start: date, end: date, department: str | None) -> list:
    employee_filter = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_filter["department"] = department

    logs_in_month = {"$match": {"$expr": {"$and": [
        {"$eq": ["$emp_code", "$$code"]},
        {"$gte": ["$date", start.isoformat()]},
        {"$lte": ["$date", end.isoformat()]},
        {"$gte": ["$date", "$$joined"]},
    ]}}}

    worked_hours = {"$cond": [
        {"$and": [is_present("$logs.status"), {"$ne": ["$logs.work_hours", None]}]},
        "$logs.work_hours",
        None,
    ]}

    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [logs_in_month],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": present_value("$logs.status", "$logs.half_day", day="$logs.date")},
            "hours": {"$avg": worked_hours},
            "late_count": count_if({"$gt": ["$logs.late_minutes", 0]}),
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": count_if({"$eq": ["$logs.status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$logs.status", "ON_DUTY"]}),
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "avg_work_hours": {"$round": ["$hours", 2]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


# ---------- late leaderboard ----------
def leaderboard_pipeline(month: str, department: str | None, limit: int | None = None) -> list:
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": date_range(start, end), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.append({"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}})
    if limit is not None:
        pipeline.append({"$match": {"rank": {"$lte": limit}}})
    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
            "rank": 1,
        }},
    ]
    return pipeline


# ---------- department trend ----------
def trend_pipeline(department: str, start: date, end: date) -> list:
    first_day = datetime.combine(start, time.min, UTC)
    day_count = (end - start).days + 1

    attendance_on_day = {"$lookup": {
        "from": "attendance_logs",
        "let": {"d": "$date", "codes": "$codes"},
        "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
            {"$group": {
                "_id": None,
                "present_count": {"$sum": present_value("$status", "$half_day")},
                "late_count": count_if({"$gt": ["$late_minutes", 0]}),
            }},
        ],
        "as": "daily",
    }}
    headcount_on_day = {"$lookup": {
        "from": "employees",
        "let": {"d": "$date"},
        "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"},
        ],
        "as": "hc",
    }}

    def first_or_zero(array_field: str) -> dict:
        return {"$ifNull": [{"$arrayElemAt": [array_field, 0]}, 0]}

    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$addToSet": "$emp_code"}}},
        {"$project": {"codes": 1, "days": {"$map": {
            "input": {"$range": [0, day_count]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": first_day, "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$days"},
        {"$set": {
            "date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d"}},
            "is_working_day": {"$not": [{"$in": [{"$isoDayOfWeek": "$days"}, [6, 7]]}]},
        }},
        attendance_on_day,
        headcount_on_day,
        {"$set": {
            "present_count": first_or_zero("$daily.present_count"),
            "late_count": first_or_zero("$daily.late_count"),
            "headcount": first_or_zero("$hc.n"),
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


def weekday_count(start: date, end: date) -> int:
    days = (start + timedelta(days=n) for n in range((end - start).days + 1))
    return sum(1 for d in days if d.weekday() < 5)


# ======================================================================
# API
# ======================================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# =============================== helpers ===============================
def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    return emp


def serialize(doc: dict) -> dict:
    """Convert a Mongo attendance document into the API response shape."""
    doc["id"] = str(doc.pop("_id"))
    doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
    doc["history"] = [{**e, "at": to_epoch_ms(e.get("at"))} for e in doc.get("history", [])]
    doc["half_day"] = doc.get("half_day", False)
    doc["late_minutes"] = doc.get("late_minutes", 0)
    doc["overtime_minutes"] = doc.get("overtime_minutes", 0)
    return doc


def page_of(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# =============================== health ===============================
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ============================== employees ==============================
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now_utc()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id")
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [{**e, "created_at": to_epoch_ms(e["created_at"])} for e in cursor]
    return page_of(items, db.employees.count_documents(query), page, page_size)


# ============================== attendance ==============================
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    punched_at = resolve_time(body.punched_at)
    day = attendance_day(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # adds "_id" to doc
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    punched_at = resolve_time(body.punched_at)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    started = record["punch_in"]
    if punched_at <= started or punched_at - started > MAX_SHIFT_LENGTH:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    hours = compute_work_hours(started, punched_at)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # guards against double punch-out
        {"$set": {
            "punch_out": punched_at,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(punched_at, emp, record["date"]),
            "half_day": is_half_day(hours),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize(d) for d in cursor]
    return page_of(items, db.attendance_logs.count_documents(query), page, page_size)


# ------------------------------ regularization ------------------------------
def corrected_values(rec: dict, emp: dict, day: str, supplied: dict) -> dict:
    """Apply a correction on top of an existing record and recompute derived fields."""
    status = supplied.get("status") or rec.get("status")
    supplied_punch = "punch_in" in supplied or "punch_out" in supplied
    if status in ("ABSENT", "LEAVE") and supplied_punch:
        raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")

    if status in ("ABSENT", "LEAVE"):
        punch_in = punch_out = None
    else:
        punch_in = from_epoch_ms(supplied["punch_in"]) if "punch_in" in supplied else rec.get("punch_in")
        punch_out = from_epoch_ms(supplied["punch_out"]) if "punch_out" in supplied else rec.get("punch_out")

    if status in PRESENCE and punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if punch_in is not None and attendance_day(punch_in, emp) != day:
        raise HTTPException(422, "punch_in must fall on the attendance date")
    if punch_out is not None and (
        punch_in is None or punch_out <= punch_in or punch_out - punch_in > MAX_SHIFT_LENGTH
    ):
        raise HTTPException(422, "invalid punch_out")

    hours = compute_work_hours(punch_in, punch_out) if punch_in and punch_out else None
    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": hours,
        "late_minutes": compute_late_minutes(punch_in, emp["shift_start"], day) if punch_in else 0,
        "overtime_minutes": compute_overtime(punch_out, emp, day) if punch_out else 0,
        "half_day": is_half_day(hours),
    }


FIELD_DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def diff_changes(rec: dict, values: dict) -> dict:
    """{field: {"from": old, "to": new}} for every field that actually changed."""
    changes = {}
    for field, new in values.items():
        old = rec.get(field, FIELD_DEFAULTS.get(field))
        if field in ("punch_in", "punch_out"):
            old, new = to_epoch_ms(old), to_epoch_ms(new)
        if old != new:
            changes[field] = {"from": old, "to": new}
    return changes


@app.patch("/attendance/{emp_code}/{day}")
def regularize_attendance(emp_code: str, day: str, body: RegularizeIn):
    valid_date(day)
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not rec:
        raise HTTPException(404, "no attendance record for this date")

    values = corrected_values(rec, emp, day, body.model_dump(exclude_none=True))
    changes = diff_changes(rec, values)
    if not changes:
        raise HTTPException(422, "correction does not change the attendance record")

    audit_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        # optimistic lock: only update if history is unchanged (None also matches a missing field)
        {"_id": rec["_id"], "history": rec.get("history")},
        {"$set": values, "$push": {"history": audit_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "attendance record was changed concurrently")
    return serialize(updated)


# ============================== analytics ==============================
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    emp = get_employee(emp_code)

    first_counted_day = max(start, date.fromisoformat(emp["joined_on"]))
    working_days = weekday_count(first_counted_day, end)
    stats = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start, end)), {})
    present = stats.get("present_days", 0)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present * 100 / working_days) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start, end = month_bounds(month)
    pipeline = department_summary_pipeline(start, end, department)
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    pipeline = leaderboard_pipeline(month, department, limit)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = valid_trend_range(from_, to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": list(db.employees.aggregate(trend_pipeline(department, start, end)))}


# ================================ admin ================================
def explain_target(endpoint: str, p: dict) -> tuple[str, dict]:
    """Build the (collection, command) that an endpoint would run, for query-plan inspection."""
    if endpoint == "attendance_list":
        query = attendance_filter(p["emp_code"], p["date_from"], p["date_to"], p["status"])
        return "attendance_logs", {
            "find": "attendance_logs", "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (p["page"] - 1) * p["page_size"], "limit": p["page_size"],
        }

    if endpoint == "employee_monthly":
        if not p["emp_code"] or not p["month"]:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(p["month"])
        pipeline = employee_monthly_pipeline(p["emp_code"], start, end)
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

    if endpoint == "department_summary":
        if not p["month"]:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(p["month"])
        pipeline = department_summary_pipeline(start, end, p["department"])
        return "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

    if endpoint == "late_leaderboard":
        if not p["month"]:
            raise HTTPException(422, "month is required")
        pipeline = leaderboard_pipeline(p["month"], p["department"])
        return "attendance_logs", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}

    if endpoint == "department_trend":
        if not (p["department"] and p["from"] and p["to"]):
            raise HTTPException(422, "department, from and to are required")
        start, end = valid_trend_range(p["from"], p["to"])
        pipeline = trend_pipeline(p["department"], start, end)
        return "employees", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}

    raise HTTPException(422, "unknown endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    params = {
        "emp_code": emp_code, "month": month, "department": department,
        "date_from": date_from, "date_to": date_to, "status": status,
        "from": from_, "to": to, "page": page, "page_size": page_size,
    }
    collection, command = explain_target(endpoint, params)
    result = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": result}