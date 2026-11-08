from collections.abc import Iterator

from app.db import database


class DummySession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_get_db_closes_session() -> None:
    dummy_session = DummySession()
    original_session_local = database.SessionLocal

    try:
        database.SessionLocal = lambda: dummy_session
        dependency: Iterator[DummySession] = database.get_db()

        yielded_session = next(dependency)
        assert yielded_session is dummy_session

        try:
            next(dependency)
        except StopIteration:
            pass

        assert dummy_session.closed is True
    finally:
        database.SessionLocal = original_session_local
