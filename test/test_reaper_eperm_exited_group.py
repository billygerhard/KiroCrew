"""An EPERM from a process group whose leader exited is a group with nothing left, not a refusal.

macOS answers ``killpg`` on a group whose only members are zombies -- the exited
leader its parent has not collected yet -- with ``EPERM``, for signal 0 and
SIGKILL alike, and only with ``ESRCH`` once the zombie is collected. Read as "a
member we may not signal", that made the sub-agent teardown call a reset that
had ended the process a survivor, force-kill it, and record a finished run as
``kill failed: PermissionError``. These tests pin the three halves of the fix:
the shim reads that ``EPERM`` as an emptied group ONLY when the group's listing
holds no running member (a running member keeps the refusal), the survivor
check does not count a zombie leader as standing, and a run that completed its
turn keeps its result whatever its teardown hits afterwards. The kernel is
faked throughout, so they hold on every platform.
"""

from __future__ import annotations

import asyncio
import errno
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_force_stop_audit_helpers
from subagent_force_stop_audit_helpers import (
    _REFUSAL,
    _audit,
    _kill_path_stubs,
    _mock_ctx_builder,
    _session_with_pid,
)
from test_subagent_continuable import _mock_sessions as _completing_sessions

from kiro_crew import (
    platform_compat,
    platform_compat_kill,
    platform_compat_liveness,
    process_identity,
)
from kiro_crew.process_identity import ProcessHandle
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import await_until

_kill_seam = subagent_force_stop_audit_helpers._kill_seam
_leads_its_own_group = subagent_force_stop_audit_helpers._leads_its_own_group
_no_tree_behind_the_leader = subagent_force_stop_audit_helpers._no_tree_behind_the_leader

_PGID = 424_242

#: The real probe, bound at import: the shared autouse pin patches
#: ``platform_compat.pgroup_exists``, and the facade forwards that patch to its owner.
_REAL_PGROUP_EXISTS = platform_compat_kill.pgroup_exists


def _eperm() -> PermissionError:
    return PermissionError(errno.EPERM, "Operation not permitted")


def _member(pid: int, *, zombie: bool) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, zombie=zombie, start_id="1.000001")


@pytest.fixture
def posix_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill shim takes its POSIX branch whatever the host is."""
    monkeypatch.setattr(platform_compat_kill, "IS_POSIX", True)


def _group_answers_eperm(live: bool | None) -> Any:
    """``os.killpg`` raising EPERM, and the group's listing answering *live*."""
    return (
        patch.object(platform_compat_kill.os, "killpg", side_effect=_eperm()),
        patch.object(platform_compat_kill, "pgroup_has_live_member", return_value=live),
    )


# ── The listing that decides ──


class TestPgroupHasLiveMember:
    """Positive evidence only: a group called empty must have been listed, and listed as zombies."""

    @pytest.fixture
    def darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat_liveness, "sys", SimpleNamespace(platform="darwin"))

    @pytest.fixture
    def linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat_liveness, "sys", SimpleNamespace(platform="linux"))

    @pytest.mark.usefixtures("darwin")
    def test_a_group_held_only_by_zombies_has_no_live_member(self) -> None:
        members = [_member(_PGID, zombie=True), _member(_PGID + 1, zombie=True)]
        with patch.object(platform_compat_liveness, "darwin_pgroup_members", return_value=members):
            assert platform_compat.pgroup_has_live_member(_PGID) is False

    @pytest.mark.usefixtures("darwin")
    def test_one_running_member_beside_the_zombie_leader_is_live(self) -> None:
        members = [_member(_PGID, zombie=True), _member(_PGID + 1, zombie=False)]
        with patch.object(platform_compat_liveness, "darwin_pgroup_members", return_value=members):
            assert platform_compat.pgroup_has_live_member(_PGID) is True

    @pytest.mark.usefixtures("darwin")
    @pytest.mark.parametrize("listing", [None, []])
    def test_an_unreadable_or_empty_listing_is_unknown(self, listing: Any) -> None:
        with patch.object(platform_compat_liveness, "darwin_pgroup_members", return_value=listing):
            assert platform_compat.pgroup_has_live_member(_PGID) is None

    @pytest.mark.usefixtures("linux")
    def test_linux_names_a_running_member_and_never_calls_a_group_empty(self) -> None:
        with patch.object(platform_compat_liveness, "linux_pgroup_members", return_value={7: 1}):
            assert platform_compat.pgroup_has_live_member(_PGID) is True
        with patch.object(platform_compat_liveness, "linux_pgroup_members", return_value={}):
            assert platform_compat.pgroup_has_live_member(_PGID) is None

    def test_a_reserved_group_id_is_never_answered(self) -> None:
        assert platform_compat.pgroup_has_live_member(1) is None
        assert platform_compat.pgroup_has_live_member(0) is None


# ── The shim: EPERM from an exited group is ESRCH, any other EPERM stands ──


@pytest.mark.usefixtures("posix_shim")
class TestTheKillShimsReadAnExitedGroupsEpermAsGone:
    def test_no_running_member_left_is_the_group_emptied(self) -> None:
        killpg, live = _group_answers_eperm(False)
        with killpg, live:
            with pytest.raises(ProcessLookupError) as raised:
                platform_compat.kill_process_group(_PGID, platform_compat.SIGKILL)
        assert isinstance(raised.value.__cause__, PermissionError)

    def test_a_running_member_keeps_the_refusal(self) -> None:
        killpg, live = _group_answers_eperm(True)
        with killpg, live:
            with pytest.raises(PermissionError):
                platform_compat.kill_process_group(_PGID, platform_compat.SIGKILL)

    def test_a_group_that_cannot_be_listed_keeps_the_refusal(self) -> None:
        killpg, live = _group_answers_eperm(None)
        with killpg, live:
            with pytest.raises(PermissionError):
                platform_compat.kill_process_group(_PGID, platform_compat.SIGKILL)

    def test_the_tree_kill_reads_it_the_same_way(self) -> None:
        killpg, live = _group_answers_eperm(False)
        with (
            killpg,
            live,
            patch.object(platform_compat_kill.os, "getpgid", return_value=_PGID, create=True),
        ):
            with pytest.raises(ProcessLookupError):
                platform_compat.kill_process_tree(_PGID, platform_compat.SIGKILL)

    @pytest.mark.parametrize(("live", "exists"), [(False, False), (True, True), (None, True)])
    def test_the_group_probe_reads_it_the_same_way(self, live: bool | None, exists: bool) -> None:
        killpg, listing = _group_answers_eperm(live)
        with killpg, listing:
            assert _REAL_PGROUP_EXISTS(_PGID) is exists


# ── The reaper's group signal ──


def _handle() -> ProcessHandle:
    return ProcessHandle(pid=_PGID, start_id="1.000001", pgid=_PGID, child_pids={})


@pytest.mark.usefixtures("posix_shim")
class TestTheRetainedGroupSignal:
    """The exact path of the field log: SIGKILL of the group of an exited leader."""

    @pytest.mark.asyncio
    async def test_an_exited_leaders_zombie_only_group_is_nothing_to_kill(self) -> None:
        killpg, live = _group_answers_eperm(False)
        with killpg, live:
            failure = await process_identity._signal_retained_group(
                _handle(), who="Reaper", key="subagent:t", leader_alive=False
            )
        assert failure is None

    @pytest.mark.asyncio
    async def test_a_running_member_the_gateway_may_not_signal_is_still_a_failure(self) -> None:
        killpg, live = _group_answers_eperm(True)
        with killpg, live:
            failure = await process_identity._signal_retained_group(
                _handle(), who="Reaper", key="subagent:t", leader_alive=False
            )
        assert failure is not None
        assert failure.startswith("PermissionError:")
        assert f"process group {_PGID} still has a running member" in failure


# ── The survivor check after a reset that returned ──


class TestAZombieLeaderDidNotSurviveTheReset:
    """A reset that ended the process leaves a zombie until the child watcher collects it."""

    def _probe(self, *, zombie: bool, live: bool | None, group_exists: bool) -> bool:
        with (
            patch("kiro_crew.platform_compat.get_process_start_id", return_value="1.000001"),
            patch("kiro_crew.platform_compat.pid_exists", return_value=True),
            patch.object(process_identity, "_listed_as_zombie", return_value=zombie),
            patch("kiro_crew.platform_compat.pgroup_has_live_member", return_value=live),
            patch("kiro_crew.platform_compat.pgroup_exists", return_value=group_exists),
            patch("kiro_crew.platform_compat.IS_WINDOWS", False),
        ):
            return process_identity.process_survived(_handle())

    def test_a_zombie_leader_alone_in_its_group_is_gone(self) -> None:
        # ``pgroup_exists`` still answers True: a zombie keeps ``killpg(pgid, 0)`` answering.
        assert self._probe(zombie=True, live=False, group_exists=True) is False

    def test_a_zombie_leader_with_a_running_member_still_stands(self) -> None:
        assert self._probe(zombie=True, live=True, group_exists=True) is True

    def test_a_zombie_leader_whose_group_cannot_be_listed_asks_the_probe(self) -> None:
        assert self._probe(zombie=True, live=None, group_exists=True) is True
        assert self._probe(zombie=True, live=None, group_exists=False) is False

    def test_a_running_leader_stands(self) -> None:
        assert self._probe(zombie=False, live=False, group_exists=False) is True


# ── A finished run keeps its result ──


def _complete_event() -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_COMPLETE,
        stop_reason="end_turn",
        text="",
        title="",
        tool_input="",
        runtime_global=False,
        refusal=None,
    )


def _finishing_run(seen: list[Any]) -> SubagentManager:
    """A manager whose provider answers one whole turn, so a spawned run claims its completed ending."""
    sessions = _completing_sessions()
    sessions._sessions = {}
    sessions.tearing_down = MagicMock(return_value=[])
    provider = sessions.get_or_create.return_value[0]

    async def _whole_answer(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="PR merged.", runtime_global=False)
        yield _complete_event()

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _whole_answer())

    async def _on_done(info: Any) -> None:
        seen.append((info.error, info.outcome))

    return SubagentManager(
        sessions=sessions,
        ctx_builder=_mock_ctx_builder(),
        on_done=_on_done,
        on_event=AsyncMock(),
        is_yolo=lambda: True,
    )


@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
class TestAFinishedRunKeepsItsResult:
    @pytest.mark.asyncio
    async def test_a_teardown_kill_that_fails_after_the_turn_completed_does_not_fail_the_run(
        self,
    ) -> None:
        seen: list[Any] = []
        manager = _finishing_run(seen)
        children, alive, start_id, sweep = _kill_path_stubs()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel") as mock_sel,
            patch("kiro_crew.subagent.mark_delivered") as delivered,
            children,
            alive,
            start_id,
            sweep,
            patch(
                "kiro_crew.platform_compat.kill_process_group",
                MagicMock(side_effect=_REFUSAL),
            ),
        ):
            info = manager.spawn("merge the PR and report", keep=True)
            assert info is not None
            # Live under its key when the teardown snapshots it; the reset
            # returns, the process reads as standing and the kill is refused.
            _session_with_pid(manager, f"subagent:{info.id}", 9911)
            await await_until(manager._tasks[info.id], what="the run finishing")
            await await_until(
                asyncio.gather(*manager._report_tasks, return_exceptions=True),
                what="the report tasks finishing",
            )

        assert info._ending_claimed, "the fixture must reach a claimed completed ending"
        assert len(seen) == 1 and not seen[0][0], seen
        assert seen[0][1] == "completed", seen
        assert not info.error
        assert info.outcome == "completed"
        # The teardown problem is not lost: audited, and the process handed to
        # the next start through the leaked-process tombstone.
        assert _audit(mock_sel, "run_finally_force_kill")["outcome"] == "failed"
        delivered.assert_called_once()
        assert delivered.call_args.kwargs["leaked_process"] is True
        assert delivered.call_args.kwargs["outcome"] == "completed"
