"""Capability inheritance: a ``harnesses.json`` backend that speaks a builtin's protocol.

A Claude seat is claude-agent-acp launched through a wrapper that changes only
``CLAUDE_CONFIG_DIR``. Its id (``claude-seat1``) is a member of no frozen
capability set, so every gate and the Settings card answered as if it were an
unknown host, though it is the same adapter as ``claude``. A descriptor's
``capabilities_from`` names the builtin it speaks for; this module records that
link and makes the capability sets answer for it.

What is inherited is MEMBERSHIP only: the descriptor's own executable, argv, env,
routing and model source stay its own. Sets that describe how a builtin is
LAUNCHED, how it SIGNS IN, or a confinement only the builtin's own driver applies
are listed in :data:`NOT_INHERITED` and keep answering by id.

The sets are module constants imported by value all over the tree, so they cannot
be rebound after import. Instead each inheritable set is replaced, once, while
:mod:`kiro_crew.agent_sdk.backends` is still loading, by an
:class:`InheritingSet`: a ``frozenset`` with the same members whose ``in`` also
answers for an id that inherits from a member. Iteration, equality and set algebra
are untouched, so every reader that enumerates a set still sees builtins only.

A descriptor that is NOT a builtin's clone (Antigravity, a Strands harness) names
the capabilities it was measured to support instead, in ``capabilities``: each name
in :data:`DESCRIPTOR_CAPABILITIES` admits the id to the runtime set that gates that
behaviour, and the card reads the same set. Side Chat read-only tools are not a name
a descriptor may claim: the lock-down that makes them safe is built only by the
builtins' own drivers.
"""

from __future__ import annotations

import sys
from typing import Dict, FrozenSet, Iterable, Set, Tuple

#: Registered id -> the builtin backend id whose capability memberships it takes.
_capabilities_from: Dict[str, str] = {}

#: A capability name a descriptor may declare -> the runtime sets it joins. Each set
#: is the one the runtime gate AND the Settings card read for that behaviour.
DESCRIPTOR_CAPABILITIES: Dict[str, Tuple[str, ...]] = {
    # Crew's MCP servers on session/new (the card's "Kiro Crew tools" line).
    "crew_tools": ("ACP_BACKENDS_SESSION_MCP_ARRAY",),
    # The member session-control server appended to a mate thread's array.
    "member_tools": ("ACP_BACKENDS_MEMBER_DISPATCH",),
    # A mid-turn message on ``_session/steering``.
    "steer": ("ACP_BACKENDS_STEERING_REQUEST",),
    # ``/compact`` sent as a prompt the harness compacts on.
    "compact": ("ACP_BACKENDS_COMPACT",),
    # Effort pushed as an ``effort`` select on ``session/set_config_option``.
    "effort": ("ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION",),
    # Model switched as a ``model`` select on ``session/set_config_option``.
    "model_switch": ("ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION",),
}

#: Names a descriptor may not claim, with the reason a validation error states.
REFUSED_DESCRIPTOR_CAPABILITIES: Dict[str, str] = {
    "side_chat_tools": (
        "Side Chat read-only tools need a read-only lock-down that only the builtin "
        "harnesses' own drivers build; a harnesses.json launch path has none"
    ),
}

#: ``id(InheritingSet)`` -> registered ids that declared that set's capability.
_declared_members: Dict[int, Set[str]] = {}
#: Set name -> its InheritingSet, filled by :func:`make_inheritable`.
_sets_by_name: Dict[str, "InheritingSet"] = {}

#: Sets that are NOT inherited, each for a reason that is about the builtin's own
#: launch, sign-in or driver rather than the protocol its adapter speaks.
NOT_INHERITED: Dict[str, str] = {
    "ACP_BACKENDS_KNOWN": "identity: which ids this build can spell",
    "ACP_BACKENDS_BUILTIN": "identity: which ids ship a harness class",
    "ACP_BACKENDS_KIRO_FAMILY": "identity: the kiro-cli/KAS family",
    "ACP_BACKENDS_ACP_CLIENT_SPAWNABLE": "launch: a descriptor runs on AcpRuntime",
    "ACP_BACKENDS_ACP_RUNTIME": "launch: registered ids join through acp_runtime_backends",
    "ACP_BACKENDS_SELF_SERVED_ACP": "launch: the descriptor's argv is its launch",
    "ACP_BACKENDS_INDEPENDENT_SETUP": "install: first-run setup of the builtin's binary",
    "ACP_BACKENDS_HOST_AUTH_CALLBACK": "sign-in: a host credential handed to the child",
    "ACP_BACKENDS_POD_HOME_REMAP": "sign-in: where the builtin's credential store lives",
    "ACP_BACKENDS_INTERNAL_SANDBOX": "security: the builtin's own OS sandbox",
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS": "driver: AcpClient writes the builtin's config home",
    "ACP_BACKENDS_SIDE_READONLY": (
        "security: claude's Side Chat confinement (read built-ins only, deny rules, "
        "no hooks) is built by AcpClient, which a descriptor never runs on"
    ),
}


class InheritingSet(frozenset):  # type: ignore[type-arg]
    """A capability set whose ``in`` also admits an id inheriting from a member."""

    __slots__ = ()

    def __contains__(self, item: object) -> bool:
        if frozenset.__contains__(self, item):
            return True
        declared = _declared_members.get(id(self))
        if declared and item in declared:
            return True
        source = _capabilities_from.get(item) if isinstance(item, str) else None
        return source is not None and frozenset.__contains__(self, source)


def record_capabilities_from(backend_id: str, source: str) -> None:
    """Make *backend_id* answer every inheritable set as *source* does."""
    _capabilities_from[backend_id] = source


def record_declared_capabilities(backend_id: str, names: Iterable[str]) -> None:
    """Admit *backend_id* to the runtime set behind each declared capability name.

    Raises ``KeyError`` for a name outside :data:`DESCRIPTOR_CAPABILITIES`;
    the descriptor parser has already refused one, so this is a programming error.
    """
    targets = [
        _sets_by_name[set_name] for name in names for set_name in DESCRIPTOR_CAPABILITIES[name]
    ]
    for target in targets:
        _declared_members.setdefault(id(target), set()).add(backend_id)


def declared_capabilities(backend: str) -> FrozenSet[str]:
    """The capability names *backend* holds by declaration (not by inheritance)."""
    return frozenset(
        name
        for name, set_names in DESCRIPTOR_CAPABILITIES.items()
        if all(backend in _declared_members.get(id(_sets_by_name[n]), ()) for n in set_names)
    )


def capability_source(backend: str) -> str:
    """The builtin *backend* takes its capabilities from, or *backend* itself."""
    return _capabilities_from.get(backend, backend)


def inherits_capabilities(backend: str) -> bool:
    """Whether *backend* is a registered id that declared a capability source."""
    return backend in _capabilities_from


def forget_capabilities_from(backend_id: str) -> None:
    """Drop *backend_id*'s link and declarations (registration rollback and tests)."""
    _capabilities_from.pop(backend_id, None)
    for members in _declared_members.values():
        members.discard(backend_id)


def make_inheritable(module_names: Iterable[str]) -> None:
    """Replace each inheritable ``ACP_BACKENDS_*`` frozenset in *module_names*.

    Run once, from the vocabulary module, after its owners have loaded and before
    any other module imports a set from them. A set re-exported under the same name
    by several of those modules is replaced by ONE object everywhere.
    """
    replaced: Dict[int, InheritingSet] = {}
    for module_name in module_names:
        module = sys.modules[module_name]
        for name, value in list(vars(module).items()):
            if (
                name.startswith("ACP_BACKENDS_")
                and name not in NOT_INHERITED
                and type(value) is frozenset
            ):
                wrapped = replaced.setdefault(id(value), InheritingSet(value))
                setattr(module, name, wrapped)
                _sets_by_name[name] = wrapped


def _reset_capabilities_from() -> None:
    """TEST-ONLY: forget every link and declaration."""
    _capabilities_from.clear()
    _declared_members.clear()


__all__ = [
    "DESCRIPTOR_CAPABILITIES",
    "NOT_INHERITED",
    "REFUSED_DESCRIPTOR_CAPABILITIES",
    "declared_capabilities",
    "record_declared_capabilities",
    "InheritingSet",
    "capability_source",
    "forget_capabilities_from",
    "inherits_capabilities",
    "make_inheritable",
    "record_capabilities_from",
]
