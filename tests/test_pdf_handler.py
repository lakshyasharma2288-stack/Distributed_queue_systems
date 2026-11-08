from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import settings
from app.tasks.dispatcher import dispatch
from app.tasks.handlers.pdf import generate_pdf
from app.tasks.schemas import GeneratePDFPayload


@pytest.fixture()
def pdf_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    output_dir = tmp_path / "generated-pdfs"
    monkeypatch.setattr(settings, "pdf_output_dir", str(output_dir))
    return output_dir


def test_pdf_is_created_with_valid_signature(
    pdf_output_dir: Path,
) -> None:
    result = generate_pdf(
        GeneratePDFPayload(
            title="Monthly report",
            sections=[
                {"heading": "Summary", "paragraphs": ["Report content."]},
            ],
        )
    )

    assert result.success is True
    output_path = Path(str(result.data["output_path"]))
    pdf_bytes = output_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 0
    assert result.data["file_size"] == len(pdf_bytes)
    assert output_path.parent == pdf_output_dir.resolve()


def test_pdf_output_directory_is_created(pdf_output_dir: Path) -> None:
    assert not pdf_output_dir.exists()

    result = generate_pdf(
        GeneratePDFPayload(
            title="Report",
            sections=[{"paragraphs": ["Created in a new directory."]}],
        )
    )

    assert result.success is True
    assert pdf_output_dir.is_dir()


def test_pdf_job_retry_reuses_existing_output(pdf_output_dir: Path) -> None:
    job_id = uuid4()
    payload = GeneratePDFPayload(
        title="Retry-safe report",
        sections=[{"paragraphs": ["Generated once."]}],
    )

    first = generate_pdf(payload, job_id=job_id)
    replay = generate_pdf(payload, job_id=job_id)

    assert first.success is True
    assert replay.success is True
    assert first.data == replay.data
    assert Path(str(first.data["output_path"])).parent == (
        pdf_output_dir.resolve() / str(job_id)
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "sections": [{"paragraphs": ["Body"]}]},
        {"title": "Report", "sections": []},
        {"title": "Report", "sections": [{}]},
    ],
)
def test_invalid_pdf_payload_returns_validation_error(
    payload: dict[str, object],
) -> None:
    result = dispatch("generate_pdf", payload)

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"


@pytest.mark.usefixtures("pdf_output_dir")
def test_pdf_escapes_special_characters() -> None:
    result = generate_pdf(
        GeneratePDFPayload(
            title="Report <&>",
            sections=[
                {
                    "heading": "Heading <&>",
                    "paragraphs": ["Text with <tag> & special characters."],
                },
            ],
        )
    )

    assert result.success is True
    assert Path(str(result.data["output_path"])).read_bytes().startswith(b"%PDF")


@pytest.mark.usefixtures("pdf_output_dir")
def test_pdf_rejects_output_path_traversal() -> None:
    result = generate_pdf(
        GeneratePDFPayload(
            title="Report",
            sections=[{"paragraphs": ["Body"]}],
            output_filename="../outside.pdf",
        )
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
