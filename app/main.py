"""FastAPI application for bearing exposure statistics."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from . import service
from .db import get_db_path, init_db, reader
from .service import InvalidRequest


# --------------------------------------------------------------------------
# Request / response schemas
# --------------------------------------------------------------------------


def _coerce_date(value: object) -> object:
    """Accept ``YYYY-MM-DD`` as well as ISO timestamps and ``date`` objects."""

    if isinstance(value, str):
        text = value.strip()
        if len(text) > 10 and ("T" in text or " " in text):
            return text[:10]
    return value


IsoDate = Annotated[date, BeforeValidator(_coerce_date)]

SerialField = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices(
            "serial_number", "serialNumber", "serial", "bearing_serial", "sn"
        ),
        serialization_alias="serial_number",
    ),
]
BatchField = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("batch", "batch_no", "batch_number", "batch_id"),
        serialization_alias="batch",
    ),
]
InstallDateField = Annotated[
    IsoDate,
    Field(
        validation_alias=AliasChoices(
            "first_install_date",
            "first_installed_on",
            "install_date",
            "installed_on",
        ),
        serialization_alias="first_install_date",
    ),
]
FailureDateField = Annotated[
    IsoDate | None,
    Field(
        validation_alias=AliasChoices(
            "first_failure_date", "first_failed_on", "failure_date", "failed_on"
        ),
        serialization_alias="first_failure_date",
    ),
]
RecordIdAliases = AliasChoices(
    "record_id", "recordId", "record_no", "record_number"
)
StartDateField = Annotated[
    IsoDate,
    Field(
        validation_alias=AliasChoices("start_date", "start", "started_on", "from_date"),
        serialization_alias="start_date",
    ),
]
EndDateField = Annotated[
    IsoDate,
    Field(
        validation_alias=AliasChoices("end_date", "end", "ended_on", "to_date"),
        serialization_alias="end_date",
    ),
]


class BearingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    serial_number: SerialField
    batch: BatchField
    first_install_date: InstallDateField
    first_failure_date: FailureDateField = None

    @field_validator("serial_number", "batch")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class FailureUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    first_failure_date: FailureDateField = None


class IntervalUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    record_id: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=200,
            validation_alias=RecordIdAliases,
            serialization_alias="record_id",
        ),
    ] = None
    start_date: StartDateField
    end_date: EndDateField

    @field_validator("record_id")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _ordered(self) -> "IntervalUpsert":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class IntervalOut(BaseModel):
    record_id: str
    start_date: date
    end_date: date


class BearingOut(BaseModel):
    serial_number: str
    batch: str
    first_install_date: date
    first_failure_date: date | None
    intervals: list[IntervalOut]


class ExposureResponse(BaseModel):
    batch: str
    age_start: int
    age_end: int
    at_risk_bearing_days: int
    failure_count: int


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(get_db_path())
    yield


app = FastAPI(
    title="Bearing Early-Failure Exposure Service",
    version="1.0.0",
    description=(
        "Ledger of bearing batches, first installation/failure dates, and "
        "operation intervals.  Exposes saved per-batch at-risk bearing-day and "
        "failure counts for closed age ranges (age 0 = first installation day)."
    ),
    lifespan=lifespan,
)


def db_path() -> str:
    return get_db_path()


def _bearing_out(record: service.BearingRecord) -> BearingOut:
    return BearingOut(
        serial_number=record.serial_number,
        batch=record.batch,
        first_install_date=record.first_install_date,
        first_failure_date=record.first_failure_date,
        intervals=[
            IntervalOut(record_id=rid, start_date=start, end_date=end)
            for rid, start, end in record.intervals
        ],
    )


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.exception_handler(service.ServiceError)
async def _service_error_handler(request: Request, exc: service.ServiceError) -> JSONResponse:
    return _error(exc.status_code, exc.detail)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.get("loc", ())[1:])
        message = first.get("msg", "invalid request")
        detail = f"{location}: {message}" if location else message
    else:
        detail = "invalid request"
    return _error(422, detail)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}


router = APIRouter()


@router.get("/batches", tags=["query"])
def batches() -> dict[str, list[str]]:
    with reader(db_path()) as conn:
        return {"batches": list(service.list_batches(conn))}


@router.post("/bearings", tags=["ledger"])
def create_bearing(payload: BearingCreate) -> JSONResponse:
    record, created = service.register_bearing(
        db_path(),
        payload.serial_number,
        payload.batch,
        payload.first_install_date,
        payload.first_failure_date,
    )
    body = _bearing_out(record).model_dump(mode="json")
    body["created"] = created
    return JSONResponse(status_code=201 if created else 200, content=body)


@router.get("/bearings", response_model=list[BearingOut], tags=["ledger"])
def read_bearings() -> list[BearingOut]:
    with reader(db_path()) as conn:
        return [_bearing_out(record) for record in service.list_bearings(conn)]


@router.get("/bearings/{serial_number}", response_model=BearingOut, tags=["ledger"])
def read_bearing(serial_number: Annotated[str, Path(min_length=1)]) -> BearingOut:
    with reader(db_path()) as conn:
        return _bearing_out(service.get_bearing(conn, serial_number))


@router.put(
    "/bearings/{serial_number}/failure",
    response_model=BearingOut,
    tags=["ledger"],
)
@router.post(
    "/bearings/{serial_number}/failure",
    response_model=BearingOut,
    tags=["ledger"],
    include_in_schema=False,
)
def report_failure(
    serial_number: Annotated[str, Path(min_length=1)],
    payload: FailureUpdate = Body(...),
) -> BearingOut:
    record = service.set_first_failure_date(
        db_path(), serial_number, payload.first_failure_date
    )
    return _bearing_out(record)


@router.put(
    "/bearings/{serial_number}/intervals/{record_id}",
    response_model=BearingOut,
    tags=["ledger"],
)
def put_interval(
    serial_number: Annotated[str, Path(min_length=1)],
    record_id: Annotated[str, Path(min_length=1)],
    payload: IntervalUpsert = Body(...),
) -> BearingOut:
    if payload.record_id is not None and payload.record_id != record_id:
        raise InvalidRequest("record_id in the path and body must match")
    record, _ = service.put_operation_interval(
        db_path(),
        serial_number,
        record_id,
        payload.start_date,
        payload.end_date,
    )
    return _bearing_out(record)


@router.post("/bearings/{serial_number}/intervals", tags=["ledger"])
def post_interval(
    serial_number: Annotated[str, Path(min_length=1)],
    payload: IntervalUpsert = Body(...),
) -> JSONResponse:
    if payload.record_id is None:
        raise InvalidRequest("record_id is required")
    record, created = service.put_operation_interval(
        db_path(),
        serial_number,
        payload.record_id,
        payload.start_date,
        payload.end_date,
    )
    return JSONResponse(
        status_code=201 if created else 200,
        content=_bearing_out(record).model_dump(mode="json"),
    )


@router.delete(
    "/bearings/{serial_number}/intervals/{record_id}",
    response_model=BearingOut,
    tags=["ledger"],
)
def delete_interval(
    serial_number: Annotated[str, Path(min_length=1)],
    record_id: Annotated[str, Path(min_length=1)],
) -> BearingOut:
    record = service.delete_operation_interval(db_path(), serial_number, record_id)
    return _bearing_out(record)


def _exposure(batch: str, age_start: int, age_end: int) -> ExposureResponse:
    if age_end < age_start:
        raise InvalidRequest("age_end must be greater than or equal to age_start")
    with reader(db_path()) as conn:
        bearing_days, failures = service.query_batch_stats(conn, batch, age_start, age_end)
    return ExposureResponse(
        batch=batch,
        age_start=age_start,
        age_end=age_end,
        at_risk_bearing_days=bearing_days,
        failure_count=failures,
    )


@router.get("/exposure", response_model=ExposureResponse, tags=["query"])
def exposure(
    batch: Annotated[
        str,
        Query(
            min_length=1,
            validation_alias=AliasChoices("batch", "batch_no", "batch_number", "batch_id"),
        ),
    ],
    age_start: Annotated[
        int,
        Query(
            ge=0,
            validation_alias=AliasChoices(
                "age_start", "start_age", "day_from", "from_day", "min_age"
            ),
        ),
    ] = 0,
    age_end: Annotated[
        int | None,
        Query(
            ge=0,
            validation_alias=AliasChoices(
                "age_end", "end_age", "day_to", "to_day", "max_age"
            ),
        ),
    ] = None,
) -> ExposureResponse:
    if age_end is None:
        age_end = age_start
    return _exposure(batch, age_start, age_end)


@router.get(
    "/batches/{batch}/exposure",
    response_model=ExposureResponse,
    tags=["query"],
)
def batch_exposure(
    batch: Annotated[str, Path(min_length=1)],
    age_start: Annotated[
        int,
        Query(
            ge=0,
            validation_alias=AliasChoices(
                "age_start", "start_age", "day_from", "from_day", "min_age"
            ),
        ),
    ] = 0,
    age_end: Annotated[
        int | None,
        Query(
            ge=0,
            validation_alias=AliasChoices(
                "age_end", "end_age", "day_to", "to_day", "max_age"
            ),
        ),
    ] = None,
) -> ExposureResponse:
    if age_end is None:
        age_end = age_start
    return _exposure(batch, age_start, age_end)


@router.get(
    "/batches/{batch}/exposure/points",
    tags=["query"],
    include_in_schema=False,
)
def batch_exposure_points(
    batch: Annotated[str, Path(min_length=1)],
    age_start: Annotated[int, Query(ge=0)] = 0,
    age_end: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, object]:
    """Optional per-age view backed by the saved segments.

    The range in this response is expanded from stored segments only; no
    bearing's operation interval is re-expanded and nothing is persisted.
    """

    if age_end is None:
        age_end = age_start
    if age_end < age_start:
        raise InvalidRequest("age_end must be greater than or equal to age_start")
    with reader(db_path()) as conn:
        rows = service.query_batch_series(conn, batch, age_start, age_end)
    return {
        "batch": batch,
        "age_start": age_start,
        "age_end": age_end,
        "points": [
            {
                "age": age,
                "at_risk_bearing_days": bearing_days,
                "failure_count": failures,
            }
            for age, bearing_days, failures in rows
        ],
    }


# Mount the same behaviour under the two conventional API prefixes as well, so
# clients may use /bearings, /api/bearings, or /api/v1/bearings.
app.include_router(router)
app.include_router(router, prefix="/api", include_in_schema=False)
app.include_router(router, prefix="/api/v1", include_in_schema=False)
