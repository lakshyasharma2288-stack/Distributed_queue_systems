from pathlib import Path
from uuid import uuid4

from PIL import Image
import pytest

from app.core.config import settings
from app.tasks.dispatcher import dispatch
from app.tasks.handlers.image import resize_image
from app.tasks.schemas import ResizeImagePayload


@pytest.fixture()
def image_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    monkeypatch.setattr(settings, "image_input_dir", str(input_dir))
    monkeypatch.setattr(settings, "image_output_dir", str(output_dir))
    monkeypatch.setattr(settings, "max_image_pixels", 1_000_000)
    return input_dir, output_dir


def test_resize_preserves_aspect_ratio_and_original(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, output_dir = image_directories
    input_path = input_dir / "source.png"
    Image.new("RGB", (400, 200), color="red").save(input_path)
    original_bytes = input_path.read_bytes()

    result = resize_image(
        ResizeImagePayload(input_path="source.png", width=100, height=100)
    )

    assert result.success is True
    assert result.data["original_dimensions"] == {"width": 400, "height": 200}
    assert result.data["new_dimensions"] == {"width": 100, "height": 50}
    assert input_path.read_bytes() == original_bytes
    output_path = Path(str(result.data["output_path"]))
    assert output_path.is_file()
    with Image.open(output_path) as resized:
        assert resized.size == (100, 50)
        assert resized.format == "PNG"
    assert output_path.parent == output_dir.resolve()


def test_image_job_retry_reuses_existing_output(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, output_dir = image_directories
    Image.new("RGB", (40, 20), color="blue").save(input_dir / "source.png")
    job_id = uuid4()
    payload = ResizeImagePayload(
        input_path="source.png",
        width=10,
    )

    first = resize_image(payload, job_id=job_id)
    replay = resize_image(payload, job_id=job_id)

    assert first.success is True
    assert replay.success is True
    assert first.data == replay.data
    assert Path(str(first.data["output_path"])).parent == (
        output_dir.resolve() / str(job_id)
    )


def test_single_dimension_can_upscale_when_allowed(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, _ = image_directories
    Image.new("RGB", (20, 10)).save(input_dir / "source.png")

    result = resize_image(
        ResizeImagePayload(
            input_path="source.png",
            width=40,
            allow_upscale=True,
        )
    )

    assert result.success is True
    assert result.data["new_dimensions"] == {"width": 40, "height": 20}


def test_exif_orientation_is_applied(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, _ = image_directories
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (40, 20)).save(
        input_dir / "oriented.jpg",
        format="JPEG",
        exif=exif,
    )

    result = resize_image(
        ResizeImagePayload(input_path="oriented.jpg", width=10)
    )

    assert result.success is True
    assert result.data["original_dimensions"] == {"width": 20, "height": 40}
    assert result.data["new_dimensions"] == {"width": 10, "height": 20}


def test_pixel_limit_is_enforced(
    image_directories: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, _ = image_directories
    Image.new("RGB", (20, 20)).save(input_dir / "large.png")
    monkeypatch.setattr(settings, "max_image_pixels", 100)

    result = resize_image(
        ResizeImagePayload(input_path="large.png", width=10)
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"


@pytest.mark.usefixtures("image_directories")
def test_invalid_dimensions_return_validation_error() -> None:
    result = dispatch(
        "resize_image",
        {"input_path": "source.png", "width": 0},
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"


@pytest.mark.usefixtures("image_directories")
def test_missing_image_returns_file_not_found() -> None:
    result = resize_image(ResizeImagePayload(input_path="missing.png", width=10))

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "FILE_NOT_FOUND"


def test_corrupt_image_returns_io_error(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, _ = image_directories
    (input_dir / "corrupt.png").write_bytes(b"not an image")

    result = resize_image(
        ResizeImagePayload(input_path="corrupt.png", width=10)
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "IO_ERROR"


def test_unsupported_image_format_returns_unsupported_format(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, _ = image_directories
    Image.new("RGB", (20, 20)).save(input_dir / "source.bmp")

    result = resize_image(ResizeImagePayload(input_path="source.bmp", width=10))

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "UNSUPPORTED_FORMAT"


def test_input_path_traversal_is_rejected(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, _ = image_directories
    outside_path = input_dir.parent / "outside.png"
    Image.new("RGB", (20, 20)).save(outside_path)

    result = resize_image(
        ResizeImagePayload(input_path="../outside.png", width=10)
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "VALIDATION_ERROR"
    assert outside_path.is_file()


def test_existing_output_is_not_overwritten(
    image_directories: tuple[Path, Path],
) -> None:
    input_dir, output_dir = image_directories
    Image.new("RGB", (20, 20), color="blue").save(input_dir / "source.png")
    output_dir.mkdir()
    existing = output_dir / "chosen.png"
    existing.write_bytes(b"keep this file")

    result = resize_image(
        ResizeImagePayload(
            input_path="source.png",
            width=10,
            output_filename="chosen.png",
        )
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.code == "IO_ERROR"
    assert existing.read_bytes() == b"keep this file"
