import logging
from html import escape
from pathlib import Path
from uuid import UUID

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.core.config import settings
from app.tasks.handlers._files import (
    exclusive_output,
    job_output_directory,
    safe_output_path,
)
from app.tasks.schemas import GeneratePDFPayload, HandlerResult, failure_result

logger = logging.getLogger(__name__)


def generate_pdf(
    payload: GeneratePDFPayload,
    *,
    job_id: UUID | None = None,
) -> HandlerResult:
    if not settings.pdf_output_dir or not settings.pdf_output_dir.strip():
        return failure_result(
            "generate_pdf",
            "MISSING_CONFIG",
            "PDF_OUTPUT_DIR must be configured.",
        )

    output_dir = Path(settings.pdf_output_dir).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir = output_dir.resolve(strict=True)
        if job_id is not None:
            output_dir = job_output_directory(output_dir, job_id)
        try:
            output_path = safe_output_path(
                output_dir,
                payload.output_filename
                or (f"generated_{job_id.hex}.pdf" if job_id is not None else None),
                default_stem="generated",
                extension=".pdf",
            )
        except ValueError:
            return failure_result(
                "generate_pdf",
                "VALIDATION_ERROR",
                "output_filename must be a simple filename without path components.",
            )

        if job_id is not None and output_path.is_symlink():
            return failure_result(
                "generate_pdf",
                "IO_ERROR",
                "The job output path must not be a symbolic link.",
            )
        if job_id is not None and output_path.is_file():
            file_size = output_path.stat().st_size
            with output_path.open("rb") as existing_file:
                header = existing_file.read(5)
                existing_file.seek(max(0, file_size - 1024))
                trailer = existing_file.read()
            valid_pdf = (
                header == b"%PDF-"
                and file_size > 5
                and b"%%EOF" in trailer
            )
            if valid_pdf:
                return HandlerResult(
                    success=True,
                    task_type="generate_pdf",
                    data={
                        "output_path": str(output_path),
                        "file_size": file_size,
                    },
                )
            return failure_result(
                "generate_pdf",
                "IO_ERROR",
                "The job output exists but is not a valid PDF.",
            )

        styles = getSampleStyleSheet()
        story = [Paragraph(escape(payload.title), styles["Title"]), Spacer(1, 0.2 * inch)]
        for section in payload.sections:
            if section.heading:
                story.append(Paragraph(escape(section.heading), styles["Heading2"]))
            for paragraph in section.paragraphs:
                story.append(Paragraph(escape(paragraph), styles["BodyText"]))
                story.append(Spacer(1, 0.1 * inch))

        with exclusive_output(output_path) as output_file:
            document = SimpleDocTemplate(output_file)
            document.build(story)
        file_size = output_path.stat().st_size
    except FileExistsError:
        return failure_result(
            "generate_pdf",
            "IO_ERROR",
            "The output file already exists; choose a different output_filename.",
        )
    except OSError:
        logger.warning("Task generate_pdf outcome=failure error_code=IO_ERROR")
        return failure_result(
            "generate_pdf",
            "IO_ERROR",
            "The PDF could not be written.",
            retryable=True,
        )

    return HandlerResult(
        success=True,
        task_type="generate_pdf",
        data={
            "output_path": str(output_path),
            "file_size": file_size,
        },
    )
