import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.models.job import Job
from app.models.outbox_event import OutboxEvent
from app.schemas.job import JobCreate, JobCreated, JobRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobCreated, status_code=status.HTTP_201_CREATED)
def submit_job(
    job_request: JobCreate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
    ),
) -> JobCreated:
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Idempotency-Key must not be blank",
            )

    job = Job(
        id=uuid.uuid4(),
        task_type=job_request.task_type,
        payload=job_request.payload,
        idempotency_key=idempotency_key,
        status="pending",
        max_attempts=settings.default_max_attempts,
    )
    try:
        db.add(job)
        db.flush()
        db.add(
            OutboxEvent(
                event_type="job_created",
                job_id=job.id,
                payload={
                    "job_id": str(job.id),
                    "task_type": job.task_type,
                    "priority": job.priority,
                },
            )
        )
        db.commit()
        db.refresh(job)
    except IntegrityError as exc:
        db.rollback()
        if idempotency_key is not None:
            try:
                existing = db.scalar(
                    select(Job).where(Job.idempotency_key == idempotency_key)
                )
            except SQLAlchemyError as lookup_exc:
                logger.exception("Could not retrieve idempotent job")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save job",
                ) from lookup_exc
            if existing is not None:
                if (
                    existing.task_type != job_request.task_type
                    or existing.payload != job_request.payload
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency-Key was already used for a different request",
                    ) from exc
                return JobCreated(id=existing.id, status=existing.status)
        logger.exception("Could not save submitted job")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save job",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Could not save submitted job")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save job",
        ) from exc

    return JobCreated(id=job.id, status=job.status)


@router.get("", response_model=list[JobRead])
def list_jobs(db: Session = Depends(get_db)) -> list[JobRead]:
    try:
        jobs = db.scalars(
            select(Job).order_by(Job.created_at.desc(), Job.id.desc())
        ).all()
    except SQLAlchemyError as exc:
        logger.exception("Could not retrieve jobs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve jobs",
        ) from exc

    return [JobRead.model_validate(job) for job in jobs]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> JobRead:
    try:
        job = db.scalar(select(Job).where(Job.id == job_id))
    except SQLAlchemyError as exc:
        logger.exception("Could not retrieve job %s", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve job",
        ) from exc

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    return JobRead.model_validate(job)


@router.get("/{job_id}/output")
def get_job_output(job_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    try:
        job = db.scalar(select(Job).where(Job.id == job_id))
    except SQLAlchemyError as exc:
        logger.exception("Could not retrieve job %s", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve job",
        ) from exc

    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.task_type != "resize_image" or job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job has no image output",
        )

    data = (job.result or {}).get("data") or {}
    output_path = data.get("output_path")
    media_types = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Output not found"
    )
    if not isinstance(output_path, str) or not settings.image_output_dir:
        raise not_found
    try:
        base = Path(settings.image_output_dir).expanduser().resolve(strict=True)
        path = Path(output_path).resolve(strict=True)
        path.relative_to(base)
    except (OSError, ValueError) as exc:
        raise not_found from exc
    media_type = media_types.get(path.suffix.lower())
    if media_type is None or not path.is_file():
        raise not_found
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )