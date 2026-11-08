import os
from typing import NoReturn
from urllib.parse import urlsplit

import pytest
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class DatabaseSafetyError(ValueError):
    pass


def _database_target(url: URL) -> tuple[str, int | None, str | None]:
    host = (url.host or "").lower().rstrip(".")
    if host in {"localhost", "127.0.0.1"}:
        host = "loopback"

    port = url.port
    if port is None and url.get_backend_name() == "postgresql":
        port = 5432
    return host, port, url.database


def validate_test_database_url(
    test_database_url: str | None,
    configured_database_url: str | None,
) -> URL:
    if not test_database_url:
        raise DatabaseSafetyError("TEST_DATABASE_URL is required")
    if not configured_database_url:
        raise DatabaseSafetyError(
            "Cannot verify TEST_DATABASE_URL against DATABASE_URL"
        )

    try:
        test_url = make_url(test_database_url)
        configured_url = make_url(configured_database_url)
        test_target = _database_target(test_url)
        configured_target = _database_target(configured_url)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise DatabaseSafetyError("A database URL is invalid") from exc

    if test_url.get_backend_name() != "postgresql":
        raise DatabaseSafetyError("TEST_DATABASE_URL must use PostgreSQL")
    if test_url.database != "distributed_queue_test":
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL must use the dedicated distributed_queue_test database"
        )
    if test_target == configured_target:
        raise DatabaseSafetyError(
            "TEST_DATABASE_URL must not point to the configured DATABASE_URL database"
        )
    return test_url


def validate_test_redis_url(
    test_redis_url: str | None,
    configured_redis_url: str | None,
) -> None:
    if not test_redis_url:
        raise DatabaseSafetyError("REDIS_TEST_URL is required")
    if not configured_redis_url:
        raise DatabaseSafetyError(
            "Cannot verify REDIS_TEST_URL against REDIS_URL"
        )

    try:
        test = urlsplit(test_redis_url)
        configured = urlsplit(configured_redis_url)
        if test.scheme not in {"redis", "rediss"} or configured.scheme not in {
            "redis",
            "rediss",
        }:
            raise DatabaseSafetyError("Redis URLs must use redis or rediss")
        test_host = (test.hostname or "").lower().rstrip(".")
        configured_host = (configured.hostname or "").lower().rstrip(".")
        if not test_host or not configured_host:
            raise DatabaseSafetyError("Redis URLs must include a host")
        if test_host in {"localhost", "127.0.0.1"}:
            test_host = "loopback"
        if configured_host in {"localhost", "127.0.0.1"}:
            configured_host = "loopback"
        test_port = test.port or (6380 if test.scheme == "rediss" else 6379)
        configured_port = configured.port or (
            6380 if configured.scheme == "rediss" else 6379
        )
        test_database = test.path.strip("/") or "0"
        configured_database = configured.path.strip("/") or "0"
    except DatabaseSafetyError:
        raise
    except ValueError as exc:
        raise DatabaseSafetyError("A Redis URL is invalid") from exc

    if (test_host, test_port, test_database) == (
        configured_host,
        configured_port,
        configured_database,
    ):
        raise DatabaseSafetyError(
            "REDIS_TEST_URL must not point to the configured REDIS_URL database"
        )


def unavailable_integration(reason: str) -> NoReturn:
    if os.environ.get("REQUIRE_INTEGRATION") == "1":
        pytest.fail(reason)
    pytest.skip(reason)
