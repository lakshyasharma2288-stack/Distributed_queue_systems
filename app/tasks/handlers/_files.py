import re
import uuid
from contextlib import contextmanager
from collections.abc import Generator
from pathlib import Path
from typing import BinaryIO
from uuid import UUID


def safe_output_path(
    output_dir: Path,
    filename: str | None,
    *,
    default_stem: str,
    extension: str,
) -> Path:
    requested_name = filename or f"{default_stem}_{uuid.uuid4().hex}"
    if (
        Path(requested_name).name != requested_name
        or "/" in requested_name
        or "\\" in requested_name
        or requested_name in {".", ".."}
    ):
        raise ValueError("output_filename must be a filename, not a path")

    stem = Path(requested_name).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
    if not safe_stem:
        raise ValueError("output_filename must contain a safe filename character")
    return output_dir / f"{safe_stem}{extension}"


def job_output_directory(output_dir: Path, job_id: UUID) -> Path:
    job_dir = output_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    resolved_job_dir = job_dir.resolve(strict=True)
    try:
        resolved_job_dir.relative_to(output_dir.resolve(strict=True))
    except ValueError as exc:
        raise OSError(
            "Job output directory must remain inside output directory"
        ) from exc
    return resolved_job_dir


@contextmanager
def exclusive_output(path: Path) -> Generator[BinaryIO, None, None]:
    output_file = path.open("xb")
    try:
        yield output_file
    except BaseException:
        output_file.close()
        path.unlink(missing_ok=True)
        raise
    else:
        output_file.close()
