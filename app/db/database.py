from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine_kwargs: dict[str, object] = {"pool_pre_ping": True}
database_url = make_url(settings.database_url)

if database_url.get_backend_name().startswith("postgresql"):
    engine_kwargs["connect_args"] = {"connect_timeout": 3}

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
