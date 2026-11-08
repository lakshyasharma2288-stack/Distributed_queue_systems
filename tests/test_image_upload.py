from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.core.config import settings
from app.main import app
from app.tasks.handlers.image import _resolve_input_path


@pytest.fixture()
def upload_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_db,
) -> Iterator[tuple[TestClient, Path]]:
    input_directory = tmp_path / "image-input"
    monkeypatch.setattr(settings, "image_input_dir", str(input_directory))
    monkeypatch.setattr(settings, "max_image_upload_size_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "max_image_pixels", 40_000_000)

    with TestClient(app) as client:
        yield client, input_directory


def image_bytes(
    image_format: str = "PNG",
    size: tuple[int, int] = (12, 8),
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color="blue").save(output, format=image_format)
    return output.getvalue()


@pytest.mark.parametrize(
    ("image_format", "original_filename", "expected_extension"),
    [
        ("JPEG", "client-name.png", ".jpg"),
        ("PNG", "client-name.jpg", ".png"),
        ("WEBP", "client-name.gif", ".webp"),
    ],
)
def test_upload_accepts_supported_image_formats_and_uses_detected_extension(
    upload_client: tuple[TestClient, Path],
    image_format: str,
    original_filename: str,
    expected_extension: str,
) -> None:
    client, input_directory = upload_client
    content = image_bytes(image_format)

    response = client.post(
        "/uploads/images",
        files={"file": (original_filename, content, "image/unknown")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["format"] == image_format
    assert body["size_bytes"] == len(content)
    assert body["width"] == 12
    assert body["height"] == 8
    assert body["filename"].endswith(expected_extension)
    assert body["input_path"] == body["filename"]
    stored_path = input_directory / body["filename"]
    assert stored_path.is_file()
    assert stored_path.resolve().parent == input_directory.resolve()


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        ("notes.txt", b"plain text", "text/plain"),
        ("vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    ],
)
def test_upload_rejects_unsupported_content(
    upload_client: tuple[TestClient, Path],
    filename: str,
    content: bytes,
    content_type: str,
) -> None:
    client, input_directory = upload_client

    response = client.post(
        "/uploads/images",
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 415
    assert list(input_directory.iterdir()) == []


def test_upload_rejects_text_renamed_as_png(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client

    response = client.post(
        "/uploads/images",
        files={"file": ("photo.png", b"not a PNG image", "image/png")},
    )

    assert response.status_code == 415
    assert list(input_directory.iterdir()) == []


def test_upload_rejects_corrupted_image(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client
    corrupted_png = image_bytes()[:-8]

    response = client.post(
        "/uploads/images",
        files={"file": ("corrupt.png", corrupted_png, "image/png")},
    )

    assert response.status_code in {415, 422}
    assert list(input_directory.iterdir()) == []


def test_upload_rejects_empty_file(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client

    response = client.post(
        "/uploads/images",
        files={"file": ("empty.png", b"", "image/png")},
    )

    assert response.status_code == 422
    assert list(input_directory.iterdir()) == []


def test_upload_enforces_size_limit_while_copying(
    upload_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, input_directory = upload_client
    monkeypatch.setattr(settings, "max_image_upload_size_bytes", 32)

    response = client.post(
        "/uploads/images",
        files={"file": ("large.png", image_bytes(), "image/png")},
    )

    assert response.status_code == 413
    assert list(input_directory.iterdir()) == []


def test_upload_enforces_pixel_limit(
    upload_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, input_directory = upload_client
    monkeypatch.setattr(settings, "max_image_pixels", 100)

    response = client.post(
        "/uploads/images",
        files={"file": ("large.png", image_bytes(size=(11, 11)), "image/png")},
    )

    assert response.status_code == 413
    assert list(input_directory.iterdir()) == []


def test_same_original_filename_creates_distinct_files(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client
    content = image_bytes()

    first = client.post(
        "/uploads/images",
        files={"file": ("same-name.png", content, "image/png")},
    )
    second = client.post(
        "/uploads/images",
        files={"file": ("same-name.png", content, "image/png")},
    )

    assert first.status_code == second.status_code == 201
    first_name = first.json()["filename"]
    second_name = second.json()["filename"]
    assert first_name != second_name
    assert (input_directory / first_name).is_file()
    assert (input_directory / second_name).is_file()


def test_original_path_traversal_filename_is_ignored(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client

    response = client.post(
        "/uploads/images",
        files={"file": ("../../x.png", image_bytes(), "image/png")},
    )

    assert response.status_code == 201
    filename = response.json()["filename"]
    stored_path = input_directory / filename
    assert stored_path.is_file()
    assert stored_path.resolve().parent == input_directory.resolve()
    assert not (input_directory.parent / "x.png").exists()


def test_unavailable_input_directory_returns_generic_server_error(
    upload_client: tuple[TestClient, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _input_directory = upload_client
    unavailable_path = tmp_path / "not-a-directory"
    unavailable_path.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(settings, "image_input_dir", str(unavailable_path))

    response = client.post(
        "/uploads/images",
        files={"file": ("image.png", image_bytes(), "image/png")},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Image storage is unavailable."}
    assert str(tmp_path) not in response.text


def test_uploaded_input_path_resolves_with_resize_handler_logic(
    upload_client: tuple[TestClient, Path],
) -> None:
    client, input_directory = upload_client

    response = client.post(
        "/uploads/images",
        files={"file": ("image.png", image_bytes(), "image/png")},
    )

    assert response.status_code == 201
    input_path = response.json()["input_path"]
    resolved_path = _resolve_input_path(input_path, input_directory.resolve())
    assert resolved_path.is_file()
    assert resolved_path.parent == input_directory.resolve()
