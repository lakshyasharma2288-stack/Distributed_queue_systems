import logging
from urllib.parse import unquote, urlsplit

import redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.queue.exceptions import QueueConfigurationError

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def masked_redis_error(exc: BaseException) -> str:
    cause = exc.__cause__ or exc
    message = str(cause)
    try:
        parsed_url = urlsplit(settings.redis_url)
    except ValueError:
        parsed_url = None

    secrets = [settings.redis_url]
    if parsed_url is not None and parsed_url.password:
        secrets.extend((parsed_url.password, unquote(parsed_url.password)))
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return f"{type(cause).__name__}: {message[:500]}"


def get_redis_client() -> redis.Redis:
    global _client
    if _client is not None:
        return _client

    try:
        parsed_url = urlsplit(settings.redis_url)
        if parsed_url.scheme not in {"redis", "rediss"} or not parsed_url.hostname:
            raise ValueError("Unsupported or incomplete Redis URL")
        _client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
    except (ValueError, RedisError) as exc:
        raise QueueConfigurationError(
            "REDIS_URL must be a valid redis:// or rediss:// URL."
        ) from exc

    return _client


def ping() -> bool:
    try:
        return bool(get_redis_client().ping())
    except Exception as exc:
        logger.warning("Redis ping failed error_type=%s", type(exc).__name__)
        return False


def close_redis_client() -> None:
    global _client
    client = _client
    _client = None
    if client is not None:
        client.close()
