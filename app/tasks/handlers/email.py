import logging
from uuid import UUID

import resend
from resend.exceptions import ResendError

from app.core.config import settings
from app.tasks.schemas import HandlerResult, SendEmailPayload, failure_result

logger = logging.getLogger(__name__)


def send_email(
    payload: SendEmailPayload,
    *,
    job_id: UUID | None = None,
) -> HandlerResult:
    api_key = settings.resend_api_key
    from_email = settings.resend_from_email
    if not api_key or not api_key.strip() or not from_email or not from_email.strip():
        return failure_result(
            "send_email",
            "MISSING_CONFIG",
            "RESEND_API_KEY and RESEND_FROM_EMAIL must be configured.",
        )

    recipients = payload.to if isinstance(payload.to, list) else [payload.to]
    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [str(recipient) for recipient in recipients],
        "subject": payload.subject,
        "html" if payload.body_type == "html" else "text": payload.body,
    }

    resend.api_key = api_key
    try:
        if job_id is None:
            response = resend.Emails.send(params)
        else:
            response = resend.Emails.send(
                params,
                options={"idempotency_key": f"job-{job_id}"},
            )
    except ResendError:
        logger.warning(
            "Task send_email outcome=failure error_code=PROVIDER_ERROR"
        )
        return failure_result(
            "send_email",
            "PROVIDER_ERROR",
            "The email provider rejected or could not accept the request.",
            retryable=True,
        )

    message_id = getattr(response, "id", None)
    if message_id is None and isinstance(response, dict):
        message_id = response.get("id")
    if not message_id:
        raise RuntimeError("The email provider response did not contain a message ID")

    return HandlerResult(
        success=True,
        task_type="send_email",
        data={"provider_message_id": str(message_id)},
    )
