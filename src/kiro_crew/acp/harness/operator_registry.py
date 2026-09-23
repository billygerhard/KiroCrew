"""Load ``harnesses.json`` at boot and register every valid operator harness.

This module is the ONE orchestration point that turns operator descriptors (data
on disk) into a servable, selectable backend. It sits between three seams that
each own a narrower job and deliberately know nothing of each other:

* :func:`kiro_crew.acp.harness.descriptor.load_operator_descriptors` -- pure
  parse. Reads the file, returns ``(valid, invalid)``, never registers or raises.
* :func:`kiro_crew.agent_sdk.backends.register_known_backend` -- the vocabulary
  side. Makes an id spellable, routed, labelled, policy-nameable, own-namespaced,
  and (with ``runtime=True``) served on ``AcpRuntime``.
* :func:`kiro_crew.acp.harness.register_operator_harness` -- the runtime side.
  Makes the id resolvable through ``harness_for`` as a ``DescriptorHarness``.
* :func:`kiro_crew.agent_sdk.backends.register_selectable_backend` -- the
  selection side. Makes a ROUTED id offerable in the backend switch.

Ordering within one valid descriptor matters and is fixed here:
``register_known_backend`` FIRST (it records the routing ``register_selectable_backend``
then reads), the harness registration, and ``register_selectable_backend`` LAST and
ONLY when the descriptor is selectable. A descriptor with no recognized routing is
registered as KNOWN (spellable, nameable in a rule) but NOT selectable -- the
visible-but-unselectable row D3 wants, whose reason stays retrievable via
:func:`invalid_operator_harnesses` / :func:`unselectable_operator_harnesses` for the
future Settings surface.

Idempotence is a hard requirement: ``bootstrap_context`` can run more than once in
a process (``cli.main`` then ``run_gateway``), and re-registering a known id raises.
So :func:`load_and_register_operator_descriptors` SKIPS an id already registered,
which makes a second boot a no-op rather than a crash.

A note on precedence, load-bearing for the ordering constraint (D4): this runs
BEFORE the first config load resolves ``agent.acp_backend``, because a persisted
value naming an operator harness must survive ``resolve_selected_backend`` -- which
reads the selectable registry live. The call site is the ``ProviderRegistry``
seam: ``DefaultProviderRegistry.register_acp_backends`` calls this, and
``bootstrap_context`` invokes that seam immediately after ``set_context`` and
before the governance narrowing that re-runs ``resolve_selected_backend`` on
``cfg.agent.acp_backend``. No other boot path registers a harness (H13).

A note on freshness: this reads ``harnesses.json`` ONCE, at boot. An edit to that
file takes effect on the next gateway start, not live -- the same restart the
docstring of every boot-time registration implies, and stated for the operator in
the module docstring of :mod:`kiro_crew.acp.harness.descriptor`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from kiro_crew.acp.harness import register_operator_harness
from kiro_crew.acp.harness.descriptor import (
    ROUTING_AGENT_SPEC,
    ROUTING_SESSION_CONFIG,
    HarnessDescriptor,
    load_operator_descriptors,
)
from kiro_crew.acp.harness.routing_verification import (
    UNVERIFIED_REASON,
    descriptor_fingerprint,
    is_attested,
)
from kiro_crew.agent_sdk.backends import (
    Routing,
    register_known_backend,
    register_selectable_backend,
)

logger = logging.getLogger(__name__)

#: Descriptors that FAILED to parse/validate, keyed by the id they were filed
#: under, mapped to their diagnosable reasons. The ``invalid()`` shape the salvage
#: registry carried, kept for the future Settings surface: a malformed entry costs
#: its row, never boot, and the row stays retrievable so an operator can be shown
#: what is wrong. Populated by :func:`load_and_register_operator_descriptors`.
_INVALID: dict[str, list[str]] = {}

#: Valid descriptors that registered as KNOWN but NOT SELECTABLE -- their routing
#: was absent, unrecognized, or declared but not yet verified end to end -- keyed
#: by id and mapped to the reason. Distinct from :data:`_INVALID`: these parsed
#: cleanly and can be spelled and named in a rule; they simply cannot be offered
#: as a session backend. The visible-but-unselectable row D3 wants.
_UNSELECTABLE: dict[str, str] = {}

#: The subset of :data:`_UNSELECTABLE` whose ONLY missing piece is the routing
#: attestation (``routing_verification``): a recognized routing was declared, the
#: descriptor registered as known and runnable, and a successful probe makes it
#: selectable without a restart (:func:`mark_routing_verified`). The listing
#: offers these a Verify action; the unroutable rest get none.
_UNVERIFIED: set[str] = set()


def _routing_enum_for(descriptor: HarnessDescriptor) -> Optional[Routing]:
    """The ``backends.Routing`` a descriptor's routing string maps to, or ``None``.

    ``None`` means "no verified routing" -- the descriptor registers as
    known-but-unselectable. The two descriptor routing constants and the two enum
    members share their wire values (``"agent_spec"`` / ``"session_config"``), but
    this maps them explicitly rather than by ``Routing(descriptor.routing)`` so an
    unrecognized string returns ``None`` here instead of raising a ``ValueError``
    the caller would have to catch -- the descriptor layer already validated the
    string, and anything it let through that is not one of these two is, by
    definition, the unselectable case.
    """
    if descriptor.routing == ROUTING_AGENT_SPEC:
        return Routing.AGENT_SPEC
    if descriptor.routing == ROUTING_SESSION_CONFIG:
        return Routing.SESSION_CONFIG
    return None


def load_and_register_operator_descriptors(*, path=None) -> None:
    """Read ``harnesses.json`` and register every valid operator harness.

    Called once per boot from ``DefaultProviderRegistry.register_acp_backends`` --
    the ``ProviderRegistry`` seam ``bootstrap_context`` invokes after the context
    is installed and before the first config load resolves ``agent.acp_backend``
    (D4). Never raises:
    a parse failure is recorded in :data:`_INVALID`, and a single descriptor that
    cannot be registered is logged and skipped, so one bad entry costs its row and
    nothing more -- the gateway still boots on the builtin harnesses.

    For each VALID descriptor, in order:

    1. ``register_known_backend`` -- makes the id spellable, routed, labelled,
       policy-nameable, own-namespaced, and served on ``AcpRuntime``
       (``runtime=True``, the default: a ``DescriptorHarness`` only exists on path
       A). Its routing is the descriptor's, mapped to the enum;
       ``permission_config`` is passed only for a ``session_config`` descriptor,
       from the ``(option, value)`` the descriptor validated.
    2. ``register_operator_harness`` -- makes the id resolvable through
       ``harness_for`` as a ``DescriptorHarness`` built from this descriptor.
    3. ``register_selectable_backend`` -- ONLY when the descriptor is selectable (a
       recognized routing). An unroutable descriptor is left known-but-unselectable
       with its reason in :data:`_UNSELECTABLE`.

    Idempotent: an id already in ``ACP_BACKENDS_KNOWN`` is skipped, so a second
    bootstrap pass is a no-op rather than a re-registration crash.
    """
    valid, invalid = load_operator_descriptors(path=path)

    for harness_id, reasons in invalid:
        _INVALID[harness_id] = list(reasons)
        logger.warning("operator harness %r ignored: %s", harness_id, "; ".join(reasons))

    for descriptor in valid:
        backend_id = descriptor.id
        # Idempotent: a second bootstrap pass must not re-register (which raises).
        # The operator register is the authoritative "already registered BY THIS
        # LOADER" signal -- register_operator_harness is what writes it. The known
        # set is NOT that signal: a builtin's id is in it from module import, so a
        # descriptor that names ``kiro`` or ``claude`` would read as "already done"
        # and vanish without a diagnosable row. That collision falls through to the
        # registrar below, which refuses it, and the refusal is recorded invalid.
        if _is_registered_operator_id(backend_id):
            continue

        routing_enum = _routing_enum_for(descriptor)
        # A descriptor with no recognized routing is still KNOWN (spellable,
        # nameable). It is registered as UNVERIFIED so register_selectable_backend
        # will refuse it, which is the visible-but-unselectable state.
        effective_routing = routing_enum if routing_enum is not None else Routing.UNVERIFIED
        permission_config = None
        if routing_enum is Routing.SESSION_CONFIG and descriptor.permission_config is not None:
            permission_config = (
                descriptor.permission_config.option,
                descriptor.permission_config.value,
            )

        # The label is not only a display name: ``provider_label`` persists a
        # session under it, ``detect_provider_switch`` compares it, and cleanup
        # routes on it (harness-parity H11). A label another backend already
        # answers to would file this backend's sessions under that backend --
        # ``"acp"`` would make them kiro sessions and get them pruned. Refused here,
        # where both the builtin mapping and the registered labels are visible.
        taken_by = _label_owner(descriptor.label)
        if taken_by is not None:
            _INVALID.setdefault(
                backend_id,
                [
                    f"display_name {descriptor.label!r} is already the provider label of "
                    f"backend {taken_by!r}; sessions are persisted and cleaned up by label, "
                    "so two backends cannot share one"
                ],
            )
            continue

        try:
            register_known_backend(
                backend_id,
                label=descriptor.label,
                routing=effective_routing,
                permission_config=permission_config,
                model_namespace=backend_id,
                runtime=True,
            )
            register_operator_harness(descriptor)
        except ValueError as exc:
            # A registration collision (an id a builtin already serves) or a
            # malformed id the descriptor layer did not catch: record it invalid with
            # the registrar's own reason so the row is diagnosable, and keep serving
            # everything else.
            logger.warning("operator harness %r could not be registered", backend_id, exc_info=True)
            _INVALID.setdefault(backend_id, [f"could not be registered: {exc}"])
            continue

        if not descriptor.selectable:
            _UNSELECTABLE[backend_id] = (
                "no recognized routing declared, so nothing establishes that its "
                "tool calls reach the host permission gate"
            )
            continue
        # A declared routing is a CLAIM. Selectability needs the gateway's own
        # evidence that the harness asks before it acts: an attestation this
        # gateway recorded after an end-to-end probe (routing_verification), keyed
        # by the descriptor's spawn fingerprint so an edit to the binary, argv,
        # routing or option revokes it by construction. Without one the backend is
        # known and runnable-for-verification, but no chat can pick it.
        if not is_attested(descriptor):
            _UNSELECTABLE[backend_id] = UNVERIFIED_REASON
            _UNVERIFIED.add(backend_id)
            continue
        try:
            register_selectable_backend(backend_id)
        except ValueError as exc:
            # Known but refused selectability -- record the reason and leave it
            # visible-but-unselectable rather than aborting the whole load.
            _UNSELECTABLE[backend_id] = str(exc)
            logger.warning("operator harness %r is known but not selectable: %s", backend_id, exc)


def invalid_operator_harnesses() -> dict[str, list[str]]:
    """Descriptors that failed to parse/validate, id -> reasons (a copy).

    The ``invalid()`` shape, for the Settings surface: what is wrong with each entry
    an operator wrote that could not become a harness at all.
    """
    return {k: list(v) for k, v in _INVALID.items()}


def unselectable_operator_harnesses() -> dict[str, str]:
    """Valid-but-unselectable operator harnesses, id -> reason (a copy).

    Distinct from :func:`invalid_operator_harnesses`: these parsed cleanly and are
    spellable and nameable, but declared no verified routing, so they are visible in
    Settings with a reason rather than offered as a session backend.
    """
    return dict(_UNSELECTABLE)


def unverified_operator_harnesses() -> frozenset[str]:
    """Ids whose only missing piece is the end-to-end routing attestation.

    A subset of :func:`unselectable_operator_harnesses`'s keys. These are the
    rows Settings offers a Verify action for; an unroutable descriptor (no
    recognized routing) is not among them because there is nothing to verify.
    """
    return frozenset(_UNVERIFIED)


def registered_operator_descriptor(backend_id: str) -> Optional[HarnessDescriptor]:
    """The registered descriptor for *backend_id*, or ``None`` for a builtin/unknown."""
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    return _OPERATOR_REGISTER.get(backend_id)


#: The reason recorded for a verified backend the deployment's ``agent_backend``
#: policy denies: verified routing is necessary for selectability, not sufficient.
POLICY_DENIED_REASON = (
    "routing verified, but this deployment's agent_backend policy does not permit "
    "the backend, so it is not selectable"
)


def mark_routing_verified(backend_id: str, record: Mapping[str, Any]) -> bool:
    """Project a just-written attestation into the live registry. True when the
    backend is selectable afterwards; False when the deployment's policy denies it.

    The boot-time gate reads the attestation file; this is the live half so a
    successful Verify in Settings does not need a gateway restart. *record* is the
    attestation :func:`routing_verification.record_attestation` just wrote (the
    caller ran that off the event loop); this function does no file I/O of its
    own, and it refuses an id that is not a registered operator descriptor or a
    record whose fingerprint is not the live descriptor's.

    Registration widens the baseline, and the ``agent_backend`` governance
    narrowing that ran at boot has not seen this id, so it is re-applied here
    exactly as at boot: an administrator's denial holds for a backend the owner
    verified after that boot. A policy-denied id stays unselectable with
    :data:`POLICY_DENIED_REASON` and is NOT offered for verification again --
    verification is not what it lacks.
    """
    from kiro_crew.agent_backend_governance import narrow_selectable_backends
    from kiro_crew.agent_sdk.backends import selectable_backends

    descriptor = registered_operator_descriptor(backend_id)
    if descriptor is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")
    if record.get("fingerprint") != descriptor_fingerprint(descriptor):
        raise ValueError(
            f"the attestation does not match the current descriptor for {backend_id!r}"
        )
    register_selectable_backend(backend_id)
    narrow_selectable_backends()
    _UNVERIFIED.discard(backend_id)
    if backend_id in selectable_backends():
        _UNSELECTABLE.pop(backend_id, None)
        return True
    _UNSELECTABLE[backend_id] = POLICY_DENIED_REASON
    return False


def revoke_routing_verification(backend_id: str, reason: str) -> None:
    """Take an operator backend OUT of the selectable set now, with *reason*.

    The spawn path calls this when the executable it is about to exec does not
    match the verified digest (``routing_verification.spawn_attestation_problem``):
    the attestation on disk is dropped, the id leaves the selectable registry,
    and Settings shows the row as unverified with the reason and a Verify action.
    A builtin or unknown id is refused -- only registered descriptors carry a
    verification to revoke.
    """
    from kiro_crew.acp.harness.routing_verification import revoke_attestation
    from kiro_crew.agent_sdk.backends import unregister_selectable_backend

    if registered_operator_descriptor(backend_id) is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")
    revoke_attestation(backend_id)
    unregister_selectable_backend(backend_id)
    _UNSELECTABLE[backend_id] = f"{reason}; {UNVERIFIED_REASON}"
    _UNVERIFIED.add(backend_id)


def operator_backend_models(backend_id: str) -> "tuple[str, tuple[str, ...]] | None":
    """A registered operator backend's ``(model_source, models)``, or ``None``.

    ``None`` when ``backend_id`` names no registered operator descriptor (a
    builtin, or nothing). For a static descriptor ``models`` is its declared
    catalog; for an ``acp_advertised`` one ``models`` is empty and the caller
    reads the live/cached advertised list from the model registry instead. This
    is the read the ``/api/models`` endpoint uses so an operator backend's picker
    offers ITS models rather than falling through to kiro-cli's ``--list-models``
    catalog (which the descriptor's harness would reject).
    """
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    descriptor = _OPERATOR_REGISTER.get(backend_id)
    if descriptor is None:
        return None
    return (descriptor.model_source, tuple(descriptor.models))


def _is_registered_operator_id(backend_id: str) -> bool:
    """True when an earlier bootstrap pass registered ``backend_id`` from a descriptor.

    Reads the operator register -- the table :func:`register_operator_harness`
    writes -- rather than ``ACP_BACKENDS_KNOWN``, which also holds every builtin
    from module import and so cannot distinguish "wired by a prior pass" from "a
    descriptor colliding with a builtin id". Deferred import: the harness package
    imports this module.
    """
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    return backend_id in _OPERATOR_REGISTER


def _label_owner(label: str) -> Optional[str]:
    """The backend id already answering to *label*, or ``None`` when it is free.

    Checks the builtin mapping (``PROVIDER_LABEL_BY_BACKEND``, which also carries
    kiro's DEFAULT label) and then every registered id's recorded label. Deferred
    import: ``acp.types`` is above the vocabulary leaf this module builds on.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND
    from kiro_crew.agent_sdk.backends import known_backends, provider_label_for

    for builtin_id, builtin_label in PROVIDER_LABEL_BY_BACKEND.items():
        if builtin_label == label:
            return builtin_id
    # ``provider_label_for`` answers only for registered ids (empty for a
    # builtin), so walking the whole known set visits exactly the registered ones.
    for known_id in known_backends():
        if provider_label_for(known_id) == label:
            return known_id
    return None


def _reset_operator_diagnostics() -> None:
    """TEST-ONLY: clear the invalid/unselectable diagnostic maps.

    Paired with ``backends._reset_registered_backends`` and
    ``harness._reset_operator_register`` so one test's boot-load cannot leak its
    diagnostic rows into the next. Not called by product code -- the load runs once
    at boot.
    """
    _INVALID.clear()
    _UNSELECTABLE.clear()
    _UNVERIFIED.clear()
