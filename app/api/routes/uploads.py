import logging
import os
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from PIL import Image as PILImage, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.models.image import Image

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/uploads", tags=["uploads"])

CHUNK_SIZE = 64 * 1024
SUPPORTED_FORMATS: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
CONTENT_TYPES: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


class ImageUploadResponse(BaseModel):
    id: uuid.UUID
    filename: str
    input_path: str
    format: Literal["JPEG", "PNG", "WEBP"]
    size_bytes: int
    width: int
    height: int


def _input_directory() -> Path:
    directory = settings.image_input_dir
    if not directory or not directory.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image storage is unavailable.",
        )

    try:
        configured_path = Path(directory).expanduser()
        configured_path.mkdir(parents=True, exist_ok=True)
        resolved_path = configured_path.resolve(strict=True)
        if not resolved_path.is_dir():
            raise OSError("Configured image input location is not a directory")
        return resolved_path
    except (OSError, RuntimeError) as exc:
        logger.error("Image storage unavailable error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image storage is unavailable.",
        ) from exc


def _inspect_image(path: Path) -> tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(path) as image:
                detected_format = image.format
                if detected_format not in SUPPORTED_FORMATS:
                    raise HTTPException(
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        detail="Only JPEG, PNG, and WEBP images are supported.",
                    )

                width, height = image.size
                if width < 1 or height < 1:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="Uploaded image has invalid dimensions.",
                    )
                if settings.max_image_pixels <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Image validation is unavailable.",
                    )
                if width * height > settings.max_image_pixels:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Image exceeds the maximum pixel limit.",
                    )

                image.verify()
                return detected_format, width, height
    except HTTPException:
        raise
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning) as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Image exceeds the maximum pixel limit.",
        ) from exc
    except UnidentifiedImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Uploaded file is not a supported raster image.",
        ) from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded file is not a valid image.",
        ) from exc


def _reserve_destination(directory: Path, extension: str) -> Path:
    for _ in range(10):
        destination = directory / f"{uuid.uuid4().hex}{extension}"
        try:
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            continue

        try:
            os.close(descriptor)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return destination

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Image storage is unavailable.",
    )


def _remove_created_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove upload temporary file error_type=%s", type(exc).__name__)


@router.post(
    "/images",
    response_model=ImageUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ImageUploadResponse:
    input_directory = _input_directory()
    temporary_path: Path | None = None
    reserved_path: Path | None = None
    stored_path: Path | None = None
    completed = False

    try:
        size_bytes = 0
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".upload-",
            suffix=".tmp",
            dir=input_directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            while True:
                chunk = file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > settings.max_image_upload_size_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Image exceeds the maximum upload size.",
                    )
                temporary_file.write(chunk)
            if size_bytes == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Uploaded file is empty.",
                )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        detected_format, width, height = _inspect_image(temporary_path)
        extension = SUPPORTED_FORMATS[detected_format]
        reserved_path = _reserve_destination(input_directory, extension)
        os.replace(temporary_path, reserved_path)
        temporary_path = None
        stored_path = reserved_path
        reserved_path = None

        try:
            stored_path.resolve(strict=True).relative_to(input_directory)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Image storage is unavailable.",
            ) from exc

        record = Image(
            id=uuid.uuid4(),
            stored_filename=stored_path.name,
            content_type=CONTENT_TYPES[detected_format],
            size_bytes=size_bytes,
            width=width,
            height=height,
        )
        try:
            db.add(record)
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("Image metadata save failed error_type=%s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not save image.",
            ) from exc

        response = ImageUploadResponse(
            id=record.id,
            filename=stored_path.name,
            input_path=stored_path.name,
            format=detected_format,
            size_bytes=size_bytes,
            width=width,
            height=height,
        )
        completed = True
        return response
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        logger.error("Image upload failed error_type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image storage is unavailable.",
        ) from exc
    finally:
        if not completed:
            _remove_created_file(temporary_path)
            _remove_created_file(reserved_path)
            _remove_created_file(stored_path)
