"""The kill-switch flag: the one bit that says "stop", and how it is read.

Split from the engine-wide stop operation in :mod:`.killswitch` on purpose.
Reading this flag has to be cheap and dependency-free, because the zero-token
watch tick reads it before every poll and the budget guard reads it before every
turn. A flag whose reader needed a run machine, a notifier, and an audit log to
answer "am I engaged" would not be readable from those places at all.

Three properties are load-bearing.

**Absence is the only "go".** An unreadable or unparseable file reads as
*engaged*. A file that exists but cannot be parsed is not evidence that nobody
threw the switch — it is the absence of evidence either way, and for a safety
control those are not interchangeable.

**The record is the first engage, not the last.** A second engage keeps the
initiator, reason, and timestamp of the first, because the question asked
afterwards is who stopped the engine and when.

**A failed write fails the operation.** A switch that reported success without
landing on disk would read as released at the next process start, which is the
one failure this control cannot absorb.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import atomic_write

from ..state import StatePersistenceError, reject_spec_tree_path, state_root, utc_now_iso

logger = logging.getLogger(__name__)

#: File under the state root holding the switch. Beside the state database rather
#: than inside it: the watch tick reads this on every poll and has no reason to
#: open a database to learn that nobody has thrown the switch.
KILL_SWITCH_FILENAME = "kill_switch.json"

#: Owner-only. The record names who stopped the engine and why.
_FILE_MODE = 0o600

#: Fields of the persisted record.
FIELD_ENGAGED = "engaged"
FIELD_INITIATOR = "initiator"
FIELD_REASON = "reason"
FIELD_ENGAGED_TS = "engaged_ts"


@dataclass(frozen=True)
class KillSwitchState:
    """What the persisted flag says right now."""

    engaged: bool
    initiator: str = ""
    reason: str = ""
    engaged_ts: str = ""
    #: True when the flag file exists but could not be read or parsed. The switch
    #: reads engaged in that case, and this records that the reason was doubt
    #: rather than an operator.
    unreadable: bool = False

    def describe(self) -> str:
        """One line for a human."""
        if not self.engaged:
            return "kill switch: released"
        if self.unreadable:
            return "kill switch: engaged (its record could not be read)"
        who = self.initiator or "an operator"
        because = f": {self.reason}" if self.reason else ""
        return f"kill switch: engaged by {who} at {self.engaged_ts}{because}"


class KillSwitch:
    """The persisted stop flag, read per attempt and written by an operator action.

    Does no halting of its own. What the flag stops is decided by its readers —
    the watch tick, the dispatch gate, and the budget guard — each of which reads
    it per attempt rather than being told once, so work that did not exist when
    the switch was thrown is stopped as well.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        resolved = Path(root) if root is not None else None
        if resolved is not None:
            reject_spec_tree_path(resolved)
        self._root = resolved

    @property
    def root(self) -> Path:
        """The state root holding the flag, resolved live when unset.

        Resolved per call rather than captured at construction, so a
        ``KIROCREW_HOME`` override set after this object was built is honoured.
        """
        return self._root if self._root is not None else state_root()

    @property
    def path(self) -> Path:
        """The flag file, whether or not it exists."""
        return self.root / KILL_SWITCH_FILENAME

    def read(self) -> KillSwitchState:
        """The switch's current state. Doubt reads as engaged."""
        path = self.path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return KillSwitchState(engaged=False)
        except OSError as exc:
            return self._unreadable(f"cannot be read ({exc})")
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return self._unreadable("is not readable JSON")
        if not isinstance(record, dict):
            return self._unreadable("does not hold an object")
        return KillSwitchState(
            engaged=bool(record.get(FIELD_ENGAGED, False)),
            initiator=str(record.get(FIELD_INITIATOR, "")),
            reason=str(record.get(FIELD_REASON, "")),
            engaged_ts=str(record.get(FIELD_ENGAGED_TS, "")),
        )

    @property
    def engaged(self) -> bool:
        """Whether unattended work is stopped right now."""
        return self.read().engaged

    def engage(self, *, initiator: str, reason: str = "") -> KillSwitchState:
        """Throw the switch, and return the record in force afterwards.

        Raises :class:`~..state.StatePersistenceError` when the flag cannot be
        persisted, so the caller fails its operation rather than reporting a stop
        that no later process would see.
        """
        current = self.read()
        if current.engaged and not current.unreadable:
            return current
        record = KillSwitchState(
            engaged=True,
            initiator=initiator,
            reason=reason,
            engaged_ts=utc_now_iso(),
        )
        self._persist(record)
        logger.warning("%s", record.describe())
        return record

    def release(self, *, initiator: str = "") -> bool:
        """Release the switch. Returns whether it had been engaged.

        The file is removed rather than rewritten as released, because absence is
        the only state that reads as "go": a rewritten record that failed to land
        would leave a stop behind.
        """
        was_engaged = self.read().engaged
        path = self.path
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StatePersistenceError(
                f"cannot release the kill switch at {path}: {exc}"
            ) from exc
        logger.warning("kill switch released by %s", initiator or "an operator")
        return was_engaged

    def _unreadable(self, problem: str) -> KillSwitchState:
        logger.error("kill switch flag %s %s; treating it as engaged", self.path, problem)
        return KillSwitchState(engaged=True, unreadable=True)

    def _persist(self, record: KillSwitchState) -> None:
        path = self.path
        payload = {
            FIELD_ENGAGED: record.engaged,
            FIELD_INITIATOR: record.initiator,
            FIELD_REASON: record.reason,
            FIELD_ENGAGED_TS: record.engaged_ts,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                mode=_FILE_MODE,
            )
        except OSError as exc:
            raise StatePersistenceError(
                f"cannot persist the kill switch at {path}: {exc}"
            ) from exc
