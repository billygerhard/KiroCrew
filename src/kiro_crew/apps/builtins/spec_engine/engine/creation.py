"""Creating a spec, atomically, with its type recorded.

The failure this module exists to eliminate is the half-created spec: a
directory that exists but records no type. Nothing downstream can work with one.
The engine refuses to validate or advance it, the IDE lists it as a spec with no
workflow, and the person who asked for it has to notice the difference between
"created" and "created and usable" -- which nobody does, so it surfaces later as
a confusing refusal instead of a clear failure at the moment of creation.

So creation is all-or-nothing. The spec is assembled in a staging directory
outside the specs tree and moved into place in one step, and the move happens
only once the type is recorded. Anything that fails first -- an unknown type, a
sidecar that will not write, engine state that will not persist -- leaves the
specs tree exactly as it was.

What creation writes is the sidecar and nothing else. The native documents are
authored afterwards, one gate at a time, and pre-creating empty ones would make a
brand-new spec look like a spec whose requirements had already been written.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .spec_types import (
    SIDECAR_FILENAME,
    DocumentPlan,
    Sidecar,
    SpecType,
    build_sidecar,
    write_sidecar_document,
)
from .state import SpecRecord, SpecRef, StatePersistenceError, StateStore

logger = logging.getLogger(__name__)

#: A spec directory name. It becomes a path segment, a display name, and part of
#: a state key, so it is restricted to characters that are unambiguous in all
#: three rather than sanitised differently by each.
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

#: Where a spec is assembled before it is moved into place. Inside ``.kiro`` so
#: the move is a rename on one filesystem, and outside ``.kiro/specs`` so a
#: half-assembled spec is never visible to anything that lists specs.
STAGING_PREFIX = ".spec-staging-"

#: Lock owner recorded while a spec is being created, when the caller names none.
DEFAULT_LOCK_OWNER = "spec-create"


class SpecCreationError(Exception):
    """Base class for creation failures. No spec directory was left behind."""


class InvalidSpecName(SpecCreationError):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"invalid spec name {name!r}: {reason}")
        self.name = name
        self.reason = reason


class SpecAlreadyExists(SpecCreationError):
    """A spec directory of that name is already there, and was not touched."""

    def __init__(self, spec_dir: Path) -> None:
        super().__init__(f"a spec already exists at {spec_dir}")
        self.spec_dir = spec_dir


class SpecTypeNotRecorded(SpecCreationError):
    """The type could not be recorded, so the spec was not created.

    Carries the underlying failure as ``__cause__``. The directory the engine was
    building has been removed; the specs tree is as it was before the call.
    """

    def __init__(self, spec_dir: Path, detail: str) -> None:
        super().__init__(
            f"could not record the spec type for {spec_dir}: {detail}; "
            f"no spec directory was created"
        )
        self.spec_dir = spec_dir
        self.detail = detail


@dataclass(frozen=True)
class CreatedSpec:
    """What a successful creation produced."""

    ref: SpecRef
    spec_dir: Path
    sidecar: Sidecar
    record: SpecRecord

    @property
    def spec_type(self) -> SpecType:
        return self.sidecar.spec_type

    @property
    def plan(self) -> DocumentPlan:
        """The document plan, derived from the type that was actually recorded."""
        return self.sidecar.plan


def validate_spec_name(name: str) -> str:
    """Return ``name`` unchanged, or raise :class:`InvalidSpecName`."""
    if not name or not name.strip():
        raise InvalidSpecName(name, "a spec name must not be empty")
    if name != name.strip():
        raise InvalidSpecName(name, "a spec name must not be padded with whitespace")
    if not NAME_PATTERN.match(name):
        raise InvalidSpecName(
            name,
            "a spec name starts with a letter or digit and continues with letters, "
            "digits, hyphens, or underscores",
        )
    return name


def create_spec(
    project: Path | str,
    name: str,
    spec_type: SpecType | str,
    *,
    store: StateStore,
    spec_id: str | None = None,
    owner: str = DEFAULT_LOCK_OWNER,
) -> CreatedSpec:
    """Create the spec directory for ``name`` with ``spec_type`` recorded in it.

    The type is validated before anything is created, so an unknown one raises
    :class:`~.spec_types.UnknownSpecType` without having touched the filesystem.

    Raises :class:`SpecAlreadyExists` rather than adopting or overwriting a
    directory that is already there: an existing spec may hold authored work, and
    a creation call is not permission to replace it.

    Two writers racing on the same name serialise through the spec's lock, and
    the loser is rejected with :class:`~.state.SpecLocked` carrying the spec's
    current state.
    """
    validate_spec_name(name)
    ref = SpecRef.of(project, name)
    spec_dir = ref.spec_dir
    # Built before the lock so an unknown type costs nothing and leaves no row.
    sidecar = build_sidecar(spec_type, spec_id=spec_id)

    with store.lock(ref, owner=owner) as lock:
        if spec_dir.exists():
            raise SpecAlreadyExists(spec_dir)
        # Cheap, but it is the difference between failing here and writing a
        # directory a second writer has already been told it owns.
        store.verify_lock(lock)
        _stage_and_move(spec_dir, sidecar)
        try:
            record = store.register_spec(ref, spec_type=sidecar.spec_type.value)
        except StatePersistenceError as exc:
            # The type is in the sidecar, but the engine cannot record that this
            # spec exists. Undoing the directory keeps the two consistent: a spec
            # the engine does not know about is exactly the half-created state
            # this function refuses to produce.
            _remove_created(spec_dir)
            raise SpecTypeNotRecorded(spec_dir, str(exc)) from exc
    return CreatedSpec(ref=ref, spec_dir=spec_dir, sidecar=sidecar, record=record)


def _stage_and_move(spec_dir: Path, sidecar: Sidecar) -> None:
    """Assemble the spec in a staging directory and move it into place.

    The move is the only step that makes the spec visible, and it runs after the
    sidecar is written, so a spec directory never exists without its type.
    """
    specs_root = spec_dir.parent
    staging_root = specs_root.parent
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        specs_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SpecTypeNotRecorded(spec_dir, str(exc)) from exc

    try:
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=staging_root))
    except OSError as exc:
        raise SpecTypeNotRecorded(spec_dir, str(exc)) from exc

    try:
        write_sidecar_document(staging, sidecar.to_json_object())
        _move_into_place(staging, spec_dir)
    except OSError as exc:
        raise SpecTypeNotRecorded(spec_dir, str(exc)) from exc
    finally:
        # Present only when the move did not happen. Removing it is what keeps a
        # failed creation from littering the project tree.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _move_into_place(staging: Path, spec_dir: Path) -> None:
    """Move the assembled spec onto its final path.

    ``rename``, not ``replace``: the target must not exist. Replace would quietly
    adopt an empty directory a concurrent writer had just made.
    """
    os.rename(staging, spec_dir)


def _remove_created(spec_dir: Path) -> None:
    """Undo a directory this call created, refusing to remove anything else.

    The guard is deliberate. Rolling back means deleting a directory inside the
    user's project, so it happens only while the contents are still exactly what
    creation wrote. Anything else -- a racing writer, an editor's scratch file --
    means something is in there that this function did not put there, and
    leaving it is the safer failure.
    """
    expected = {SIDECAR_FILENAME}
    try:
        present = {entry.name for entry in spec_dir.iterdir()}
    except OSError as exc:  # pragma: no cover - the directory was just created
        logger.warning("could not inspect %s while undoing a creation: %s", spec_dir, exc)
        return
    if not present <= expected:
        logger.warning(
            "leaving %s in place while undoing a creation: it holds %s",
            spec_dir,
            ", ".join(sorted(present - expected)),
        )
        return
    shutil.rmtree(spec_dir, ignore_errors=True)
