"""Audit log behaviour: append-only, outside every spec directory, loud on failure.

The two claims worth pinning are that the log only ever grows — an entry a later
operation could rewrite is evidence of nothing — and that it lives outside the
spec directory, since a log written next to the documents would be read by the
IDE and CLI as part of the spec.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.spec_engine.engine.audit import AuditLog, audit_root
from kiro_crew.apps.builtins.spec_engine.engine.state import SpecRef, StatePersistenceError
from kiro_crew.platform_compat import IS_POSIX

from .conftest import NATIVE_SPEC_FILES, spec_dir_snapshot


@pytest.fixture()
def log(state_dir: Path) -> AuditLog:
    return AuditLog(root=state_dir)


class TestAppend:
    def test_events_read_back_in_the_order_they_were_written(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        log.append(ref, "spec.created", initiator="user:ada")
        log.append(ref, "gate.approved", run="run-1", initiator="policy:autonomy")
        log.append(ref, "run.completed", run="run-1", cost=2.5)
        events = log.read(ref)
        assert [event.event for event in events] == [
            "spec.created",
            "gate.approved",
            "run.completed",
        ]
        assert events[1].run == "run-1"
        assert events[1].initiator == "policy:autonomy"
        assert events[2].cost == 2.5

    def test_optional_fields_are_omitted_rather_than_written_as_null(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        log.append(ref, "spec.created")
        record = json.loads(log.path_for(ref).read_text(encoding="utf-8").strip())
        assert set(record) == {"ts", "event"}

    def test_detail_round_trips(self, log: AuditLog, ref: SpecRef) -> None:
        detail = {"gates": ["lint", "tests"], "exit_status": 1}
        log.append(ref, "quality_gate.failed", detail=detail)
        assert log.read(ref)[0].detail == detail

    def test_an_unserialisable_detail_fails_the_append(self, log: AuditLog, ref: SpecRef) -> None:
        # ``default=str`` covers exotic values; a key that is not a string is a
        # genuine encoding failure and must not be written half-formed.
        with pytest.raises(StatePersistenceError):
            log.append(ref, "run.started", detail={(1, 2): "tuple key"})  # type: ignore[dict-item]
        assert log.read(ref) == []

    def test_an_event_needs_a_name(self, log: AuditLog, ref: SpecRef) -> None:
        with pytest.raises(ValueError):
            log.append(ref, "")

    def test_an_explicit_timestamp_is_kept(self, log: AuditLog, ref: SpecRef) -> None:
        log.append(ref, "run.started", ts="2026-01-01T00:00:00+00:00")
        assert log.read(ref)[0].ts == "2026-01-01T00:00:00+00:00"

    def test_reading_a_spec_with_no_log_yields_nothing(self, log: AuditLog, project: Path) -> None:
        assert log.read(SpecRef.of(project, "absent")) == []

    def test_limit_returns_the_most_recent_events(self, log: AuditLog, ref: SpecRef) -> None:
        for index in range(5):
            log.append(ref, f"event.{index}")
        assert [event.event for event in log.read(ref, limit=2)] == ["event.3", "event.4"]
        assert log.read(ref, limit=0) == []


class TestAppendOnly:
    def test_later_appends_never_rewrite_earlier_lines(self, log: AuditLog, ref: SpecRef) -> None:
        log.append(ref, "spec.created")
        first = log.path_for(ref).read_text(encoding="utf-8")
        log.append(ref, "gate.approved")
        second = log.path_for(ref).read_text(encoding="utf-8")
        assert second.startswith(first)
        assert len(second) > len(first)

    def test_a_torn_tail_line_is_preserved_and_the_next_record_stays_readable(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        log.append(ref, "spec.created")
        path = log.path_for(ref)
        # Simulate a crash mid-append: a fragment with no trailing newline.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"ts": "2026-01-01T00:00:00+00:00", "event": "tor')
        log.append(ref, "gate.approved")
        text = path.read_text(encoding="utf-8")
        assert '"event": "tor' in text
        assert [event.event for event in log.read(ref)] == ["spec.created", "gate.approved"]

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX file modes")
    def test_the_log_is_owner_only(self, log: AuditLog, ref: SpecRef) -> None:
        log.append(ref, "spec.created")
        assert oct(os.stat(log.path_for(ref)).st_mode & 0o777) == oct(0o600)

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX file modes")
    def test_a_pre_existing_wider_mode_is_narrowed(self, log: AuditLog, ref: SpecRef) -> None:
        path = log.path_for(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        os.chmod(path, 0o644)
        log.append(ref, "spec.created")
        assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)

    def test_an_unparseable_line_is_skipped_rather_than_failing_the_read(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        log.append(ref, "spec.created")
        with open(log.path_for(ref), "a", encoding="utf-8") as handle:
            handle.write("not json at all\n")
        log.append(ref, "gate.approved")
        assert [event.event for event in log.read(ref)] == ["spec.created", "gate.approved"]

    def test_a_json_line_that_is_not_an_object_is_skipped(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        path = log.path_for(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        assert log.read(ref) == []


class TestLocation:
    def test_the_log_lives_outside_every_spec_directory(
        self, log: AuditLog, ref: SpecRef, state_dir: Path
    ) -> None:
        log.append(ref, "spec.created")
        path = log.path_for(ref)
        assert path.is_relative_to(audit_root(state_dir))
        assert ".kiro/specs" not in path.as_posix()
        assert spec_dir_snapshot(ref.spec_dir).keys() == set(NATIVE_SPEC_FILES)

    def test_an_audit_root_inside_a_spec_tree_is_refused(self, project: Path) -> None:
        with pytest.raises(StatePersistenceError):
            AuditLog(root=project / ".kiro" / "specs" / "example")

    def test_same_named_specs_in_different_projects_do_not_share_a_log(
        self, log: AuditLog, tmp_path: Path
    ) -> None:
        first = SpecRef.of(tmp_path / "one", "login")
        second = SpecRef.of(tmp_path / "two", "login")
        log.append(first, "spec.created")
        log.append(second, "spec.created")
        assert log.path_for(first) != log.path_for(second)
        assert len(log.read(first)) == 1
        assert len(log.read(second)) == 1

    def test_a_project_path_cannot_escape_the_audit_directory(
        self, log: AuditLog, tmp_path: Path, state_dir: Path
    ) -> None:
        awkward = SpecRef.of(tmp_path / "weird name (v2)", "spec")
        path = log.path_for(awkward)
        assert path.is_relative_to(audit_root(state_dir))
        log.append(awkward, "spec.created")
        assert len(log.read(awkward)) == 1


class TestFailure:
    def test_an_unwritable_log_path_fails_the_operation_and_leaves_the_spec_alone(
        self, log: AuditLog, ref: SpecRef
    ) -> None:
        before = spec_dir_snapshot(ref.spec_dir)
        # A file where the per-project directory belongs: mkdir cannot succeed.
        blocker = log.path_for(ref).parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory", encoding="utf-8")
        with pytest.raises(StatePersistenceError):
            log.append(ref, "spec.created")
        assert spec_dir_snapshot(ref.spec_dir) == before

    def test_an_unreadable_log_is_reported_rather_than_read_as_empty(
        self, log: AuditLog, ref: SpecRef, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log.append(ref, "spec.created")

        def boom(*_args: object, **_kwargs: object) -> str:
            raise OSError("input/output error")

        monkeypatch.setattr(Path, "read_text", boom)
        with pytest.raises(StatePersistenceError):
            log.read(ref)
