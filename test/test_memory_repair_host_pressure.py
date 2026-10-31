"""The member-memory embedding repair waits while the host is busy."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from kiro_crew import resource_status
from kiro_crew.slack.gateway import GatewayOrchestrator


def _gateway():
    gateway = object.__new__(GatewayOrchestrator)
    gateway._memory_repair_stop = threading.Event()
    return gateway


def _status(posture="ample", load_per_cpu=1.0):
    return SimpleNamespace(posture=posture, load_per_cpu=load_per_cpu)


@pytest.fixture
def host(monkeypatch):
    """Drive the three host signals the pressure check reads."""
    state = {"level_held": False, "status": _status(), "probe_error": None}
    monkeypatch.setattr(resource_status, "read_memory_pressure_level", lambda: 2)
    monkeypatch.setattr(resource_status, "pressure_level_held", lambda level: state["level_held"])

    def probe(cfg=None):
        if state["probe_error"]:
            raise state["probe_error"]
        return state["status"]

    monkeypatch.setattr(resource_status, "probe", probe)
    return state


def test_clear_host_allows_a_round(host):
    assert _gateway()._embedding_repair_pressure() is None


def test_kernel_memory_pressure_defers(host):
    host["level_held"] = True
    assert _gateway()._embedding_repair_pressure() == "kernel memory pressure"


@pytest.mark.parametrize("posture", [resource_status.POSTURE_TIGHT, resource_status.POSTURE_CRITICAL])
def test_tight_or_critical_memory_defers(host, posture):
    host["status"] = _status(posture=posture)
    assert _gateway()._embedding_repair_pressure() == f"memory {posture}"


def test_high_load_defers_and_moderate_load_does_not(host):
    host["status"] = _status(load_per_cpu=4.5)
    assert _gateway()._embedding_repair_pressure() == "load 4.5/core"
    host["status"] = _status(load_per_cpu=3.9)
    assert _gateway()._embedding_repair_pressure() is None


def test_unknown_load_and_unreadable_probe_fail_open(host):
    host["status"] = _status(load_per_cpu=None)
    assert _gateway()._embedding_repair_pressure() is None
    host["probe_error"] = OSError("sysctl unavailable")
    assert _gateway()._embedding_repair_pressure() is None


@pytest.mark.asyncio
async def test_loop_skips_rounds_while_busy_backs_off_then_resumes(monkeypatch, caplog):
    gateway = _gateway()
    verdicts = iter(["kernel memory pressure", "kernel memory pressure", "load 5.0/core", None])
    rounds = []
    sleeps = []

    gateway._embedding_repair_pressure = lambda: next(verdicts)

    def repair_once():
        rounds.append(len(sleeps))
        gateway._memory_repair_stop.set()

    gateway._repair_member_memory_once = repair_once

    async def fake_sleep(secs):
        sleeps.append(secs)

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr("kiro_crew.slack.gateway.embed_executor", lambda: executor)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            CancelledError=asyncio.CancelledError,
            sleep=fake_sleep,
        ),
    )
    try:
        with caplog.at_level("INFO", logger="kiro_crew.slack.gateway"):
            await gateway._repair_member_memory()
    finally:
        executor.shutdown(wait=True)

    # Three busy verdicts: no round, waits of 30, 60, 120. Then one round on a clear host.
    assert sleeps == [30.0, 60.0, 120.0, 30]
    assert rounds == [3]
    paused = [r for r in caplog.records if "paused while the host is busy" in r.getMessage()]
    resumed = [r for r in caplog.records if "repair resumed" in r.getMessage()]
    assert len(paused) == 1 and "kernel memory pressure" in paused[0].getMessage()
    assert len(resumed) == 1


@pytest.mark.asyncio
async def test_backoff_is_capped_at_five_minutes(monkeypatch):
    gateway = _gateway()
    sleeps = []
    gateway._embedding_repair_pressure = lambda: "memory critical"
    gateway._repair_member_memory_once = lambda: pytest.fail("no round while busy")

    async def fake_sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 7:
            gateway._memory_repair_stop.set()

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr("kiro_crew.slack.gateway.embed_executor", lambda: executor)
    monkeypatch.setattr(
        "kiro_crew.slack.gateway.asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            CancelledError=asyncio.CancelledError,
            sleep=fake_sleep,
        ),
    )
    try:
        await gateway._repair_member_memory()
    finally:
        executor.shutdown(wait=True)

    assert sleeps == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0, 300.0]
