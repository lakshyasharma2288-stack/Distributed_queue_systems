import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.uploads import _input_directory
from app.db.database import get_db
from app.models.image import Image
from app.schemas.image import ImageRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/images", tags=["images"])

def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")


def _get_image(db: Session, image_id: uuid.UUID) -> Image:
    try:
        image = db.get(Image, image_id)
    except SQLAlchemyError as exc:
        logger.exception("Could not retrieve image %s", image_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve image",
        ) from exc
    if image is None:
        raise _not_found()
    return image


def stored_image_path(image: Image) -> Path:
    directory = _input_directory()
    try:
        path = (directory / image.stored_filename).resolve(strict=True)
        path.relative_to(directory)
    except (OSError, ValueError) as exc:
        logger.warning("Stored image file missing image_id=%s", image.id)
        raise _not_found() from exc
    if not path.is_file():
        raise _not_found()
    return path


@router.get("", response_model=list[ImageRead])
def list_images(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ImageRead]:
    try:
        images = db.scalars(
            select(Image)
            .order_by(Image.created_at.desc(), Image.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    except SQLAlchemyError as exc:
        logger.exception("Could not retrieve images")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve images",
        ) from exc
    return [ImageRead.model_validate(image) for image in images]


@router.get("/{image_id}", response_model=ImageRead)
def get_image(image_id: uuid.UUID, db: Session = Depends(get_db)) -> ImageRead:
    return ImageRead.model_validate(_get_image(db, image_id))


@router.get("/{image_id}/file")
def get_image_file(image_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    image = _get_image(db, image_id)
    return FileResponse(
        stored_image_path(image),
        media_type=image.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
