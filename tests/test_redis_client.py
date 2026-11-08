import fakeredis
import pytest
from redis.exceptions import ConnectionError

from app.core.config import settings
from app.queue import redis_client
from app.queue.exceptions import QueueConfigurationError


@pytest.fixture(autouse=True)
def reset_redis_client():
    redis_client.close_redis_client()
    yield
    redis_client.close_redis_client()


def test_get_redis_client_is_lazy_shared_and_configures_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/2")
    monkeypatch.setattr(settings, "redis_connect_timeout_seconds", 2.5)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", 3.5)

    first_client = redis_client.get_redis_client()
    second_client = redis_client.get_redis_client()

    assert first_client is second_client
    assert first_client.connection_pool.connection_kwargs["decode_responses"] is True
    assert first_client.connection_pool.connection_kwargs["socket_connect_timeout"] == 2.5
    assert first_client.connection_pool.connection_kwargs["socket_timeout"] == 3.5


def test_ping_returns_true_with_responsive_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_client, "_client", fakeredis.FakeRedis(decode_responses=True))

    assert redis_client.ping() is True


def test_ping_returns_false_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableClient:
        def ping(self) -> bool:
            raise ConnectionError("connection refused")

    monkeypatch.setattr(redis_client, "_client", UnavailableClient())

    assert redis_client.ping() is False


def test_ping_returns_false_for_an_unavailable_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "redis_connect_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", 0.1)

    assert redis_client.ping() is False


def test_invalid_redis_url_raises_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_url = "redis://user:secret@/0"
    monkeypatch.setattr(settings, "redis_url", invalid_url)

    with pytest.raises(QueueConfigurationError) as exc_info:
        redis_client.get_redis_client()

    assert "secret" not in str(exc_info.value)
    assert invalid_url not in str(exc_info.value)


def test_close_clears_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_client", client)

    redis_client.close_redis_client()

    assert redis_client._client is None
