"""Per-app tool-approval grants — the posture applied to sessions an app seeds.

An app that does unattended work (a watcher, a planner, a headless run driver)
needs its sessions to call tools without a human to answer the approval prompt.
Cron solved this once with ``approval_mode`` (``""`` hook-based | ``"auto"``
auto-approve) flowing from a job row into ``sessions.get_or_create``. This module
generalizes that same shape to apps, keeping the two-key structure that makes it
safe:

1. **The app DECLARES a wanted posture** in its manifest
   (``permissions.approvalMode``). A declaration is a request and grants nothing.
2. **The operator GRANTS it** in ``<data home>/app_approval_grants.json``. The
   grant file is a trust root: it lives on the keystone floor
   (``security._SENSITIVE_HOME_DIRS``), so an agent can neither read nor write
   it, and it is never sourced from the app being granted.

The applied posture is the INTERSECTION — ``"auto"`` only when the app asked for
it and the operator granted it. Everything else resolves to the default
hook-based posture. There is deliberately no setter, no MCP tool, and no
env-var, request-header, or config path an app or a running session can use to
raise its own posture: the only inputs are the manifest on disk and the operator's
grant file.

``KIROCREW_APPROVAL_MODE`` is a RESERVED control variable for the same reason it
is reserved on the cron path: the spawned ``kiro-cli`` process inherits it and
``spawn_run`` forwards it, so an app-authored ``env`` block carrying it would be
a self-grant. :func:`sanitize_app_env` strips it from every app-controlled env
map, and :func:`posture_extra_env` is the only re-injection point.

Verification closes the loop. :func:`verify_applied_posture` is called before a
run starts and :func:`posture_exceeds_grant` re-checks an already-scheduled job
at fire time, so a posture that was elevated after the grant was resolved — an
edited job row, a tampered manifest snapshot, a caller passing its own value —
refuses the run instead of running elevated.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from kiro_crew.config.loader import config_dir
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Hook-based approval: every tool call goes through the normal approval ladder.
POSTURE_DEFAULT = ""
#: Auto-approve: tool calls in the seeded session are approved without a prompt.
POSTURE_AUTO = "auto"
#: The whole posture vocabulary, ordered from least to most permissive. Same
#: two values the cron ``approval_mode`` grant has always carried, so a job row,
#: a session ``approval_policy`` and an app grant all speak one language.
VALID_POSTURES: tuple[str, ...] = (POSTURE_DEFAULT, POSTURE_AUTO)

#: Env var the host injects so a seeded session's ``spawn_run`` subagents inherit
#: the posture without depending on parent-session resolution. RESERVED: never
#: honored from an app-controlled env map.
RESERVED_APPROVAL_ENV = "KIROCREW_APPROVAL_MODE"

#: Where the operator drops the grant file (keystone floor; see module docstring).
GRANTS_FILENAME = "app_approval_grants.json"

#: Prefix that marks a cron job / session as owned by an app.
_OWNER_PREFIX = "app:"


def _normalize_name(name: str) -> str:
    """Canonical app-name form for grant lookups.

    NFKC + casefold + strip, so a grant for ``"spec-engine"`` is not matched by a
    lookalike (``"Spec-Engine "``) and cannot be evaded by one either. Mirrors
    ``apps.admission._normalize_name`` — both compare an app-supplied name
    against an operator-authored list.
    """
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _coerce_posture(value: object) -> str:
    """Coerce an untrusted JSON value to a posture, failing toward the default.

    Only the literal string ``"auto"`` yields the permissive posture. Every other
    value — a non-string, a mixed-case variant, an unknown word — resolves to
    :data:`POSTURE_DEFAULT`, because this coercion sits on both sides of a
    capability GRANT and a malformed grant must withhold rather than confer
    (same direction as ``Permissions.from_dict``).
    """
    if isinstance(value, str) and value == POSTURE_AUTO:
        return POSTURE_AUTO
    return POSTURE_DEFAULT


@dataclass(frozen=True)
class AppApprovalGrants:
    """The operator-authored grant table. Never sourced from an app."""

    #: normalized app name -> granted posture. Absent app = no grant.
    postures: Mapping[str, str] = field(default_factory=dict)

    @staticmethod
    def none_granted() -> "AppApprovalGrants":
        """The no-file default: nothing granted, every app on the default posture."""
        return AppApprovalGrants(postures={})

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "AppApprovalGrants":
        raw = data.get("grants")
        if not isinstance(raw, Mapping):
            return AppApprovalGrants.none_granted()
        postures: Dict[str, str] = {}
        for name, value in raw.items():
            app = _normalize_name(str(name))
            if not app:
                continue
            posture = _coerce_posture(value)
            if posture != POSTURE_DEFAULT:
                postures[app] = posture
        return AppApprovalGrants(postures=postures)

    def posture_for(self, app: str) -> str:
        """Granted posture for *app*, or :data:`POSTURE_DEFAULT` when ungranted."""
        return self.postures.get(_normalize_name(app), POSTURE_DEFAULT)


def load_app_approval_grants() -> AppApprovalGrants:
    """Load the grant table from ``config_dir()/app_approval_grants.json``.

    Both failure modes withhold rather than confer: an ABSENT file grants
    nothing (the shipped default — no app runs unattended until an operator says
    so), and a PRESENT-but-unreadable file also grants nothing, audited as a
    critical SEL event so a corrupt trust root is visible rather than silently
    equivalent to "no grants configured".
    """
    path = config_dir() / GRANTS_FILENAME
    if not path.exists():
        return AppApprovalGrants.none_granted()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{GRANTS_FILENAME} must be a JSON object")
    except Exception as exc:
        logger.error("app approval grants at %s are unreadable (%s); granting nothing", path, exc)
        _audit(
            operation="grants_load",
            outcome="failed",
            app="",
            detail="unreadable_grants_no_posture_granted",
            critical=True,
        )
        return AppApprovalGrants.none_granted()
    return AppApprovalGrants.from_dict(data)


def granted_posture(app: str) -> str:
    """Posture the operator granted *app*, independent of what the app asked for."""
    if not app:
        return POSTURE_DEFAULT
    return load_app_approval_grants().posture_for(app)


def wanted_posture(permissions: Mapping[str, Any] | None) -> str:
    """Posture an app's manifest ``permissions`` DECLARES it wants.

    A declaration is a request, not a grant: this value only ever narrows the
    operator's grant in :func:`effective_posture`. Coerced strictly, so a
    manifest cannot express a posture this host does not model.
    """
    if not permissions:
        return POSTURE_DEFAULT
    return _coerce_posture(permissions.get("approvalMode"))


def effective_posture(app: str, wanted: str) -> str:
    """The posture the gateway applies to a session *app* seeds.

    ``granted ∩ wanted``: the permissive posture requires BOTH the app's manifest
    declaration and the operator's grant. A declaration without a grant is
    audited as denied — it is the case an operator most needs to see, because the
    app's unattended work will stall on approvals until they grant it.
    """
    wanted = _coerce_posture(wanted)
    granted = granted_posture(app)
    if wanted == POSTURE_DEFAULT:
        return POSTURE_DEFAULT
    if granted != wanted:
        _audit(
            operation="posture_resolve",
            outcome="denied",
            app=app,
            detail=f"wanted={wanted} granted={granted or 'none'}",
        )
        return POSTURE_DEFAULT
    _audit(operation="posture_resolve", outcome="allowed", app=app, detail=f"posture={wanted}")
    return wanted


def verify_applied_posture(app: str, wanted: str, applied: str) -> Optional[str]:
    """Reason to refuse or halt a run, or ``None`` when *applied* is correct.

    Called before a seeded run starts (and again to halt one already started).
    Both directions of mismatch refuse:

    - ``applied`` more permissive than the grant is an ELEVATION — the case the
      whole mechanism exists to prevent, audited critical.
    - ``applied`` more restrictive is a configuration fault. It refuses too,
      because an unattended run on the hook-based posture does not fail: it
      blocks on an approval prompt nobody will ever answer, which is a silent
      stall instead of a reported refusal.
    """
    applied = _coerce_posture(applied)
    expected = effective_posture(app, wanted)
    if applied == expected:
        return None
    elevated = applied == POSTURE_AUTO
    reason = (
        f"approval posture {applied or 'default'!r} does not match the posture "
        f"{expected or 'default'!r} granted to app {app!r} in configuration"
    )
    _audit(
        operation="posture_verify",
        outcome="denied",
        app=app,
        detail=f"applied={applied or 'default'} expected={expected or 'default'}",
        critical=elevated,
    )
    return reason


def posture_exceeds_grant(app: str, applied: str) -> Optional[str]:
    """Reason to refuse when *applied* is MORE permissive than *app*'s grant.

    The ceiling-only half of :func:`verify_applied_posture`, for re-checking work
    that was authorized earlier and persisted since — a scheduled job row, a
    resumed run — where the app's own declaration is not at hand and only the
    operator's grant is authoritative. Catches every path that could raise a
    stored posture after the fact (a hand-edited row, an update call carrying its
    own value) without refusing a legitimately narrowed one.
    """
    applied = _coerce_posture(applied)
    if applied == POSTURE_DEFAULT:
        return None
    granted = granted_posture(app)
    if applied == granted:
        return None
    _audit(
        operation="posture_ceiling",
        outcome="denied",
        app=app,
        detail=f"applied={applied} granted={granted or 'none'}",
        critical=True,
    )
    return (
        f"approval posture {applied!r} exceeds the posture "
        f"{granted or 'default'!r} granted to app {app!r} in configuration"
    )


def declared_posture(app: str) -> str:
    """The posture *app*'s AUTHORITATIVE manifest declares it wants.

    Resolved the same way executable-resource registration resolves a manifest: a
    shipped builtin is read from its immutable package root, so mutable installed
    metadata cannot borrow a builtin's name to declare an unattended posture the
    shipped app never asked for. An unreadable or absent manifest declares
    nothing.
    """
    if not app:
        return POSTURE_DEFAULT
    # Deferred imports: the app platform package imports this module through the
    # cron SDK, so a module-level import here would close a cycle.
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manager import get_app_manifest
    from kiro_crew.apps.manifest import AppManifest

    manifest: Any = None
    shipped_root = shipped_builtin_app_root(app)
    if shipped_root is not None:
        try:
            manifest = AppManifest.from_json_file(shipped_root / "app.json")
        except Exception as exc:  # noqa: BLE001 — unreadable manifest declares nothing
            logger.warning(
                "App %s: shipped manifest unreadable (%s); no posture declared", app, exc
            )
            return POSTURE_DEFAULT
    else:
        manifest = get_app_manifest(app)
    if manifest is None:
        return POSTURE_DEFAULT
    return wanted_posture(manifest.permissions.to_dict())


def session_posture(app: str) -> str:
    """The posture the gateway applies to a session *app* seeds.

    The one call a seeding path needs: it resolves the app's declaration itself
    and intersects it with the operator's grant, so a caller cannot pass a
    ``wanted`` value of its own and cannot widen the result.
    """
    return effective_posture(app, declared_posture(app))


def verify_session_posture(app: str, applied: str) -> Optional[str]:
    """Reason to refuse or halt *app*'s seeded run, or ``None`` when correct.

    The verification counterpart of :func:`session_posture`, resolving the
    declaration from the manifest rather than trusting a caller-supplied value.
    """
    return verify_applied_posture(app, declared_posture(app), applied)


def clamp_posture(app: str, requested: str) -> str:
    """Narrow a caller-supplied posture to what *app* was granted.

    For SDK entry points an app itself calls: the app may ask, and the grant
    decides. Returns the requested posture only when the operator granted it,
    :data:`POSTURE_DEFAULT` otherwise, so an SDK caller can never widen its own
    posture by passing an argument.
    """
    requested = _coerce_posture(requested)
    if requested == POSTURE_DEFAULT:
        return POSTURE_DEFAULT
    if granted_posture(app) == requested:
        return requested
    _audit(
        operation="posture_clamp",
        outcome="denied",
        app=app,
        detail=f"requested={requested} granted=none",
    )
    return POSTURE_DEFAULT


def app_from_owner(created_by: str) -> str:
    """App name behind an ``app:<name>`` owner tag, or ``""`` when not app-owned.

    Cron rows carry either an app owner tag or a human creator id in the same
    field, so the prefix is what distinguishes an app-seeded job from a
    user-authored one.
    """
    if not created_by.startswith(_OWNER_PREFIX):
        return ""
    return created_by[len(_OWNER_PREFIX) :].strip()


def sanitize_app_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Copy *env* with the reserved approval control var removed.

    Applied to every app-controlled env map before it is stored or spawned with.
    Without this an app could set ``KIROCREW_APPROVAL_MODE`` in its own manifest
    env block and have its subagents auto-approved with no grant at all.
    """
    return {k: v for k, v in (env or {}).items() if k != RESERVED_APPROVAL_ENV}


def posture_extra_env(posture: str, env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Sanitized *env* plus the reserved control var when *posture* is auto.

    The single re-injection point for :data:`RESERVED_APPROVAL_ENV`. ``None`` when
    the result would be empty, matching what ``get_or_create(extra_env=...)``
    treats as "nothing to add".
    """
    out = sanitize_app_env(env)
    if _coerce_posture(posture) == POSTURE_AUTO:
        out[RESERVED_APPROVAL_ENV] = POSTURE_AUTO
    return out or None


def _audit(
    *,
    operation: str,
    outcome: str,
    app: str,
    detail: str = "",
    critical: bool = False,
) -> None:
    """Best-effort SEL record for a posture decision (allowed and denied alike).

    Never raises: an audit failure must not turn a refusal into a crash, nor a
    grant resolution into a failed run.
    """
    try:
        sel().log_api_access(
            caller=f"{_OWNER_PREFIX}{app}" if app else "app_approval_grants",
            operation=f"app_approval.{operation}",
            outcome=outcome,
            source="apps",
            resources=f"app={app or 'unknown'} {detail}".strip(),
            critical=critical,
        )
    except Exception:  # noqa: BLE001 — auditing must never break a decision
        logger.debug("app approval posture audit emit failed", exc_info=True)
