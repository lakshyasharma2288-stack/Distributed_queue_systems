import argparse
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.attempt import JobAttempt
from app.models.dead_letter import DeadLetter
from app.models.job import Job


def read_dead_letters(
    session: Session,
    *,
    limit: int,
    job_id: UUID | None = None,
) -> list[dict[str, object]]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")

    statement = (
        select(DeadLetter, Job)
        .join(Job, Job.id == DeadLetter.job_id)
        .order_by(DeadLetter.created_at.desc())
        .limit(limit)
    )
    if job_id is not None:
        statement = statement.where(DeadLetter.job_id == job_id)

    entries: list[dict[str, object]] = []
    for dead_letter, job in session.execute(statement):
        attempts = session.scalars(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id)
            .order_by(JobAttempt.attempt_number)
        ).all()
        entries.append(
            {
                "job_id": str(job.id),
                "task_type": job.task_type,
                "payload": job.payload,
                "final_error": {
                    "code": dead_letter.final_error_code,
                    "message": dead_letter.final_error_message,
                },
                "created_at": dead_letter.created_at.isoformat(),
                "attempts": [
                    {
                        "attempt_number": attempt.attempt_number,
                        "worker_id": attempt.worker_id,
                        "started_at": attempt.started_at.isoformat(),
                        "ended_at": (
                            attempt.ended_at.isoformat()
                            if attempt.ended_at is not None
                            else None
                        ),
                        "outcome": attempt.outcome,
                        "error_code": attempt.error_code,
                        "error_message": attempt.error_message,
                        "result": attempt.result,
                    }
                    for attempt in attempts
                ],
            }
        )
    return entries


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect dead-lettered jobs.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--job-id", type=UUID)
    args = parser.parse_args()
    if not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")
    return args


def main() -> None:
    args = _parse_args()
    with SessionLocal() as session:
        entries = read_dead_letters(
            session,
            limit=args.limit,
            job_id=args.job_id,
        )
    print(json.dumps(entries, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
