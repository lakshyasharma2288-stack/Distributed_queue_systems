from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str
    debug: bool = False
    redis_url: str = "redis://localhost:6379/0"
    redis_connect_timeout_seconds: float = 1.0
    redis_socket_timeout_seconds: float = 1.0
    queue_key: str = "djq:queue:default"
    enable_queue_inspection: bool = True
    outbox_batch_size: int = 100
    outbox_poll_interval_seconds: float = 1.0
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    worker_heartbeat_timeout_seconds: float = Field(default=30.0, gt=0)
    worker_lease_duration_seconds: float = Field(default=60.0, gt=0)
    worker_lease_renewal_interval_seconds: float = Field(default=10.0, gt=0)
    default_max_attempts: int = Field(default=3, ge=1)
    retry_base_delay_seconds: float = Field(default=5.0, gt=0)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0)
    retry_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    retry_scheduler_batch_size: int = Field(default=100, ge=1)
    max_image_upload_size_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    resend_api_key: str | None = None
    resend_from_email: str | None = None
    image_input_dir: str | None = None
    image_output_dir: str | None = None
    pdf_output_dir: str | None = None
    max_image_pixels: int = 40_000_000

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
