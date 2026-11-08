import os
from pathlib import Path

from dotenv import dotenv_values
import pytest


_configured_database_url = os.environ.get("DATABASE_URL")
if not _configured_database_url:
    _configured_database_url = dotenv_values(
        Path(__file__).resolve().parents[1] / ".env"
    ).get("DATABASE_URL")

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"


@pytest.fixture(scope="session")
def configured_database_url() -> str:
    return _configured_database_url or "sqlite+pysqlite:///:memory:"


@pytest.fixture()
def image_db(monkeypatch: pytest.MonkeyPatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.database import get_db
    from app.main import app
    from app.models.image import Image

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Image.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.tasks.handlers.image.SessionLocal", factory)
    yield factory
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()
