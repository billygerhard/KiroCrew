"""Backend ids an operator declares in ``harnesses.json``, made known at boot.

The builtin ids are frozen literals in :mod:`kiro_crew.agent_sdk.backends`. A
config-authored id cannot be one of them, so this module is where such an id is
written into the tables every "for every known backend" reader consults, as DATA
rather than as a module-level ``ACP_BACKEND*`` constant:

* the id joins ``ACP_BACKENDS_KNOWN`` (a live view, so earlier imports see it);
* its routing joins ``ACP_BACKEND_ROUTING`` (and ``ACP_BACKEND_PERMISSION_CONFIG``
  for ``session_config``), so ``routing_for`` answers a real mechanism;
* its policy id joins ``POLICY_ID_BY_BACKEND`` (self-named), so a governance rule,
  ``spawn_run(backend=...)`` and ``session_set_backend`` can name it;
* its provider label, model namespace and session family are recorded here.

A registered id is a member of NO frozen capability set, so it takes the default
posture at every gate (harness-parity H6/H7). The few derived answers that must
include registered ids -- the shared-runtime start path, the advertised model
vocabulary, the config-option model switch -- are functions below rather than
sets, because a set is vocabulary and these are not.

Registration is boot-once (:mod:`kiro_crew.operator_backends`) and never undone in a
running gateway; :func:`_reset_registered_backends` exists for tests.
"""

from __future__ import annotations

import re
from typing import Any, FrozenSet, Optional, Tuple

from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_KNOWN,
    POLICY_ID_BY_BACKEND,
    _registered_known,
)
from kiro_crew.agent_sdk.backends_routing import (
    ACP_BACKEND_PERMISSION_CONFIG,
    ACP_BACKEND_ROUTING,
    Routing,
)
from kiro_crew.agent_sdk.backends_sessions import ACP_BACKENDS_ACP_RUNTIME

#: Registered ids served by ``AcpRuntime``. Every descriptor is: ``DescriptorHarness``
#: exists only on the shared-runtime path. Kept apart from ``_registered_known``
#: because "this build can spell the id" and "a session for it takes the
#: shared-runtime start path" are separate questions.
_registered_runtime: set[str] = set()

#: Provider label per registered id. The builtins keep their ``PROVIDER_LABEL_*``
#: constants in ``acp/types.py``.
_REGISTERED_PROVIDER_LABELS: dict[str, str] = {}

#: Model-registry namespace per registered id (default: the id itself), so a
#: registered harness's advertised ids never share kiro's ``acp`` bucket.
_REGISTERED_MODEL_NAMESPACE: dict[str, str] = {}

#: Provider label -> session family. Keyed on the LABEL because the label is what
#: the session map persists and ``detect_provider_switch`` compares.
_REGISTERED_SESSION_FAMILY: dict[str, str] = {}

#: Registered ids whose descriptor declares ``mcp_delivery: "session_array"``: the
#: host learns its MCP servers only from the ``session/new`` array, so Crew builds it.
_registered_session_array: set[str] = set()

#: Registered id -> declared model source and static list.
_REGISTERED_MODEL_SOURCE: dict[str, Tuple[str, Tuple[str, ...]]] = {}

#: The model source a descriptor declares when its harness lists its own models.
MODEL_SOURCE_ACP_ADVERTISED = "acp_advertised"

_REGISTERABLE_BACKEND_ID = re.compile(r"^[a-z0-9-]{1,32}$")


def register_known_backend(
    backend_id: str,
    *,
    label: str,
    routing: Routing,
    permission_config: Optional[Tuple[str, str]] = None,
    model_namespace: Optional[str] = None,
    model_source: str = MODEL_SOURCE_ACP_ADVERTISED,
    models: Tuple[str, ...] = (),
    session_family: str = "",
    runtime: bool = True,
    session_array: bool = False,
) -> None:
    """Teach this build to SPELL a new (config-authored) backend id.

    Makes the id known -- so ``AcpProvider`` stops rejecting it and a governance
    rule can name it -- and records its routing, policy id, label, namespace and
    model source. Does NOT make it selectable: that is
    :func:`~kiro_crew.agent_sdk.backends.register_selectable_backend`, which refuses
    an ``UNVERIFIED`` routing.

    Raises ``ValueError`` for a malformed id, an id already known (a builtin or an
    earlier registration), a routing/permission_config mismatch or an empty label.
    All validation runs before any mutation, so a refusal leaves no half-written id.
    """
    if not isinstance(backend_id, str) or not _REGISTERABLE_BACKEND_ID.match(backend_id):
        raise ValueError(
            f"cannot register backend id {backend_id!r}: an id must match "
            f"{_REGISTERABLE_BACKEND_ID.pattern!r}"
        )
    if backend_id in ACP_BACKENDS_KNOWN:
        raise ValueError(
            f"cannot register {backend_id!r}: it is already a known backend; "
            "registration must not overwrite an existing label or routing"
        )
    if not isinstance(routing, Routing):
        raise ValueError(f"cannot register {backend_id!r}: routing must be a Routing")
    pair: Optional[Tuple[str, str]] = None
    if routing is Routing.SESSION_CONFIG:
        if not permission_config or len(permission_config) != 2:
            raise ValueError(
                f"cannot register {backend_id!r} with SESSION_CONFIG routing and no "
                "permission_config (option_id, value)"
            )
        pair = (permission_config[0], permission_config[1])
    elif permission_config is not None:
        raise ValueError(
            f"cannot register {backend_id!r}: permission_config is only meaningful for "
            f"SESSION_CONFIG routing, not {routing.value!r}"
        )
    if not label:
        raise ValueError(f"cannot register {backend_id!r}: a provider label is required")

    _registered_known.add(backend_id)
    ACP_BACKEND_ROUTING[backend_id] = routing
    if pair is not None:
        ACP_BACKEND_PERMISSION_CONFIG[backend_id] = pair
    POLICY_ID_BY_BACKEND[backend_id] = backend_id
    _REGISTERED_PROVIDER_LABELS[backend_id] = label
    _REGISTERED_MODEL_NAMESPACE[backend_id] = model_namespace or backend_id
    _REGISTERED_MODEL_SOURCE[backend_id] = (model_source, tuple(models))
    if session_family:
        _REGISTERED_SESSION_FAMILY[label] = session_family
    if runtime:
        _registered_runtime.add(backend_id)
    if session_array:
        _registered_session_array.add(backend_id)


def is_registered_backend(backend: str) -> bool:
    """True for an id :func:`register_known_backend` admitted (never a builtin)."""
    return backend in _registered_known


def registered_backend_ids() -> FrozenSet[str]:
    """Every id registered from ``harnesses.json`` in this process."""
    return frozenset(_registered_known)


def provider_label_for(backend: str) -> str:
    """The provider label a REGISTERED backend recorded, or ``""``."""
    return _REGISTERED_PROVIDER_LABELS.get(backend, "")


def registered_model_namespace(backend: str) -> str:
    """A registered backend's model namespace, or ``""`` for any other id."""
    return _REGISTERED_MODEL_NAMESPACE.get(backend, "")


def session_family_for_label(label: str) -> str:
    """The session family a provider label belongs to, or ``""`` for none."""
    return _REGISTERED_SESSION_FAMILY.get(label, "")


def same_session_family(a: str, b: str) -> bool:
    """Whether backend ids *a* and *b* are registered into one session family."""
    family = session_family_for_label(provider_label_for(a))
    return bool(family) and family == session_family_for_label(provider_label_for(b))


def registered_session_array(backend: str) -> bool:
    """Whether a registered *backend* takes its MCP servers from the session array."""
    return backend in _registered_session_array


def registered_model_source(backend: str) -> "Tuple[str, Tuple[str, ...]] | None":
    """A registered backend's ``(model_source, static_models)``, or ``None``."""
    return _REGISTERED_MODEL_SOURCE.get(backend)


def acp_runtime_backends() -> FrozenSet[str]:
    """Backends served by ``AcpRuntime`` in this process: the vocabulary plus registrations.

    The foreground start path reads this, so a descriptor backend (which exists
    only as a ``DescriptorHarness`` on the shared runtime) never falls to the
    ``AcpClient`` path, whose spawn ladder would end in the kiro-cli branch.
    Equal to ``ACP_BACKENDS_ACP_RUNTIME`` when nothing is registered.
    """
    return frozenset(ACP_BACKENDS_ACP_RUNTIME | _registered_runtime)


def uses_advertised_model_selection(backend: str) -> bool:
    """Whether *backend* takes its model vocabulary from what its harness advertises.

    Membership of ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``, OR a registered
    descriptor declaring ``model_source: "acp_advertised"``. The set is frozen at
    import, before any descriptor registers, so membership alone left a
    descriptor's picker offering nothing but ``auto``.
    """
    from kiro_crew.agent_sdk.backends_models import ACP_BACKENDS_ADVERTISED_MODEL_SELECTION

    if backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION:
        return True
    info = _REGISTERED_MODEL_SOURCE.get(backend)
    return info is not None and info[0] == MODEL_SOURCE_ACP_ADVERTISED


def switches_model_via_config_option(backend: str, config_options: Any) -> bool:
    """Whether a model switch on *backend* travels down ``session/set_config_option``.

    Membership of ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``, OR a registered
    descriptor whose session actually advertised a ``model`` select in
    *config_options*. claude-agent-acp implements no ``session/set_model``, so a
    seat routed down that request had every model pick refused. The clause claims
    nothing from config: it reads the adapter's own advertisement on this session.
    """
    from kiro_crew.agent_sdk.backends_models import ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION

    if backend in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION:
        return True
    if backend not in _registered_known:
        return False
    return any(
        isinstance(opt, dict) and opt.get("id") == "model" and opt.get("type") == "select"
        for opt in config_options or ()
    )


def _reset_registered_backends() -> None:
    """TEST-ONLY: drop every registration, restoring the builtin-only state."""
    from kiro_crew.agent_sdk.backends import _baseline, _selectable

    for backend_id in list(_registered_known):
        ACP_BACKEND_ROUTING.pop(backend_id, None)
        ACP_BACKEND_PERMISSION_CONFIG.pop(backend_id, None)
        POLICY_ID_BY_BACKEND.pop(backend_id, None)
        _baseline.discard(backend_id)
        _selectable.discard(backend_id)
    _registered_known.clear()
    from kiro_crew.agent_sdk.capability_inheritance import _reset_capabilities_from

    _reset_capabilities_from()
    _registered_runtime.clear()
    _registered_session_array.clear()
    _REGISTERED_PROVIDER_LABELS.clear()
    _REGISTERED_MODEL_NAMESPACE.clear()
    _REGISTERED_SESSION_FAMILY.clear()
    _REGISTERED_MODEL_SOURCE.clear()
    from kiro_crew.operator_backends import _reset_operator_backends_loaded

    _reset_operator_backends_loaded()


__all__ = [
    "MODEL_SOURCE_ACP_ADVERTISED",
    "acp_runtime_backends",
    "is_registered_backend",
    "provider_label_for",
    "register_known_backend",
    "registered_backend_ids",
    "registered_model_namespace",
    "registered_model_source",
    "registered_session_array",
    "same_session_family",
    "session_family_for_label",
    "switches_model_via_config_option",
    "uses_advertised_model_selection",
]
