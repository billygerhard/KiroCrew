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
"""

from __future__ import annotations

import sys
from typing import Dict, Iterable

#: Registered id -> the builtin backend id whose capability memberships it takes.
_capabilities_from: Dict[str, str] = {}

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
        source = _capabilities_from.get(item) if isinstance(item, str) else None
        return source is not None and frozenset.__contains__(self, source)


def record_capabilities_from(backend_id: str, source: str) -> None:
    """Make *backend_id* answer every inheritable set as *source* does."""
    _capabilities_from[backend_id] = source


def capability_source(backend: str) -> str:
    """The builtin *backend* takes its capabilities from, or *backend* itself."""
    return _capabilities_from.get(backend, backend)


def inherits_capabilities(backend: str) -> bool:
    """Whether *backend* is a registered id that declared a capability source."""
    return backend in _capabilities_from


def forget_capabilities_from(backend_id: str) -> None:
    """Drop *backend_id*'s link (registration rollback and tests)."""
    _capabilities_from.pop(backend_id, None)


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


def _reset_capabilities_from() -> None:
    """TEST-ONLY: forget every link."""
    _capabilities_from.clear()


__all__ = [
    "NOT_INHERITED",
    "InheritingSet",
    "capability_source",
    "forget_capabilities_from",
    "inherits_capabilities",
    "make_inheritable",
    "record_capabilities_from",
]
