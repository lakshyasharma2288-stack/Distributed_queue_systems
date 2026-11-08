import random


def compute_retry_delay_seconds(
    attempt: int,
    *,
    base_delay_seconds: float,
    max_delay_seconds: float,
    jitter_ratio: float,
    random_value: float | None = None,
) -> float:
    if attempt < 1:
        raise ValueError("attempt must be at least one")
    if base_delay_seconds <= 0 or max_delay_seconds <= 0:
        raise ValueError("retry delays must be positive")
    if not 0 <= jitter_ratio <= 1:
        raise ValueError("jitter_ratio must be between zero and one")
    sample = random.random() if random_value is None else random_value
    if not 0 <= sample <= 1:
        raise ValueError("random_value must be between zero and one")

    delay = min(base_delay_seconds, max_delay_seconds)
    for _ in range(attempt - 1):
        delay = min(delay * 2, max_delay_seconds)
        if delay >= max_delay_seconds:
            break
    lower_bound = delay * (1 - jitter_ratio)
    upper_bound = min(max_delay_seconds, delay * (1 + jitter_ratio))
    return lower_bound + (upper_bound - lower_bound) * sample
