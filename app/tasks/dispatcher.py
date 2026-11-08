import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.tasks.handlers.email import send_email
from app.tasks.handlers.image import resize_image
from app.tasks.handlers.pdf import generate_pdf
from app.tasks.schemas import (
    GeneratePDFPayload,
    HandlerResult,
    ResizeImagePayload,
    SendEmailPayload,
    failure_result,
)

logger = logging.getLogger(__name__)

TaskHandler = Callable[..., HandlerResult]
TASK_REGISTRY: dict[str, tuple[type[BaseModel], TaskHandler]] = {
    "send_email": (SendEmailPayload, send_email),
    "resize_image": (ResizeImagePayload, resize_image),
    "generate_pdf": (GeneratePDFPayload, generate_pdf),
}


def _validation_message(exc: ValidationError) -> str:
    details = [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_input=False, include_context=False)[:10]
    ]
    return "Invalid task payload: " + "; ".join(details)


def _log_result(result: HandlerResult) -> None:
    error_code = result.error.code if result.error is not None else "NONE"
    logger.info(
        "Task %s outcome=%s error_code=%s",
        result.task_type,
        "success" if result.success else "failure",
        error_code,
    )


def dispatch(
    task_type: str,
    payload: dict[str, Any],
    *,
    job_id: UUID | None = None,
) -> HandlerResult:
    registration = TASK_REGISTRY.get(task_type)
    if registration is None:
        result = failure_result(
            task_type,
            "UNSUPPORTED_TASK",
            f"Unsupported task type: {task_type}",
        )
        _log_result(result)
        return result

    payload_model, handler = registration
    try:
        validated_payload = payload_model.model_validate(payload)
    except ValidationError as exc:
        result = failure_result(
            task_type,
            "VALIDATION_ERROR",
            _validation_message(exc),
        )
        _log_result(result)
        return result

    try:
        if job_id is None:
            result = handler(validated_payload)
        else:
            result = handler(validated_payload, job_id=job_id)
    except Exception as exc:
        logger.error(
            "Task %s outcome=failure error_code=INTERNAL_ERROR",
            task_type,
            exc_info=(type(exc), None, exc.__traceback__),
        )
        result = failure_result(
            task_type,
            "INTERNAL_ERROR",
            "An unexpected error occurred while handling the task.",
            retryable=True,
        )

    _log_result(result)
    return result
