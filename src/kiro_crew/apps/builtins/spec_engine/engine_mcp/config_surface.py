"""The configuration document's shape at the MCP boundary: elision and refusals.

Two tools live on this shape, and each has one thing it must not do.

``get_config`` must not hand a credential to a model. The document legitimately
holds them -- an access token in a capability's environment, a credential in a
project's variable map -- and a tool result is serialized into a model's context
and may be logged or echoed from there. So the document is elided before it
leaves, by the Config_Store's own classification rather than by a rule spelled
here: a second spelling is how one surface comes to elide what the other returns.

``write_config`` must not report a refusal as a failure of the server. The
Config_Store refuses two whole classes of patch -- a config-only path from a
surface no operator confirmed, and a patch whose merged document would be invalid
-- and both are answers a caller can act on: which path is fenced, which key is
wrong. They come back as results carrying a ``refused`` code, in the same shape
the setup tools use, so an agent can relay them to a human rather than reporting
that the tool broke.

The refusal classes are traced against what :meth:`ConfigStore.write` actually
raises, not against what the names suggest: ``ConfigWriteRefused`` derives
``PermissionError``, ``ConfigValidationError`` derives ``ValueError``, and
``ConfigLoadError``/``ConfigRecordError`` derive ``RuntimeError``. Only the first
two are refusals of a patch. ``ConfigRecordError`` is deliberately NOT one: it
means the document changed and nothing recorded who changed it, which is a
failure the caller must see as a failure.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..engine.config import ConfigWarning
from ..engine.config.store import (
    ConfigLoadError,
    ConfigStore,
    ConfigValidationError,
    ConfigWriteRefused,
    elide_secrets,
)

__all__ = [
    "REFUSAL_CODES",
    "REFUSAL_CONFIG_INVALID",
    "REFUSAL_CONFIG_REFUSED",
    "REFUSAL_CONFIG_UNREADABLE",
    "config_payload",
    "render_advisory",
    "write_payload",
    "write_refusal",
]

#: Refusal code for a patch the config-only fence would not let this surface write.
REFUSAL_CONFIG_REFUSED = "config-write-refused"

#: Refusal code for a patch whose merged document would fail validation.
REFUSAL_CONFIG_INVALID = "config-invalid"

#: Refusal code for a persisted document that cannot be read or parsed. Not a
#: refusal of anything the caller sent -- it is the engine declining to answer
#: about a file a human has to repair -- but it is returned in refusal shape so a
#: caller relays "your configuration file is corrupt, here is where" instead of a
#: tool error naming an exception class.
REFUSAL_CONFIG_UNREADABLE = "config-unreadable"

#: Key naming the refusal, matching the setup surface so one caller-side branch
#: handles both.
_REFUSED_KEY = "refused"

#: Refusal classes paired with the code each earns. A tuple rather than a mapping
#: because order decides which code a subclass gets; no entry here derives
#: another, but that is a fact about today's classes and the tuple keeps it from
#: mattering.
REFUSAL_CODES: tuple[tuple[type[Exception], str], ...] = (
    (ConfigWriteRefused, REFUSAL_CONFIG_REFUSED),
    (ConfigValidationError, REFUSAL_CONFIG_INVALID),
    (ConfigLoadError, REFUSAL_CONFIG_UNREADABLE),
)


def write_refusal(exc: Exception) -> dict[str, Any] | None:
    """Return the structured refusal for *exc*, or ``None`` if it is not one.

    ``None`` rather than a catch-all payload: an exception this boundary does not
    recognise is a tool error, and dressing it as a refusal would report a broken
    write path as a decision the engine made.
    """
    for refusal, code in REFUSAL_CODES:
        if isinstance(exc, refusal):
            payload: dict[str, Any] = {
                _REFUSED_KEY: code,
                "reason": type(exc).__name__,
                "message": str(exc),
            }
            if isinstance(exc, ConfigWriteRefused):
                # The fenced paths, named: a caller told only "refused" retries
                # the same patch, while a caller told which path is config-only
                # can drop that key or send the operator to the panel.
                payload["surface"] = exc.surface.name
                payload["config_only_paths"] = list(exc.paths)
            if isinstance(exc, ConfigValidationError):
                payload["errors"] = [
                    {"path": error.path, "message": error.message} for error in exc.errors
                ]
            return payload
    return None


def render_advisory(warning: ConfigWarning) -> dict[str, Any]:
    """One advisory as its identifier, its location, and its text.

    The identifier travels because a refusal, a Doctor finding and this reply
    quote the same one: an operator correlating them by name is the reason the
    codes exist. ``requires_acknowledgment`` travels because an advisory that
    needs a human to say "yes, I know" is a different obligation from one that
    only needs reading, and a relaying caller cannot tell them apart otherwise.
    """
    return {
        "code": warning.code,
        "path": warning.path,
        "message": warning.message,
        "project": warning.project,
        "requires_acknowledgment": warning.requires_acknowledgment,
    }


def render_advisories(warnings: Sequence[ConfigWarning]) -> list[dict[str, Any]]:
    """Every advisory, in the order the store raised them."""
    return [render_advisory(warning) for warning in warnings]


def config_payload(store: ConfigStore) -> dict[str, Any]:
    """The persisted configuration as a caller may see it.

    ``configured`` answers the question a first-run caller actually has -- is
    there configuration yet -- separately from ``document``, because an empty
    document and an absent file both serialize to ``{}`` and only one of them
    means "run the setup assistant".

    ``elided`` lists the dotted paths whose value was withheld, so a caller can
    tell an elision from a literal ``<elided>`` somebody typed, and can report
    "a token is configured here" without ever holding the token.
    """
    document = store.document()
    elided = elide_secrets(document)
    return {
        "configured": store.path.is_file(),
        "path": str(store.path),
        "document": elided.document,
        "elided": list(elided.paths),
        "errors": [{"path": error.path, "message": error.message} for error in store.validate()],
        "advisories": render_advisories(store.advisories()),
    }


def write_payload(
    patch: Mapping[str, Any],
    merged: Mapping[str, Any],
    advisories: Sequence[ConfigWarning],
) -> dict[str, Any]:
    """What the write tool returns: what changed, the document, and what to be told.

    The document comes back elided, through the same classification
    :func:`config_payload` uses. A write reply carrying the raw merged document
    would be the second read path -- the one nobody remembered to elide -- and a
    caller only has to write one ordinary setting to make the store hand back
    every token in the file.
    """
    elided = elide_secrets(merged)
    return {
        "written": True,
        "keys": sorted(str(key) for key in patch),
        "document": elided.document,
        "elided": list(elided.paths),
        "advisories": render_advisories(advisories),
    }
