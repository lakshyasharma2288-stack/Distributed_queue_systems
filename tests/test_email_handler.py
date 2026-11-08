from types import SimpleNamespace
from uuid import uuid4

import pytest
from resend.exceptions import ResendError

from app.core.config import settings
from app.tasks import dispatcher
from app.tasks.handlers import email
from app.tasks.schemas import SendEmailPayload


@pytest.fixture()
def configured_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "resend_api_key", "test-resend-secret")
    monkeypatch.setattr(settings, "resend_from_email", "Queue <sender@example.com>")


def test_email_validation_accepts_single_or_multiple_recipients() -> None:
    single = SendEmailPayload(
        to="person@example.com",
        subject="Hello",
        body="Message",
    )
    multiple = SendEmailPayload(
        to=["one@example.com", "two@example.com"],
        subject="Hello",
        body="Message",
    )

    assert str(single.to) == "person@example.com"
    assert len(multiple.to) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "not-an-email", "subject": "Hello", "body": "Message"},
        {"to": "person@example.com", "subject": "", "body": "Message"},
        {"to": "person@example.com", "subject": "Hello", "body": "  "},
    ],
)
def test_invalid_email_payload_returns_validation_error(
    payload: dict[str, object],
) -> None:
    result = dispatcher.dispatch("send_email", payload)

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"


def test_missing_api_key_returns_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "resend_from_email", "sender@example.com")
    payload = SendEmailPayload(
        to="person@example.com",
        subject="Hello",
        body="Message",
    )

    result = email.send_email(payload)

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "MISSING_CONFIG"


@pytest.mark.usefixtures("configured_email")
def test_email_success_reports_provider_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_send(params):
        captured.update(params)
        return SimpleNamespace(id="provider-message-123")

    monkeypatch.setattr(email.resend.Emails, "send", fake_send)
    payload = SendEmailPayload(
        to=["person@example.com", "other@example.com"],
        subject="Hello",
        body="<b>Message</b>",
        body_type="html",
    )

    result = email.send_email(payload)

    assert result.success is True
    assert result.data == {"provider_message_id": "provider-message-123"}
    assert captured["to"] == ["person@example.com", "other@example.com"]
    assert captured["html"] == "<b>Message</b>"
    assert "text" not in captured


@pytest.mark.usefixtures("configured_email")
def test_job_dispatch_uses_stable_provider_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: dict[str, object] = {}

    def fake_send(params, options=None):
        if options is not None:
            captured_options.update(options)
        return SimpleNamespace(id="provider-message-456")

    monkeypatch.setattr(email.resend.Emails, "send", fake_send)
    job_id = uuid4()
    result = dispatcher.dispatch(
        "send_email",
        {
            "to": "person@example.com",
            "subject": "Hello",
            "body": "Message",
        },
        job_id=job_id,
    )

    assert result.success is True
    assert captured_options == {"idempotency_key": f"job-{job_id}"}


@pytest.mark.usefixtures("configured_email")
def test_provider_failure_does_not_expose_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = settings.resend_api_key

    def fail_send(params):
        assert api_key not in repr(params)
        raise ResendError(
            "401",
            "authentication_error",
            f"request failed with credential {api_key}",
            "check credentials",
        )

    monkeypatch.setattr(email.resend.Emails, "send", fail_send)
    payload = SendEmailPayload(
        to="person@example.com",
        subject="Hello",
        body="Message",
    )

    result = email.send_email(payload)

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "PROVIDER_ERROR"
    assert api_key not in result.error.message
