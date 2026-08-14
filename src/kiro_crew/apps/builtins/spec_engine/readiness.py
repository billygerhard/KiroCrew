"""Whether this app's declared resources actually reached Host_Agent sessions.

Registration is best-effort per resource: the host logs a warning and carries on
when a declared skill directory cannot be linked or an MCP server cannot be
written, so an app whose tools never arrived is otherwise indistinguishable from
a whole one. That silence is the problem this module exists to end. The first
symptom a user meets is then a spec operation whose tools are missing, with
nothing connecting that to the failed registration.

So the app reports a state instead of a log line:

* The state is **recorded**, in the app's own data directory, and read back by
  whatever surface asks. A caller can act on it.
* Its default is **not ready**. An app whose readiness was never assessed does
  not get to look operational by omission, which is the only default that keeps
  a surface from claiming health it never checked.
* Installation and enablement still **succeed**. This is not an exception that
  aborts a lifecycle operation; it is the state that operation finishes with.

What counts as ready is derived from ``app.json``, never restated here: the
manifest is the single declaration of which skill and which server have to
arrive, so dropping one from the manifest cannot quietly turn a broken install
into a passing one. A manifest that declares neither is itself not ready.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Iterable

logger = logging.getLogger(__name__)

#: The app manifest, beside this module in the app root.
APP_MANIFEST_PATH = Path(__file__).with_name("app.json")

#: The recorded state, relative to the app's data directory.
STATUS_FILENAME = "readiness.json"

#: Reported when nothing has assessed this app yet. Not an error state — the
#: honest description of "no one has checked", which is still not operational.
NOT_ASSESSED = "registration readiness has not been assessed"


@dataclass(frozen=True)
class Readiness:
    """Whether the app may present itself as operational, and why not."""

    ready: bool
    reasons: tuple[str, ...] = ()
    checked_at: str = ""

    @property
    def operational(self) -> bool:
        """Whether a surface may present this app as working.

        The same predicate as :attr:`ready`, named for the question a surface
        actually asks, so no surface has to decide for itself that "ready with
        reasons" might be good enough.
        """
        return self.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "operational": self.operational,
            "reasons": list(self.reasons),
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> Readiness:
        """Rebuild a recorded state, treating anything unreadable as not ready.

        A truncated or hand-edited status file must not read back as ready: the
        one thing worse than a missing assessment is a corrupt one that passes.
        """
        if not isinstance(data, dict):
            return not_assessed()
        reasons = tuple(str(r) for r in data.get("reasons", ()) if isinstance(r, (str, int)))
        if data.get("ready") is not True:
            return cls(
                ready=False,
                reasons=reasons or (NOT_ASSESSED,),
                checked_at=str(data.get("checked_at", "")),
            )
        if reasons:
            # ready=True carrying reasons is a contradiction; trust the reasons.
            return cls(ready=False, reasons=reasons, checked_at=str(data.get("checked_at", "")))
        return cls(ready=True, checked_at=str(data.get("checked_at", "")))


def not_assessed() -> Readiness:
    """The fail-closed state: nothing has checked, so nothing is operational."""
    return Readiness(ready=False, reasons=(NOT_ASSESSED,))


@dataclass(frozen=True)
class RequiredResources:
    """The resources the manifest says have to reach a session."""

    app_name: str = ""
    #: Skill directory names, as the host registers them.
    skills: tuple[str, ...] = ()
    #: MCP server names, unnamespaced as the manifest declares them.
    servers: tuple[str, ...] = ()
    #: Reasons the declaration itself is unusable (unreadable, or declaring
    #: nothing to register). Carried rather than raised so a broken manifest
    #: reports a not-ready state like every other failure.
    problems: tuple[str, ...] = field(default_factory=tuple)

    def namespaced_skill(self, skill: str) -> str:
        return f"{self.app_name}/{skill}"

    def namespaced_server(self, server: str) -> str:
        return f"{self.app_name}:{server}"


def read_manifest(path: Path | None = None) -> dict[str, Any]:
    """Return the parsed manifest, or ``{}`` when it cannot be read."""
    try:
        data = json.loads((path or APP_MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("spec-engine: app manifest unreadable: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def required_resources(manifest: dict[str, Any] | None = None) -> RequiredResources:
    """Derive what has to register from the manifest's own declarations."""
    data = read_manifest() if manifest is None else manifest
    problems: list[str] = []
    app_name = str(data.get("name") or "")
    if not app_name:
        problems.append("the app manifest could not be read, or declares no app name")

    skills = tuple(Path(str(p)).name for p in data.get("skills") or () if str(p).strip())
    if not skills:
        problems.append("the app manifest declares no discovery skill to register")

    servers_raw = data.get("mcpServers")
    servers = tuple(str(s) for s in servers_raw) if isinstance(servers_raw, dict) else ()
    if not servers:
        problems.append("the app manifest declares no MCP server to register")

    return RequiredResources(
        app_name=app_name,
        skills=skills,
        servers=servers,
        problems=tuple(problems),
    )


def assess(
    *,
    present_skills: Collection[str],
    present_servers: Collection[str],
    errors: Iterable[str] = (),
    required: RequiredResources | None = None,
) -> Readiness:
    """Judge one observation of what registered against what was declared.

    *present_skills* and *present_servers* are the plain names observed to have
    landed; *errors* are failures the registrar itself reported. A partial miss
    counts: a registered server does not compensate for a missing skill, since
    an agent that can call the tools but never sees the skill has no reason to.
    """
    req = required_resources() if required is None else required
    reasons: list[str] = [str(e) for e in errors if str(e).strip()]
    reasons.extend(req.problems)

    missing_skills = [s for s in req.skills if s not in set(present_skills)]
    if missing_skills:
        reasons.append(
            "the discovery skill did not register, so no session will be told to "
            "use this app: " + ", ".join(req.namespaced_skill(s) for s in missing_skills)
        )
    missing_servers = [s for s in req.servers if s not in set(present_servers)]
    if missing_servers:
        reasons.append(
            "the engine MCP server did not register, so its tools are missing from "
            "every session: " + ", ".join(req.namespaced_server(s) for s in missing_servers)
        )
    return Readiness(
        ready=not reasons,
        reasons=tuple(reasons),
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def observe(required: RequiredResources | None = None) -> tuple[set[str], set[str]]:
    """Observe which declared resources are registered on this host RIGHT NOW.

    Reads the two places registration actually writes — the app's skills
    directory and the host's MCP server map — rather than the return value of a
    registration call. The builtin and installed paths converge on the same two
    destinations, so one observation covers both and neither can pass while the
    other is broken.
    """
    req = required_resources() if required is None else required
    # Imported lazily: this module is reachable from the app package, which the
    # host's app manager imports, so a module-level import of the bridges would
    # close an import cycle.
    from kiro_crew.apps.bridges import app_skills_dir, registered_app_mcp_servers

    skills: set[str] = set()
    if req.app_name:
        skills_root = app_skills_dir(req.app_name)
        for skill in req.skills:
            candidate = skills_root / skill
            # A symlink is what registration creates, and `exists()` follows it,
            # so a link whose target went away is correctly NOT present.
            if candidate.exists():
                skills.add(skill)

    registered = registered_app_mcp_servers()
    servers = {s for s in req.servers if req.namespaced_server(s) in registered}
    return skills, servers


def status_path(data_dir: Path) -> Path:
    return data_dir / STATUS_FILENAME


def record(readiness: Readiness, data_dir: Path) -> None:
    """Persist the assessed state where a surface can read it back."""
    path = status_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(readiness.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # The state could not be recorded, so nothing can read it. Loud, because
        # a surface will now report NOT_ASSESSED and the reason lives only here.
        logger.warning("spec-engine: readiness state could not be recorded: %s", exc)


def current(data_dir: Path) -> Readiness:
    """Read the recorded state; absence and corruption both read as not ready."""
    try:
        raw = json.loads(status_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return not_assessed()
    return Readiness.from_dict(raw)


def on_startup(ctx: Any) -> None:
    """Assess and record readiness when the app starts or is enabled.

    Declared as ``backend.hooks.on_startup``, which the host invokes for an
    enabled app at gateway startup and again when the app is enabled — after
    registration has run in both cases, so what this observes is the outcome.

    Never raises. A hook that raised would leave no recorded state at all, and
    the app would report "not assessed" instead of the specific failure the
    operator needs.
    """
    data_dir = Path(getattr(ctx, "data_dir", "."))
    try:
        req = required_resources()
        skills, servers = observe(req)
        readiness = assess(present_skills=skills, present_servers=servers, required=req)
    except Exception as exc:  # noqa: BLE001 - a failed check is a not-ready state
        readiness = Readiness(
            ready=False,
            reasons=(f"registration could not be verified: {exc}",),
            checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
    record(readiness, data_dir)
    if readiness.ready:
        logger.info("spec-engine: registration verified, app is operational")
        return
    health = getattr(ctx, "health", None)
    reason_text = "; ".join(readiness.reasons)
    if health is not None and hasattr(health, "mark_error"):
        health.mark_error(f"spec-engine is not ready: {reason_text}")
    logger.warning("spec-engine: NOT READY — %s", reason_text)
