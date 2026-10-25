"""Find the executable an operator descriptor (``harnesses.json``) names.

The generic form of the plain-binary ladder the bundled single-binary hosts walk
(opencode, goose, deepseek), with the tool name supplied by the descriptor
instead of hardcoded. A descriptor host serves ACP itself, so there is no
``node_modules`` rung: an operator who runs a Node adapter names its launcher (or
a wrapper script) as the ``executable``.
"""

from __future__ import annotations

import os
import shutil

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path, describe_search_path

__all__ = ["descriptor_executable_not_found_message", "resolve_descriptor_executable"]


def resolve_descriptor_executable(executable: str) -> tuple[str | None, str]:
    """``(absolute_path, search_path)`` for *executable*, or ``(None, search_path)``.

    An executable path is honoured verbatim, made absolute so the file checked is
    the file exec'd whatever the session's working directory. A bare name walks
    mise, then the augmented PATH, the same two rungs the bundled resolvers walk.
    """
    from kiro_crew.acp.client import _mise_which
    from kiro_crew.acp.launch import _normalize_exe_casing

    search_path = augmented_path(os.environ.get("PATH", ""))
    if not executable:
        return None, search_path
    if platform_compat.is_executable_file(executable):
        resolved = os.path.abspath(executable)
        return _normalize_exe_casing(resolved) or resolved, search_path
    mise_resolved = _mise_which(executable)
    if mise_resolved:
        return mise_resolved, search_path
    on_path = shutil.which(executable, path=search_path)
    if on_path:
        return _normalize_exe_casing(on_path) or on_path, search_path
    return None, search_path


def descriptor_executable_not_found_message(
    harness_id: str, executable: str, search_path: str
) -> str:
    """The message for an operator harness whose executable is not where we looked."""
    return (
        f"harness {harness_id!r}: executable {executable!r} not found "
        f"({describe_search_path(search_path)}). Install it, put it on PATH, or set "
        f"the descriptor's 'executable' to an absolute path in harnesses.json."
    )
