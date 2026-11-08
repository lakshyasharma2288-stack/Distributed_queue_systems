from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool


class HandlerResult(BaseModel):
    success: bool
    task_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None


def failure_result(
    task_type: str,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> HandlerResult:
    return HandlerResult(
        success=False,
        task_type=task_type,
        error=ErrorInfo(code=code, message=message, retryable=retryable),
    )


class SendEmailPayload(BaseModel):
    to: EmailStr | list[EmailStr]
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=50_000)
    body_type: Literal["text", "html"] = "text"

    @field_validator("to")
    @classmethod
    def validate_recipients(cls, value: EmailStr | list[EmailStr]) -> EmailStr | list[EmailStr]:
        if isinstance(value, list) and not 1 <= len(value) <= 50:
            raise ValueError("provide between 1 and 50 email recipients")
        return value

    @field_validator("subject", "body")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ResizeImagePayload(BaseModel):
    input_path: str | None = Field(default=None, min_length=1)
    image_id: UUID | None = None
    width: int | None = Field(default=None, gt=0, le=10_000, strict=True)
    height: int | None = Field(default=None, gt=0, le=10_000, strict=True)
    keep_aspect_ratio: bool = True
    allow_upscale: bool = False
    output_filename: str | None = Field(default=None, min_length=1, max_length=255)
    output_format: str | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "ResizeImagePayload":
        if (self.input_path is None) == (self.image_id is None):
            raise ValueError("provide exactly one of input_path or image_id")
        if self.width is None and self.height is None:
            raise ValueError("at least one of width or height is required")
        if not self.keep_aspect_ratio and (self.width is None or self.height is None):
            raise ValueError(
                "width and height are both required when keep_aspect_ratio is false"
            )
        return self


class PDFSection(BaseModel):
    heading: str | None = Field(default=None, max_length=200)
    paragraphs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("heading")
    @classmethod
    def validate_heading(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("heading must not be blank")
        return value

    @field_validator("paragraphs")
    @classmethod
    def validate_paragraphs(cls, value: list[str]) -> list[str]:
        if any(not paragraph.strip() for paragraph in value):
            raise ValueError("paragraphs must not be blank")
        if any(len(paragraph) > 10_000 for paragraph in value):
            raise ValueError("each paragraph must be at most 10000 characters")
        return value

    @model_validator(mode="after")
    def require_content(self) -> "PDFSection":
        if self.heading is None and not self.paragraphs:
            raise ValueError("a section must have a heading or at least one paragraph")
        return self


class GeneratePDFPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    sections: list[PDFSection] = Field(min_length=1, max_length=50)
    output_filename: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def limit_total_content(self) -> "GeneratePDFPayload":
        content_size = len(self.title)
        for section in self.sections:
            content_size += len(section.heading or "")
            content_size += sum(len(paragraph) for paragraph in section.paragraphs)
        if content_size > 100_000:
            raise ValueError("total PDF text must be at most 100000 characters")
        return self
