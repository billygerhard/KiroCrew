"""What tools an assigned agent can actually reach.

A cost profile may assign a Host_Agent to a role, and that agent has to be able
to call the engine's own MCP tools — the profile's tier-1 reviewer is useless if
its session cannot record a verdict. kiro-cli decides that from the agent's
``tools`` allowlist: an agent with no allowlist reaches every tool, while an agent
that declares one reaches exactly what it names. So an operator who narrowed an
agent's tools for unrelated reasons has, without touching this app, made that
agent unable to drive a run.

This module answers only "can it reach them", and deliberately not "should we
warn": the advisory is built in :mod:`.advisories` alongside the other one, so
there is one mechanism for telling an operator about a valid-but-wrong setup.

Two limits are worth stating rather than hiding.

The allowlist is the whole question. An agent's own ``mcpServers`` map is *not*
evidence: kiro-cli loads the servers in the global MCP settings into every agent
regardless of that agent's config, so an agent that declares no server may still
reach one. Treating absence there as a problem would produce a warning on a
perfectly working setup, and a warning that cries wolf is worse than none.

A per-tool grant counts as a grant. ``@server/one_tool`` names the engine server
explicitly, and until the server's tool list is fixed there is no honest way to
say whether the subset is sufficient — so an explicit engine grant is taken at
face value rather than reported as if nothing were granted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir

from .schema import SECTION_PROJECTS

logger = logging.getLogger(__name__)

#: The MCP server the app registers, referenced from an agent's tool list as
#: ``@spec-engine``. Spelled here rather than imported from the config store so
#: the advisory path does not depend on the store that raises the advisories.
ENGINE_MCP_SERVER = "spec-engine"

#: Agent-config keys this module reads.
TOOLS_KEY = "tools"
NAME_KEY = "name"

#: Allowlist entry granting every tool.
WILDCARD_TOOL = "*"

#: Project field naming the checkout, whose ``.kiro/agents`` is a read scope.
PROJECT_PATH_FIELD = "path"


@dataclass(frozen=True)
class AgentToolSurface:
    """The tool surface of one agent, as configuration describes it."""

    name: str
    #: Whether an agent configuration by this name was found at all.
    found: bool = False
    #: The declared allowlist, or ``None`` when the agent declares none — which
    #: is not the same as an empty one. No allowlist means every tool reaches the
    #: agent; an empty allowlist means none does.
    tools: tuple[str, ...] | None = None
    #: File the surface was read from, for a message that names where to look.
    source: str = ""

    @property
    def unrestricted(self) -> bool:
        """Whether this agent declares no allowlist and so reaches every tool."""
        return self.found and self.tools is None

    def grants(self, server: str = ENGINE_MCP_SERVER) -> bool:
        """Whether *server*'s tools reach this agent."""
        if not self.found:
            return False
        if self.tools is None:
            return True
        reference = f"@{server}"
        return any(
            entry == WILDCARD_TOOL or entry == reference or entry.startswith(f"{reference}/")
            for entry in self.tools
        )


#: Resolves an agent name to its tool surface. Injected so a caller can check a
#: surface it already knows about without touching disk.
AgentSurfaceLookup = Callable[[str], AgentToolSurface]


@dataclass(frozen=True)
class DiskAgentSurfaces:
    """Reads agent surfaces from kiro-cli agent directories, in search order."""

    directories: tuple[Path, ...]

    def __call__(self, name: str) -> AgentToolSurface:
        for directory in self.directories:
            found = _read_from(directory, name)
            if found is not None:
                return found
        return AgentToolSurface(name=name, found=False)


def agent_directories(doc: Mapping[str, Any]) -> tuple[Path, ...]:
    """Agent directories to search for *doc*: every project's, then the user's.

    A profile is app-wide while a project-scoped agent belongs to one checkout, so
    every configured project's directory is searched. That errs toward finding an
    agent — the alternative reports "not installed" for an agent that is installed
    somewhere the check did not look, and an operator cannot act on that.
    """
    directories: list[Path] = []
    projects = doc.get(SECTION_PROJECTS)
    if isinstance(projects, Mapping):
        for entry in projects.values():
            if not isinstance(entry, Mapping):
                continue
            path = entry.get(PROJECT_PATH_FIELD)
            if isinstance(path, str) and path.strip():
                directories.append(project_agents_dir(path.strip()))
    directories.append(kiro_agents_dir())
    return tuple(directories)


def disk_lookup(doc: Mapping[str, Any]) -> AgentSurfaceLookup:
    """The lookup used when a caller names none: the directories *doc* implies."""
    return DiskAgentSurfaces(agent_directories(doc))


def _read_from(directory: Path, name: str) -> AgentToolSurface | None:
    """Read *name*'s surface out of *directory*, or ``None`` when it is not there.

    The filename is the fast path, not the contract: kiro-cli resolves an agent by
    the ``name`` field in the document, so a spec saved under another filename is
    still that agent and is found by scanning.
    """
    direct = directory / f"{name}.json"
    document = _load(direct)
    if document is not None and _declared_name(document, direct) == name:
        return _surface_from(name, document, direct)
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        return None
    for candidate in candidates:
        if candidate == direct:
            continue
        document = _load(candidate)
        if document is not None and _declared_name(document, candidate) == name:
            return _surface_from(name, document, candidate)
    return None


def _load(path: Path) -> dict[str, Any] | None:
    """Parse an agent document, or ``None`` when it cannot be read as one.

    An unreadable or malformed agent file is somebody else's file: this app does
    not own ``~/.kiro/agents``, so it reports what it can read and stays quiet
    about what it cannot rather than failing a configuration write over it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("agent configuration %s is not valid JSON", path)
        return None
    return parsed if isinstance(parsed, dict) else None


def _declared_name(document: Mapping[str, Any], path: Path) -> str:
    declared = document.get(NAME_KEY)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return path.stem


def _surface_from(name: str, document: Mapping[str, Any], path: Path) -> AgentToolSurface:
    declared = document.get(TOOLS_KEY)
    tools: tuple[str, ...] | None = None
    if isinstance(declared, (list, tuple)):
        tools = tuple(entry for entry in declared if isinstance(entry, str))
    return AgentToolSurface(name=name, found=True, tools=tools, source=str(path))
