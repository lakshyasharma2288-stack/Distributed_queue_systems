from typing import Any

from pydantic import BaseModel

from app.tasks import dispatcher
from app.tasks.schemas import HandlerResult, SendEmailPayload


def test_registry_contains_all_supported_tasks() -> None:
    assert set(dispatcher.TASK_REGISTRY) == {
        "send_email",
        "resize_image",
        "generate_pdf",
    }


def test_dispatch_validates_payload_and_calls_registered_handler(monkeypatch) -> None:
    calls: list[SendEmailPayload] = []

    def fake_handler(payload: SendEmailPayload) -> HandlerResult:
        calls.append(payload)
        return HandlerResult(success=True, task_type="send_email", data={"ok": True})

    monkeypatch.setitem(
        dispatcher.TASK_REGISTRY,
        "send_email",
        (SendEmailPayload, fake_handler),
    )

    result = dispatcher.dispatch(
        "send_email",
        {"to": "person@example.com", "subject": "Hello", "body": "Message"},
    )

    assert result.success is True
    assert len(calls) == 1
    assert calls[0].subject == "Hello"


def test_unknown_task_returns_unsupported_task() -> None:
    result = dispatcher.dispatch("unknown", {})

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "UNSUPPORTED_TASK"


def test_validation_error_is_returned_without_echoing_input() -> None:
    secret_body = "private content"
    result = dispatcher.dispatch(
        "send_email",
        {"to": "not-an-email", "subject": "", "body": secret_body},
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    assert secret_body not in result.error.message


def test_unexpected_handler_exception_returns_internal_error(monkeypatch) -> None:
    class EmptyPayload(BaseModel):
        pass

    def broken_handler(payload: Any) -> HandlerResult:
        assert isinstance(payload, EmptyPayload)
        raise RuntimeError("unexpected failure")

    monkeypatch.setitem(
        dispatcher.TASK_REGISTRY,
        "send_email",
        (EmptyPayload, broken_handler),
    )

    result = dispatcher.dispatch("send_email", {})

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "INTERNAL_ERROR"
