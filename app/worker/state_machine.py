from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.elements import ColumnElement

from app.models.job import Job

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing"}),
    "processing": frozenset({"completed", "failed", "retry_scheduled"}),
    "retry_scheduled": frozenset({"pending"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


def transition_job(
    job_id: UUID,
    *,
    expected_status: str,
    new_status: str,
    values: dict[str, Any] | None = None,
    conditions: Sequence[ColumnElement[bool]] = (),
) -> Update:
    if new_status not in ALLOWED_TRANSITIONS.get(expected_status, frozenset()):
        raise ValueError(
            f"Invalid job transition: {expected_status} -> {new_status}"
        )
    transition_values = dict(values or {})
    if "status" in transition_values:
        raise ValueError("status must be set by the transition")
    transition_values["status"] = new_status
    return (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == expected_status,
            *conditions,
        )
        .values(**transition_values)
    )
