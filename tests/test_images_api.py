from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.main import app
from app.models.image import Image as ImageRecord
from app.tasks.handlers.image import resize_image
from app.tasks.schemas import ResizeImagePayload


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image_db):
    input_dir = tmp_path / "input"
    monkeypatch.setattr(settings, "image_input_dir", str(input_dir))
    monkeypatch.setattr(settings, "image_output_dir", str(tmp_path / "output"))
    monkeypatch.setattr(settings, "max_image_pixels", 1_000_000)
    with TestClient(app) as client:
        yield client, input_dir, image_db


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (40, 20), color="green").save(output, format="PNG")
    return output.getvalue()


def _upload(client: TestClient) -> dict:
    response = client.post("/uploads/images", files={"file": ("a.png", _png())})
    assert response.status_code == 201
    return response.json()


def test_upload_registers_row_and_returns_legacy_keys(api) -> None:
    client, input_dir, factory = api
    body = _upload(client)
    assert body["input_path"] == body["filename"]
    with factory() as session:
        row = session.get(ImageRecord, __import__("uuid").UUID(body["id"]))
        assert row is not None
        assert row.stored_filename == body["filename"]
        assert (row.width, row.height) == (40, 20)


def test_upload_removes_file_when_db_insert_fails(api, monkeypatch) -> None:
    client, input_dir, factory = api
    from sqlalchemy.orm import Session

    def boom(self):
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("fail")

    monkeypatch.setattr(Session, "commit", boom)
    response = client.post("/uploads/images", files={"file": ("a.png", _png())})
    assert response.status_code == 500
    assert [p for p in input_dir.iterdir()] == []


def test_list_get_and_serve_image(api) -> None:
    client, _, _ = api
    body = _upload(client)
    assert [i["id"] for i in client.get("/images").json()] == [body["id"]]
    assert client.get(f"/images/{body['id']}").json()["width"] == 40
    served = client.get(f"/images/{body['id']}/file")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"


def test_unknown_and_missing_file_return_404(api) -> None:
    client, input_dir, _ = api
    assert client.get(f"/images/{uuid4()}/file").status_code == 404
    body = _upload(client)
    (input_dir / body["filename"]).unlink()
    assert client.get(f"/images/{body['id']}/file").status_code == 404


def test_payload_requires_exactly_one_input() -> None:
    with pytest.raises(ValidationError):
        ResizeImagePayload(width=10)
    with pytest.raises(ValidationError):
        ResizeImagePayload(input_path="a.png", image_id=uuid4(), width=10)


def test_resize_accepts_image_id(api) -> None:
    client, _, _ = api
    body = _upload(client)
    result = resize_image(ResizeImagePayload(image_id=body["id"], width=20))
    assert result.success is True
    assert result.data["new_dimensions"] == {"width": 20, "height": 10}


def test_resize_unknown_image_id_is_file_not_found(api) -> None:
    api[1].mkdir(parents=True, exist_ok=True)
    result = resize_image(ResizeImagePayload(image_id=uuid4(), width=20))
    assert result.success is False
    assert result.error.code == "FILE_NOT_FOUND"
