import argparse
import signal
import sys

import pytest

from app.worker import runner


def test_worker_cli_parses_and_validates_poll_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["worker", "--interval", "0.25"])

    assert runner._parse_args().interval == 0.25

    monkeypatch.setattr(sys, "argv", ["worker", "--interval", "0"])
    with pytest.raises(SystemExit):
        runner._parse_args()


def test_runner_installs_shutdown_handlers_and_closes_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_handlers: dict[int, object] = {}
    close_calls: list[bool] = []
    signals = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    previous_handlers = {signum: object() for signum in signals}
    monkeypatch.setattr(
        runner,
        "_parse_args",
        lambda: argparse.Namespace(interval=0.5),
    )
    monkeypatch.setattr(
        runner.signal,
        "getsignal",
        lambda signum: previous_handlers[signum],
    )

    def set_handler(signum: int, handler: object) -> None:
        registered_handlers[signum] = handler

    monkeypatch.setattr(runner.signal, "signal", set_handler)
    monkeypatch.setattr(
        runner,
        "close_redis_client",
        lambda: close_calls.append(True),
    )

    class FakeWorker:
        def __init__(self, *, poll_interval: float) -> None:
            assert poll_interval == 0.5

        def run(self, stop_event) -> None:
            assert stop_event.is_set() is False
            for handler in registered_handlers.values():
                handler(signal.SIGINT, None)
            assert stop_event.is_set()

    monkeypatch.setattr(runner, "Worker", FakeWorker)

    runner.main()

    assert set(registered_handlers) == set(signals)
    assert close_calls == [True]
