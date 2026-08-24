"""Braintrust is an ordered, asynchronous, fail-soft projection."""

from __future__ import annotations

import threading

from autosymph.logging.braintrust import AsyncBraintrustProjector


class _Tracer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def slow(self) -> None:
        self.entered.set()
        self.release.wait(timeout=2)
        self.calls.append("slow")

    def after(self) -> None:
        self.calls.append("after")

    def fail(self) -> None:
        raise RuntimeError("provider offline")


def test_projection_is_non_blocking_and_ordered() -> None:
    tracer = _Tracer()
    projector = AsyncBraintrustProjector(tracer)

    assert projector.submit("slow") is True
    assert tracer.entered.wait(timeout=1)
    assert projector.submit("after") is True
    assert tracer.calls == []

    tracer.release.set()
    projector.close()

    assert tracer.calls == ["slow", "after"]


def test_provider_failure_is_captured_not_raised() -> None:
    tracer = _Tracer()
    projector = AsyncBraintrustProjector(tracer)

    assert projector.submit("fail") is True
    projector.close()

    assert isinstance(projector.failure, RuntimeError)
