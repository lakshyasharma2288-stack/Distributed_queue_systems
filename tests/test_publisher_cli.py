import argparse
import signal
import sys

import pytest

from app.publisher import run_publisher


def test_publisher_cli_once_runs_one_batch_and_closes_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_calls: list[tuple[int, object]] = []
    close_calls: list[bool] = []
    monkeypatch.setattr(
        run_publisher,
        "_parse_args",
        lambda: argparse.Namespace(loop=False, batch_size=4, interval=0.1),
    )
    monkeypatch.setattr(run_publisher, "QueueManager", lambda: object())
    monkeypatch.setattr(
        run_publisher,
        "_run_batch",
        lambda _queue, batch_size: batch_calls.append((batch_size, None)),
    )
    monkeypatch.setattr(
        run_publisher,
        "close_redis_client",
        lambda: close_calls.append(True),
    )

    run_publisher.main()

    assert batch_calls == [(4, None)]
    assert close_calls == [True]


def test_publisher_loop_stops_cleanly_after_stop_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_handler = object()
    signal_handlers: list[object] = []
    batch_calls: list[int] = []
    close_calls: list[bool] = []
    monkeypatch.setattr(
        run_publisher,
        "_parse_args",
        lambda: argparse.Namespace(loop=True, batch_size=7, interval=0.01),
    )
    monkeypatch.setattr(run_publisher, "QueueManager", lambda: object())
    monkeypatch.setattr(
        run_publisher.signal,
        "getsignal",
        lambda _signal: original_handler,
    )
    monkeypatch.setattr(
        run_publisher.signal,
        "signal",
        lambda _signal, handler: signal_handlers.append(handler),
    )

    def run_batch(_queue, batch_size, stop_event) -> None:
        batch_calls.append(batch_size)
        handler = signal_handlers[0]
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert stop_event.is_set()

    monkeypatch.setattr(run_publisher, "_run_batch", run_batch)
    monkeypatch.setattr(
        run_publisher,
        "close_redis_client",
        lambda: close_calls.append(True),
    )

    run_publisher.main()

    assert batch_calls == [7]
    assert signal_handlers[1] is original_handler
    assert close_calls == [True]


def test_publisher_cli_parses_batch_and_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_publisher",
            "--loop",
            "--batch-size",
            "7",
            "--interval",
            "0.25",
        ],
    )

    args = run_publisher._parse_args()

    assert args.loop is True
    assert args.batch_size == 7
    assert args.interval == 0.25
