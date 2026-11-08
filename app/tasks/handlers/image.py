import logging
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.Image import DecompressionBombError, Resampling
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.image import Image as ImageRecord
from app.tasks.handlers._files import (
    exclusive_output,
    job_output_directory,
    safe_output_path,
)
from app.tasks.schemas import HandlerResult, ResizeImagePayload, failure_result

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


def _stored_filename(image_id: UUID) -> str | None:
    with SessionLocal() as session:
        image = session.get(ImageRecord, image_id)
        return image.stored_filename if image is not None else None


def _resolve_input_path(input_path: str, input_dir: Path) -> Path:
    candidate = Path(input_path)
    if not candidate.is_absolute():
        candidate = input_dir / candidate
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(input_dir)
    return resolved


def _output_size(
    original_size: tuple[int, int],
    payload: ResizeImagePayload,
) -> tuple[int, int]:
    original_width, original_height = original_size
    if payload.keep_aspect_ratio:
        if payload.width is not None and payload.height is not None:
            scale = min(
                payload.width / original_width,
                payload.height / original_height,
            )
        elif payload.width is not None:
            scale = payload.width / original_width
        elif payload.height is not None:
            scale = payload.height / original_height
        else:
            raise ValueError("at least one of width or height is required")
        if not payload.allow_upscale:
            scale = min(scale, 1.0)
        return (
            max(1, round(original_width * scale)),
            max(1, round(original_height * scale)),
        )

    width = payload.width
    height = payload.height
    if width is None or height is None:
        raise ValueError("width and height are both required")
    if not payload.allow_upscale and (
        width > original_width or height > original_height
    ):
        raise ValueError("requested dimensions would upscale the image")
    return width, height


def resize_image(
    payload: ResizeImagePayload,
    *,
    job_id: UUID | None = None,
) -> HandlerResult:
    if not settings.image_input_dir or not settings.image_input_dir.strip():
        return failure_result(
            "resize_image",
            "MISSING_CONFIG",
            "IMAGE_INPUT_DIR must be configured.",
        )
    if not settings.image_output_dir or not settings.image_output_dir.strip():
        return failure_result(
            "resize_image",
            "MISSING_CONFIG",
            "IMAGE_OUTPUT_DIR must be configured.",
        )
    if settings.max_image_pixels <= 0:
        return failure_result(
            "resize_image",
            "MISSING_CONFIG",
            "MAX_IMAGE_PIXELS must be a positive integer.",
        )

    input_dir = Path(settings.image_input_dir).expanduser().resolve()
    if not input_dir.is_dir():
        return failure_result(
            "resize_image",
            "MISSING_CONFIG",
            "IMAGE_INPUT_DIR must name an existing directory.",
        )

    try:
        requested_input = payload.input_path
        if payload.image_id is not None:
            requested_input = _stored_filename(payload.image_id)
            if requested_input is None:
                return failure_result(
                    "resize_image",
                    "FILE_NOT_FOUND",
                    "The input image was not found.",
                )
        input_path = _resolve_input_path(requested_input, input_dir)
    except SQLAlchemyError:
        logger.warning("Task resize_image outcome=failure error_code=IO_ERROR")
        return failure_result(
            "resize_image",
            "IO_ERROR",
            "The image record could not be read.",
            retryable=True,
        )
    except FileNotFoundError:
        return failure_result(
            "resize_image",
            "FILE_NOT_FOUND",
            "The input image was not found.",
        )
    except (OSError, ValueError):
        return failure_result(
            "resize_image",
            "VALIDATION_ERROR",
            "input_path must resolve to a file inside IMAGE_INPUT_DIR.",
        )

    if not input_path.is_file():
        return failure_result(
            "resize_image",
            "FILE_NOT_FOUND",
            "The input image was not found.",
        )

    output_dir = Path(settings.image_output_dir).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir = output_dir.resolve(strict=True)
        if job_id is not None:
            output_dir = job_output_directory(output_dir, job_id)
        with Image.open(input_path) as source:
            detected_format = source.format
            if detected_format not in SUPPORTED_FORMATS:
                return failure_result(
                    "resize_image",
                    "UNSUPPORTED_FORMAT",
                    "Supported input and output formats are JPEG, PNG, and WEBP.",
                )
            original_raw_size = source.size
            if original_raw_size[0] * original_raw_size[1] > settings.max_image_pixels:
                return failure_result(
                    "resize_image",
                    "VALIDATION_ERROR",
                    "The image exceeds the configured pixel limit.",
                )

            oriented = ImageOps.exif_transpose(source)
            try:
                original_size = oriented.size
                try:
                    new_size = _output_size(original_size, payload)
                except ValueError as exc:
                    return failure_result(
                        "resize_image",
                        "VALIDATION_ERROR",
                        str(exc),
                    )

                output_format = (
                    payload.output_format.upper()
                    if payload.output_format is not None
                    else detected_format
                )
                if output_format == "JPG":
                    output_format = "JPEG"
                if output_format not in SUPPORTED_FORMATS:
                    return failure_result(
                        "resize_image",
                        "UNSUPPORTED_FORMAT",
                        "Supported input and output formats are JPEG, PNG, and WEBP.",
                    )

                try:
                    output_path = safe_output_path(
                        output_dir,
                        payload.output_filename
                        or (
                            f"resized_{job_id.hex}{SUPPORTED_FORMATS[output_format]}"
                            if job_id is not None
                            else None
                        ),
                        default_stem="resized",
                        extension=SUPPORTED_FORMATS[output_format],
                    )
                except ValueError:
                    return failure_result(
                        "resize_image",
                        "VALIDATION_ERROR",
                        "output_filename must be a simple filename without path components.",
                    )
                if output_path.resolve() == input_path:
                    return failure_result(
                        "resize_image",
                        "VALIDATION_ERROR",
                        "The output path must not be the input image.",
                    )

                if job_id is not None and output_path.is_symlink():
                    return failure_result(
                        "resize_image",
                        "IO_ERROR",
                        "The job output path must not be a symbolic link.",
                    )
                if job_id is not None and output_path.is_file():
                    try:
                        with Image.open(output_path) as existing:
                            existing_format = existing.format
                            existing_size = existing.size
                            existing.verify()
                    except (DecompressionBombError, UnidentifiedImageError, OSError):
                        return failure_result(
                            "resize_image",
                            "IO_ERROR",
                            "The job output exists but is not a valid image.",
                        )
                    if (
                        existing_format == output_format
                        and existing_size == new_size
                    ):
                        return HandlerResult(
                            success=True,
                            task_type="resize_image",
                            data={
                                "output_path": str(output_path),
                                "original_dimensions": {
                                    "width": original_size[0],
                                    "height": original_size[1],
                                },
                                "new_dimensions": {
                                    "width": new_size[0],
                                    "height": new_size[1],
                                },
                                "format": output_format,
                            },
                        )
                    return failure_result(
                        "resize_image",
                        "IO_ERROR",
                        "The job output exists but does not match the requested image.",
                    )

                if new_size != original_size:
                    if payload.keep_aspect_ratio and not payload.allow_upscale:
                        oriented.thumbnail(
                            new_size,
                            Resampling.LANCZOS,
                            reducing_gap=3.0,
                        )
                    else:
                        resized = oriented.resize(
                            new_size,
                            Resampling.LANCZOS,
                            reducing_gap=3.0,
                        )
                        if oriented is not source:
                            oriented.close()
                        oriented = resized

                image_to_save = oriented
                if output_format == "JPEG" and image_to_save.mode not in {
                    "RGB",
                    "L",
                }:
                    image_to_save = image_to_save.convert("RGB")
                try:
                    with exclusive_output(output_path) as output_file:
                        image_to_save.save(output_file, format=output_format)
                finally:
                    if image_to_save is not oriented:
                        image_to_save.close()
            finally:
                if oriented is not source:
                    oriented.close()
    except FileExistsError:
        return failure_result(
            "resize_image",
            "IO_ERROR",
            "The output file already exists; choose a different output_filename.",
        )
    except DecompressionBombError:
        return failure_result(
            "resize_image",
            "VALIDATION_ERROR",
            "The image exceeds Pillow's configured safety limit.",
        )
    except (UnidentifiedImageError, OSError):
        logger.warning("Task resize_image outcome=failure error_code=IO_ERROR")
        return failure_result(
            "resize_image",
            "IO_ERROR",
            "The image could not be read or the output could not be written.",
            retryable=True,
        )

    return HandlerResult(
        success=True,
        task_type="resize_image",
        data={
            "output_path": str(output_path),
            "original_dimensions": {
                "width": original_size[0],
                "height": original_size[1],
            },
            "new_dimensions": {"width": new_size[0], "height": new_size[1]},
            "format": output_format,
        },
    )
