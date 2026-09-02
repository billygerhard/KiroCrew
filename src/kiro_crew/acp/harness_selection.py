"""Which harness a new session runs on, composed from the two config keys.

The registry answers "what harnesses exist" and "does ``agent.default_harness``
name one"; it deliberately does not decide the PRECEDENCE between that key and
the legacy ``agent.acp_backend``, because the surface that reads them knows which
one the operator meant. This module is that decision, in one place, so every
session-creating surface (chat, spawn, cron, task runner) resolves a harness the
same way instead of each composing its own answer.

Four rules, and each exists because its opposite has a silent failure mode:

**An explicit selection is validated and REFUSED, never substituted.** A caller
that names a harness gets that harness or an error naming it. Falling back to the
default would run the user's work on a backend they did not choose and report
success.

**An empty selection resolves without an availability probe.** That path is
today's behaviour for every existing install — an operator with no harness
configuration at all — and it must stay the same: a kiro-cli resolution failure
surfaces from the spawn, where it always has, with the same message. Probing here
would move that failure earlier and change the error a signed-out user sees on an
ordinary new chat.

**The legacy key is read RAW.** ``AgentConfig.acp_backend`` is clamped at load to
a selectable value, so a stored ``"codex"`` reads back as ``""`` and would resolve
to kiro-cli — the alias table would never see the value the operator wrote. The
raw spelling is preserved beside it (``acp_backend_alias``) precisely so the
registry's alias table is the thing that decides what it means.

**A harness with no legacy ``acp_backend`` spelling binds on its descriptor id.**
The capability gates that once read ``acp_backend`` (session sharing, the kiro
identity-store sweep, the cli.json effort and Tool Search overlays) now read the
bound descriptor fail-closed (wave 2), so a harness that declares none of kiro's
capabilities is answered for as itself rather than for kiro-cli. A descriptor with
a legacy spelling keeps it for persistence compatibility; a descriptor without one
— Codex, every operator descriptor — binds with ``acp_backend = descriptor.id``,
which the provider admits as its generic fallback. A serving refusal, when a build
has one, lives in the registry's ``_UNSERVICEABLE`` map and is served to the
selection surfaces through :func:`unserviceable_reason` so a refused row is marked
and unselectable before it is clicked, rather than explaining itself only in the
error card afterwards. That map is EMPTY in the public build after upstream #7301
(Claude Code is now a selectable public backend), so no bundled harness is refused
here today.

Everything here is synchronous and does filesystem work (the registry reads
configuration, availability stats an executable), so an event-loop caller routes
it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.acp.harness_descriptor import HarnessDescriptor
from kiro_crew.acp.harness_registry import DEFAULT_HARNESS, UnknownHarness
from kiro_crew.acp.harness_registry import registry as harness_registry
from kiro_crew.acp.harness_registry import unserviceable_reason as harness_unserviceable_reason
from kiro_crew.acp.types import legacy_backend_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


class HarnessNotServiceable(RuntimeError):
    """A registered, available harness that this build cannot start a session on.

    Distinct from :class:`~kiro_crew.acp.harness_registry.HarnessUnavailable`,
    which describes the MACHINE (a missing binary, a failed spawn) and heals when
    the operator repairs it. This one describes Kiro Crew: the harness itself is
    fine and the build is not ready for it, so the remedy is a different
    selection rather than an install.
    """

    def __init__(self, harness_id: str, reason: str) -> None:
        super().__init__(f"harness {harness_id!r} cannot serve a session: {reason}")
        self.harness_id = harness_id
        self.reason = reason


class HarnessBindingConflict(RuntimeError):
    """An explicit harness that disagrees with the binding a session recorded.

    A recorded binding is not a preference, it is where the conversation IS: the
    resume id names a transcript inside one harness's own store, so issuing
    ``session/load`` for it on a different harness loads nothing and silently
    starts a fresh conversation under an id the session map still trusts — the
    substitution this feature exists to prevent, in its quietest form.

    Both harnesses are named because the remedy depends on which one the caller
    meant: keep this conversation on the harness that holds it, or start a new
    session on the harness they asked for.
    """

    def __init__(self, requested: str, recorded: str) -> None:
        super().__init__(
            f"this session's conversation belongs to harness {recorded!r} and "
            f"cannot be resumed on {requested!r}; start a new session to run on "
            f"{requested!r}"
        )
        self.requested = requested
        self.recorded = recorded


def unserviceable_reason(harness_id: str) -> str:
    """Why THIS BUILD cannot start a session on ``harness_id``, or ``""``.

    Availability answers for the machine and heals when the operator repairs it;
    this answers for Kiro Crew and heals only in a later build. The two are
    independent, which is what makes this worth serving separately: a harness the
    code understands but a build will not serve would resolve, stat, and list as
    available yet be refused at creation, so a selection surface reads this to mark
    such a row unselectable exactly like an unavailable one instead of letting it
    look pickable and explain itself only after the click.

    Wave 2 removed the legacy-spelling gate: a harness whose only "problem" was
    lacking an ``acp_backend`` spelling — Codex, every operator descriptor — now
    serves through the generic adapter and answers ``""`` here. After upstream
    #7301 the ``_UNSERVICEABLE`` map is EMPTY in the public build (Claude Code is a
    selectable public backend), so nothing returns a reason today; the seam remains
    for an edition whose build reintroduces a serving refusal.
    """
    return harness_unserviceable_reason(harness_id)


@dataclass(frozen=True)
class HarnessBinding:
    """The harness a session is bound to, plus the backend spelling it spawns as.

    Both halves travel together because a caller needs both and they must not be
    derived independently: ``harness_id`` is what the session records and every
    surface reports, ``acp_backend`` is the spelling the provider spawns as. A
    harness with a legacy spelling carries it; one without — Codex, every operator
    descriptor — carries its own descriptor id here (the provider's generic
    fallback). Two separate resolutions could disagree — a session labelled with
    one harness running the process of another — which is the failure this whole
    feature exists to make impossible.
    """

    descriptor: HarnessDescriptor
    acp_backend: str

    @property
    def harness_id(self) -> str:
        return self.descriptor.id

    @property
    def display_name(self) -> str:
        return self.descriptor.label


def _config() -> "KiroCrewConfig":
    """The loaded configuration.

    Deferred import: the config loader reaches into the ACP vocabulary, so a
    module-scope import here would close the cycle.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load()


def _legacy_alias_value(cfg: "KiroCrewConfig") -> str:
    """The ``agent.acp_backend`` spelling to hand the registry's alias table.

    Prefers the RAW stored value (``acp_backend_alias``) over the clamped field,
    for the reason the module docstring gives, and reaches both through
    ``getattr`` so a hand-built ``AgentConfig`` from an older test double — one
    that has the field but not the property — still resolves its backend instead
    of raising.
    """
    agent = cfg.agent
    return str(getattr(agent, "acp_backend_alias", getattr(agent, "acp_backend", "")) or "")


def default_harness_id(cfg: "KiroCrewConfig | None" = None) -> str:
    """The harness id a session with no explicit selection starts on.

    Precedence: ``agent.default_harness`` when the operator set one (validated
    against the registry, degrading to kiro-cli with a logged reason if it is
    unusable), else the legacy ``agent.acp_backend`` resolved through the alias
    table. Both keys unset answers kiro-cli.

    ``default_harness`` outranks ``acp_backend`` because it is the newer and more
    specific statement: an operator who set both means the harness key, and an
    operator who set only the legacy key keeps the harness it has always named.

    ONE config object decides the whole answer. Taking the precedence gate from
    the caller's ``cfg`` and the id from the registry's own independent load is how
    the default advertised by ``/api/harnesses`` and the harness a session actually
    binds come to disagree: two reads of the same two keys, resolved a moment
    apart, can name different harnesses while each surface believes it asked one
    question. The registry is still asked whether that id EXISTS and can run —
    a question about the machine, not about configuration — and both surfaces ask
    it the same way.
    """
    reg = harness_registry()
    cfg = cfg if cfg is not None else _config()
    configured = str(getattr(cfg.agent, "default_harness", "") or "").strip()
    if not configured:
        return reg.resolve_alias(_legacy_alias_value(cfg))
    try:
        descriptor = reg.get(configured)
    except UnknownHarness:
        logger.warning(
            "Ignoring agent.default_harness %r (unknown harness); using %r",
            configured,
            DEFAULT_HARNESS,
        )
        return DEFAULT_HARNESS
    available, reason = reg.availability(descriptor.id)
    if available:
        return descriptor.id
    # Degrading here rather than to the legacy key mirrors ``registry.default()``:
    # an operator whose newer statement is unusable gets a working gateway on the
    # default harness, and the reason is logged so the ignored key is diagnosable.
    logger.warning(
        "Ignoring agent.default_harness %r (%s); using %r",
        configured,
        reason,
        DEFAULT_HARNESS,
    )
    return DEFAULT_HARNESS


def pooled_harness_id(cfg: "KiroCrewConfig | None" = None) -> str:
    """The harness an UNBOUND provider runs — what a warm-pool process really is.

    Warm-pool processes are spawned before any session claims them, so nobody
    binds them and they resolve their harness from the legacy alias alone. That
    makes this the only correct comparison for "may this session claim a pooled
    process?": comparing against :func:`default_harness_id` would let a session
    whose default came from ``agent.default_harness`` claim a process spawned on
    the aliased harness instead, which is the silent substitution
    refusal-over-fallback exists to prevent.

    Deliberately NOT the same expression as :func:`default_harness_id`: the two
    differ exactly when ``agent.default_harness`` and ``agent.acp_backend`` name
    different harnesses, and that difference is the bug this function exists to
    make visible rather than average away.
    """
    cfg = cfg if cfg is not None else _config()
    return harness_registry().resolve_alias(_legacy_alias_value(cfg))


def resolve_session_harness(
    harness_id: str = "",
    cfg: "KiroCrewConfig | None" = None,
    *,
    recorded: bool = False,
) -> HarnessBinding:
    """The binding for a session that asked for ``harness_id`` (empty = default).

    An explicit id is required to be registered AND available: the raising
    lineage is the registry's own (``UnknownHarness`` / ``HarnessUnavailable``),
    both of which name the harness, and neither is caught here — a surface that
    swallowed them would be back to silent fallback.

    An empty id takes :func:`default_harness_id` and is NOT availability-checked,
    which is what keeps the no-selection path identical to today's kiro path (see
    the module docstring).

    ``recorded=True`` says the id came from a session's own stored binding rather
    than from a fresh pick — a resume. It is still refused when the harness is
    gone or cannot run, because resuming one harness's transcript on another is
    the substitution this feature exists to prevent, but a RECENT SPAWN FAILURE
    does not gate it: that record describes one attempt and only a successful
    spawn clears it, so honouring it here would refuse every resume for the
    failure window even after the operator signed in.
    """
    reg = harness_registry()
    requested = (harness_id or "").strip()
    if requested:
        descriptor = reg.require_available(requested, honor_recent_failure=not recorded)
    else:
        descriptor = reg.get(default_harness_id(cfg))
    # ``acp_backend`` is the spelling the provider spawns as. A bundled harness
    # with a legacy spelling keeps it (kiro-cli's is the empty string, KAS's is
    # ``"kas"``) for persistence compatibility; a harness WITHOUT one — Codex,
    # every operator descriptor — binds with its own descriptor id, which the
    # provider admits as the generic fallback (``acp_backend == harness_id``,
    # wired in ``AcpProvider.__init__`` at T3). The check is ``is None`` rather
    # than truthiness precisely because kiro-cli's real spelling IS the empty
    # string: ``legacy_backend_for`` returns ``""`` for kiro and ``None`` only
    # for a harness that has no spelling at all, and collapsing the two would
    # give kiro-cli ``acp_backend = "kiro-cli"`` and refuse its own construction.
    #
    # A harness the BUILD genuinely cannot serve would sit in the registry's
    # ``_UNSERVICEABLE`` map, so ``require_available`` would refuse it
    # (``HarnessUnavailable``) before resolution reached here. That map is EMPTY in
    # the public build after upstream #7301 (Claude Code is now a selectable public
    # backend), so no bundled harness is refused at this gate today.
    # ``HarnessNotServiceable`` is therefore not raised from resolution; the class
    # remains for the surfaces that still catch it (cron, subagent, chat) and for a
    # future edition whose build reintroduces a descriptor-level serving refusal.
    backend = legacy_backend_for(descriptor.id)
    acp_backend = backend if backend is not None else descriptor.id
    return HarnessBinding(descriptor=descriptor, acp_backend=acp_backend)
