"""`agentpilot.jobs.dispatch` -- how many tasks a batch may admit, and where the
memory number comes from.

`permits()` is tested with an explicit `pressure` so the assertions do not depend
on the machine running them. The readers are tested by pointing the module's path
constants at fixture files, which is also the only way to test the cgroup paths
at all on a macOS dev box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentpilot.jobs import dispatch

# --------------------------------------------------------------- permits()


def test_unknown_pressure_admits_the_full_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform that will not say how much memory is left gets no throttling.
    Throttling on a number this module does not have would be worse than not
    throttling."""

    monkeypatch.setattr(dispatch, "read_memory_pressure", lambda: None)
    assert dispatch.permits(8) == 8


def test_low_pressure_admits_the_full_batch() -> None:
    assert dispatch.permits(8, pressure=0.2) == 8
    assert dispatch.permits(8, pressure=dispatch.SOFT_PRESSURE - 0.01) == 8


def test_soft_pressure_halves_the_batch() -> None:
    assert dispatch.permits(8, pressure=dispatch.SOFT_PRESSURE) == 4
    assert dispatch.permits(8, pressure=dispatch.HARD_PRESSURE - 0.01) == 4


def test_hard_pressure_admits_one_at_a_time() -> None:
    assert dispatch.permits(8, pressure=dispatch.HARD_PRESSURE) == 1
    assert dispatch.permits(8, pressure=0.99) == 1


def test_it_never_admits_zero() -> None:
    """A worker that admits nothing never drains the queue -- the job stalls
    silently instead of finishing slowly. One at a time still finishes."""

    for pressure in (0.85, 0.95, 1.0, 5.0):
        assert dispatch.permits(1, pressure=pressure) >= 1
        assert dispatch.permits(8, pressure=pressure) >= 1


def test_it_never_admits_more_than_configured() -> None:
    """The memory signal is only ever allowed to reduce concurrency. A quiet box
    is not a reason to exceed a ceiling an operator set deliberately."""

    for pressure in (0.0, 0.1, 0.5, 0.69):
        assert dispatch.permits(3, pressure=pressure) <= 3


def test_a_batch_of_one_survives_halving() -> None:
    assert dispatch.permits(1, pressure=dispatch.SOFT_PRESSURE) == 1


# ----------------------------------------------------------------- readers


def test_cgroup_v2_reports_a_fraction_of_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container's limit, not the host's memory -- which is the whole reason
    this reads the cgroup instead of using psutil. A worker capped at 2 GB on a
    64 GB host is at 75% here and at 2% by psutil's reckoning."""

    current = tmp_path / "memory.current"
    maximum = tmp_path / "memory.max"
    current.write_text("1610612736\n")  # 1.5 GiB
    maximum.write_text("2147483648\n")  # 2 GiB
    monkeypatch.setattr(dispatch, "_CGROUP_V2_CURRENT", current)
    monkeypatch.setattr(dispatch, "_CGROUP_V2_MAX", maximum)

    assert dispatch._cgroup_v2() == pytest.approx(0.75)


def test_an_unlimited_cgroup_v2_declines_to_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`memory.max` of `max` means no limit, so there is no fraction to be a
    fraction *of* -- falling through to /proc/meminfo is right, guessing is
    not."""

    current = tmp_path / "memory.current"
    maximum = tmp_path / "memory.max"
    current.write_text("1000\n")
    maximum.write_text("max\n")
    monkeypatch.setattr(dispatch, "_CGROUP_V2_CURRENT", current)
    monkeypatch.setattr(dispatch, "_CGROUP_V2_MAX", maximum)

    assert dispatch._cgroup_v2() is None


def test_cgroup_v1_treats_its_huge_sentinel_as_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cgroup v1 writes a very large number instead of v2's literal `max`.
    Taken at face value it reports ~0% pressure forever, which would silently
    disable throttling on a v1 host."""

    usage = tmp_path / "usage"
    limit = tmp_path / "limit"
    usage.write_text("1000\n")
    limit.write_text("9223372036854771712\n")
    monkeypatch.setattr(dispatch, "_CGROUP_V1_USAGE", usage)
    monkeypatch.setattr(dispatch, "_CGROUP_V1_LIMIT", limit)

    assert dispatch._cgroup_v1() is None


def test_proc_meminfo_uses_available_not_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MemFree` excludes page cache and reclaimable slab, so a healthy Linux box
    reports almost none and this would throttle permanently. `MemAvailable` is
    the kernel's own estimate of what a new allocation could actually get.

    Here `MemFree` is 1% of total while `MemAvailable` is 50%: reading the wrong
    one reports 99% pressure on a box that is fine.
    """

    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16000000 kB\n"
        "MemFree:          160000 kB\n"
        "MemAvailable:    8000000 kB\n"
        "Buffers:          100000 kB\n"
    )
    monkeypatch.setattr(dispatch, "_PROC_MEMINFO", meminfo)

    assert dispatch._proc_meminfo() == pytest.approx(0.5)


def test_read_memory_pressure_returns_none_when_nothing_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope"
    for attr in (
        "_CGROUP_V2_CURRENT",
        "_CGROUP_V2_MAX",
        "_CGROUP_V1_USAGE",
        "_CGROUP_V1_LIMIT",
        "_PROC_MEMINFO",
    ):
        monkeypatch.setattr(dispatch, attr, missing)

    assert dispatch.read_memory_pressure() is None


def test_a_malformed_file_is_skipped_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission control must not be able to crash the worker tick. A cgroup file
    with something unexpected in it falls through to the next reader."""

    current = tmp_path / "memory.current"
    maximum = tmp_path / "memory.max"
    current.write_text("not-a-number\n")
    maximum.write_text("2147483648\n")
    monkeypatch.setattr(dispatch, "_CGROUP_V2_CURRENT", current)
    monkeypatch.setattr(dispatch, "_CGROUP_V2_MAX", maximum)
    missing = tmp_path / "nope"
    monkeypatch.setattr(dispatch, "_CGROUP_V1_USAGE", missing)
    monkeypatch.setattr(dispatch, "_PROC_MEMINFO", missing)

    assert dispatch.read_memory_pressure() is None
