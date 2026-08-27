"""The setting registry: every scalar knob the engine has, and its bundled default.

This module is the owning module for the engine's numeric limits. A setting that
is not in ``SETTINGS`` cannot be written (the validated write path rejects
unknown keys) and cannot be read (there is no other resolver), so the table
below is the whole vocabulary rather than a convenience list that drifts from
the code using it.

Two invariants make zero-configuration safe:

* **Every setting has a default.** An absent optional setting resolves to that
  default; absence alone never fails an operation or blocks a run. There is no
  "unset" state a caller has to handle.
* **Every default is finite.** A limit whose default were "unbounded" would let
  an unattended run retry, poll, or spend without a ceiling, which is the one
  failure mode a headless run cannot recover from on its own.

``scopes`` records where a setting may be overridden. A setting overridable at
``PROJECT`` scope but written under a source is a configuration error rather
than a silently ignored key, because a silently ignored autonomy- or
budget-shaped override reads as applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any


class Scope(str, Enum):
    """The configuration layer a value comes from.

    Precedence runs source over project over app, so the narrowest declaration
    wins: a per-source poll interval beats the app-wide one.
    """

    APP = "app"
    PROJECT = "project"
    SOURCE = "source"


#: Resolution order, narrowest first. Also the order the effective-value
#: resolver walks, so it doubles as the precedence definition.
SCOPE_PRECEDENCE: tuple[Scope, ...] = (Scope.SOURCE, Scope.PROJECT, Scope.APP)

_APP = frozenset({Scope.APP})
_APP_PROJECT = frozenset({Scope.APP, Scope.PROJECT})
_APP_SOURCE = frozenset({Scope.APP, Scope.SOURCE})


@dataclass(frozen=True)
class Setting:
    """One scalar knob: its dotted key, bundled default, type, and bounds."""

    key: str
    default: Any
    kind: type
    scopes: frozenset[Scope]
    summary: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    @property
    def group(self) -> str:
        """The leading segment of the dotted key (``limits`` in ``limits.x``)."""
        return self.key.split(".", 1)[0]

    @property
    def leaf(self) -> str:
        """The trailing segment of the dotted key (``x`` in ``limits.x``)."""
        return self.key.split(".", 1)[1]

    def allows(self, scope: Scope) -> bool:
        """Whether this setting may be overridden at *scope*."""
        return scope in self.scopes

    def coerce(self, value: Any) -> Any:
        """Return *value* normalized to this setting's type, or raise ``ValueError``.

        JSON has no integer/float distinction on the way in, so an int is
        accepted for a float setting and widened. ``bool`` is rejected for the
        numeric kinds: Python treats it as an int, and accepting ``true`` where
        a count belongs turns a typo into the value 1.
        """
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValueError("expected true or false")
            return value
        if self.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("expected an integer")
            return self._bounded(value)
        if self.kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("expected a number")
            widened = float(value)
            # Infinity and NaN survive every bound: a minimum rejects neither
            # infinity (which exceeds it) nor NaN (whose comparisons are all
            # false). A hand-edited Infinity would then read as a configured
            # ceiling, and a budget ceiling that is a number no spend can reach
            # is the absence of a ceiling wearing one's clothes. Refused here,
            # where the operator who typed it is the one who hears about it.
            if not isfinite(widened):
                raise ValueError("expected a finite number")
            return self._bounded(widened)
        if not isinstance(value, str):
            raise ValueError("expected a string")
        if self.choices and value not in self.choices:
            raise ValueError("expected one of: " + ", ".join(self.choices))
        return value

    def _bounded(self, value: int | float) -> int | float:
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"must be at least {_number(self.minimum)}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"must be at most {_number(self.maximum)}")
        return value


def _number(value: float) -> str:
    """Format a bound without a trailing ``.0`` on whole numbers."""
    return f"{value:g}"


_REGISTRY: tuple[Setting, ...] = (
    # --- concurrency -------------------------------------------------------
    Setting(
        key="concurrency.global_max_runs",
        default=4,
        kind=int,
        scopes=_APP,
        minimum=1,
        summary="Runs the engine executes at once across every project; arrivals beyond it queue.",
    ),
    Setting(
        key="concurrency.project_max_runs",
        default=2,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Runs one project executes at once; arrivals beyond it queue in arrival order.",
    ),
    Setting(
        key="concurrency.wave_max_tasks",
        default=3,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Leaf tasks the orchestrator dispatches in parallel within one wave.",
    ),
    # --- retry and cycle limits -------------------------------------------
    Setting(
        key="limits.task_retry_limit",
        default=2,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=0,
        summary="Retries for one task before it fails without abandoning independent tasks.",
    ),
    Setting(
        key="limits.revision_cycle_limit",
        default=3,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Authoring revision cycles per review gate before the run is marked needs-human.",
    ),
    Setting(
        key="limits.verify_retry_limit",
        default=2,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=0,
        summary=(
            "Fix-task rounds dispatched for failing verify stages before delivery fails. "
            "Applied per verification point, so a workflow with both pre-submit gates and "
            "a post-submit check can spend this many rounds at each."
        ),
    ),
    # --- timeouts ----------------------------------------------------------
    Setting(
        key="timeouts.authoring_s",
        default=1800,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Wall clock a run may spend authoring before it is marked stalled.",
    ),
    Setting(
        key="timeouts.awaiting_review_s",
        default=604800,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary=(
            "Wall clock a run may wait at a human-reserved gate before it is marked stalled. "
            "Stalled is a notification, never an archival: nothing expires by time."
        ),
    ),
    Setting(
        key="timeouts.executing_s",
        default=7200,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Wall clock a run may spend executing tasks before it is marked stalled.",
    ),
    Setting(
        key="timeouts.delivering_s",
        default=3600,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Wall clock a run may spend in the delivery pipeline before it is marked stalled.",
    ),
    Setting(
        key="timeouts.stage_command_s",
        default=900,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Wall clock one delivery stage command may run before it is killed and fails.",
    ),
    Setting(
        key="timeouts.capability_s",
        default=120,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary="Wall clock a delegated capability call may take before it falls back to builtin.",
    ),
    Setting(
        key="timeouts.analysis_job_s",
        default=900,
        kind=int,
        scopes=_APP_PROJECT,
        minimum=1,
        summary=(
            "Total deadline for an analysis job across every transport. A job without a "
            "deadline is how a trickling provider hangs its caller indefinitely."
        ),
    ),
    Setting(
        key="timeouts.poll_command_s",
        default=120,
        kind=int,
        scopes=_APP_SOURCE,
        minimum=1,
        summary="Wall clock a watch source's poll command may run before the tick is skipped.",
    ),
    # --- budget ------------------------------------------------------------
    Setting(
        key="budget.run_ceiling_credits",
        default=5.0,
        kind=float,
        scopes=_APP_PROJECT,
        minimum=0.01,
        summary=(
            "Credit ceiling for one run. Finite by default so a headless run never executes "
            "unbounded, and independent of any per-source spending cap."
        ),
    ),
    Setting(
        key="budget.warn_fraction",
        default=0.8,
        kind=float,
        scopes=_APP_PROJECT,
        minimum=0.0,
        maximum=1.0,
        summary=(
            "Fraction of the ceiling at which a run notifies without halting. A fraction "
            "rather than an absolute amount so it tracks a changed ceiling."
        ),
    ),
    # --- watch polling -----------------------------------------------------
    Setting(
        key="watch.interval_s",
        default=300,
        kind=int,
        scopes=_APP_SOURCE,
        minimum=30,
        summary="Seconds between watch-source poll ticks. Polling spends no model credits.",
    ),
    # --- delivery posture -------------------------------------------------
    Setting(
        key="delivery.auto_integrate",
        default=False,
        kind=bool,
        scopes=_APP_PROJECT,
        summary=(
            "Whether a run may integrate into the protected destination without human "
            "action. Off by default; integration is the one stage a mistake cannot undo."
        ),
    ),
    Setting(
        key="delivery.review_feedback_enabled",
        default=False,
        kind=bool,
        scopes=_APP_PROJECT,
        summary="Whether the review artifact is polled for comments that dispatch fix tasks.",
    ),
    # --- notification and telemetry ---------------------------------------
    Setting(
        key="notify.channel",
        default="dashboard",
        kind=str,
        scopes=_APP_PROJECT,
        summary=(
            "Host gateway channel notifications route to. Defaults to the dashboard channel, "
            "which every install has."
        ),
    ),
    Setting(
        key="telemetry.enabled",
        default=False,
        kind=bool,
        scopes=_APP,
        summary="Whether the app reports content-free usage telemetry. Off by default.",
    ),
)

#: The setting registry, keyed by dotted key.
SETTINGS: dict[str, Setting] = {s.key: s for s in _REGISTRY}

#: Leading segments of every dotted key. The validated write path accepts these
#: as top-level (and per-project, per-source) containers and nothing else.
SETTING_GROUPS: frozenset[str] = frozenset(s.group for s in _REGISTRY)

#: The same groups in registry declaration order, deduplicated. Separate from
#: :data:`SETTING_GROUPS` because that is a frozenset and set iteration order is
#: neither stable across processes nor meaningful: a surface grouping its rows by
#: it would reorder them between two reads while nothing had changed. Anything
#: that puts groups in front of a reader — or on a wire — orders them by this.
SETTING_GROUP_ORDER: tuple[str, ...] = tuple(dict.fromkeys(s.group for s in _REGISTRY))


def lookup(key: str) -> Setting | None:
    """Return the setting for a dotted *key*, or ``None`` when unknown."""
    return SETTINGS.get(key)


def settings_in_scope(scope: Scope) -> tuple[Setting, ...]:
    """Return every setting overridable at *scope*, in registry order."""
    return tuple(s for s in _REGISTRY if s.allows(scope))


def default_of(key: str) -> Any:
    """Return the bundled default for *key*; raises ``KeyError`` when unknown."""
    return SETTINGS[key].default
