import pytest

from week4_safety import (
    DatabaseSafetyError,
    unavailable_integration,
    validate_test_database_url,
    validate_test_redis_url,
)


def test_database_safety_rejects_same_database_ignoring_driver_and_query() -> None:
    test_url = "postgresql+psycopg://localhost:5432/distributed_queue_test?sslmode=disable"
    configured_url = "postgresql://localhost:5432/distributed_queue_test?application_name=api"

    with pytest.raises(DatabaseSafetyError, match="must not point"):
        validate_test_database_url(test_url, configured_url)


def test_database_safety_normalizes_localhost_aliases() -> None:
    test_url = "postgresql://localhost:5432/distributed_queue_test"
    configured_url = "postgresql+psycopg://127.0.0.1/distributed_queue_test?sslmode=disable"

    with pytest.raises(DatabaseSafetyError, match="must not point"):
        validate_test_database_url(test_url, configured_url)


def test_database_safety_distinguishes_database_ports() -> None:
    test_url = "postgresql://localhost:5433/distributed_queue_test"
    configured_url = "postgresql://localhost:5432/distributed_queue_test"

    assert validate_test_database_url(test_url, configured_url).port == 5433


def test_database_safety_requires_exact_test_database_name() -> None:
    test_url = "postgresql://localhost/distributed_queue_db"
    configured_url = "postgresql://localhost/distributed_queue_test"

    with pytest.raises(DatabaseSafetyError, match="dedicated"):
        validate_test_database_url(test_url, configured_url)


def test_database_safety_rejects_missing_test_database_url() -> None:
    with pytest.raises(DatabaseSafetyError, match="TEST_DATABASE_URL is required"):
        validate_test_database_url(
            None,
            "postgresql://localhost/distributed_queue_db",
        )


def test_redis_safety_rejects_same_database_through_localhost_alias() -> None:
    test_url = "redis://localhost:6379/15"
    configured_url = "redis://127.0.0.1/15?application_name=api"

    with pytest.raises(DatabaseSafetyError, match="must not point"):
        validate_test_redis_url(test_url, configured_url)


def test_redis_safety_allows_dedicated_test_database() -> None:
    validate_test_redis_url(
        "redis://localhost:6379/15",
        "redis://localhost:6379/0",
    )


def test_required_integration_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUIRE_INTEGRATION", "1")

    with pytest.raises(pytest.fail.Exception, match="service unavailable"):
        unavailable_integration("service unavailable")


def test_optional_integration_still_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_INTEGRATION", raising=False)

    with pytest.raises(pytest.skip.Exception, match="service unavailable"):
        unavailable_integration("service unavailable")
