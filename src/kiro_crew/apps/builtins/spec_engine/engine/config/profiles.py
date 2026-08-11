"""Cost profiles: the bundle a project selects for its agents, models, and effort.

A profile answers one question per role — which agent, which model, how much
reasoning effort — and pins the two limits that ride with that choice: how many
tasks a wave dispatches at once and how many credits one of its runs may spend.
Those two are ordinary settings named in :data:`PROFILE_SETTING_KEYS`, so the
registry keeps ownership of their types and bounds and a profile cannot invent a
limit the rest of the app does not understand.

Parsing here is lenient where the schema is strict, and the split is deliberate.
:func:`~.schema.validate_config_document` is what tells an operator a profile is
malformed; this module is read on the dispatch path, where refusing to answer
because an unrelated profile is misspelled would take down work that has nothing
to do with it. So an unusable entry is skipped rather than raised on, and an
assignment missing its model keeps whatever else it declared — the caller's
fallback path then reports the missing piece by name, which is the outcome that
gets it fixed.

A profile is selected per project, which is what places it between the project
layer and the app layer in setting precedence: selecting a profile is a narrower
act than configuring the app, and a wider one than pinning a value on the project
itself. :func:`profile_pin` is the seam the effective-value resolver uses for that
layer, so profile pins are read through the same resolver as everything else
rather than through a second one that could disagree with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .schema import PROFILE_SETTING_KEYS, ROLES, SECTION_COST_PROFILES, SECTION_PROJECTS
from .settings import SETTINGS

#: Project field naming the profile that project selects.
PROJECT_PROFILE_FIELD = "cost_profile"

#: Key holding the role assignments inside a profile object.
ROLES_KEY = "roles"

#: Assignment fields. ``model`` is the one that carries a decision on its own;
#: ``agent`` and ``effort`` are optional refinements of it.
FIELD_AGENT = "agent"
FIELD_MODEL = "model"
FIELD_EFFORT = "effort"


@dataclass(frozen=True)
class RoleAssignment:
    """What one profile says about one role.

    ``agent`` and ``effort`` are empty when the profile does not pin them, and
    ``model`` is empty when the profile named the role without naming a model.
    Empty means "inherit" at every level rather than "no model": there is no
    spelling here for "run this role with nothing".
    """

    role: str
    model: str = ""
    agent: str = ""
    effort: str = ""
    #: Dotted configuration path of the assignment, for a surface that has to
    #: tell an operator where the decision was made.
    declared_at: str = ""

    @property
    def assigns_model(self) -> bool:
        return bool(self.model.strip())

    @property
    def assigns_agent(self) -> bool:
        return bool(self.agent.strip())


@dataclass(frozen=True)
class CostProfile:
    """One named profile: its role assignments and the settings it pins."""

    name: str
    assignments: Mapping[str, RoleAssignment] = field(default_factory=dict)
    #: Pinned settings, keyed by dotted setting key, holding the raw stored value.
    #: Raw rather than coerced because the setting registry owns coercion, and
    #: doing it twice is how two answers to "what is the ceiling" appear.
    pins: Mapping[str, Any] = field(default_factory=dict)
    declared_at: str = ""

    def assignment(self, role: str) -> RoleAssignment | None:
        """The assignment for *role*, or ``None`` when this profile omits it."""
        return self.assignments.get(role)

    def pin(self, key: str) -> tuple[bool, Any, str]:
        """Return ``(present, raw value, dotted path)`` for a pinned setting."""
        path = f"{self.declared_at}.{key}" if self.declared_at else key
        if key not in self.pins:
            return False, None, path
        return True, self.pins[key], path


def profiles(doc: Mapping[str, Any]) -> dict[str, CostProfile]:
    """Return every readable profile in *doc*, keyed by name."""
    section = doc.get(SECTION_COST_PROFILES)
    if not isinstance(section, Mapping):
        return {}
    parsed: dict[str, CostProfile] = {}
    for name, entry in section.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, Mapping):
            continue
        parsed[name] = _profile_from(name, entry, f"{SECTION_COST_PROFILES}.{name}")
    return parsed


def profile(doc: Mapping[str, Any], name: str) -> CostProfile | None:
    """Return the profile called *name*, or ``None`` when it is not defined."""
    return profiles(doc).get(name)


def selected_profile_name(doc: Mapping[str, Any], project: str | None) -> str:
    """Return the profile name *project* selects, empty when it selects none."""
    if project is None:
        return ""
    projects = doc.get(SECTION_PROJECTS)
    if not isinstance(projects, Mapping):
        return ""
    entry = projects.get(project)
    if not isinstance(entry, Mapping):
        return ""
    selected = entry.get(PROJECT_PROFILE_FIELD)
    return selected.strip() if isinstance(selected, str) else ""


def selected_profile(doc: Mapping[str, Any], project: str | None) -> tuple[CostProfile | None, str]:
    """Return ``(profile, selected name)`` for *project*.

    The name is returned even when the profile is missing, because "selected a
    profile that is not defined" and "selected nothing" are different mistakes
    with different fixes, and a caller that only got ``None`` could not tell them
    apart.
    """
    name = selected_profile_name(doc, project)
    if not name:
        return None, ""
    return profile(doc, name), name


def profile_pin(doc: Mapping[str, Any], key: str, project: str | None) -> tuple[bool, Any, str]:
    """Return ``(present, raw value, dotted path)`` for a profile-pinned setting.

    Absent unless *key* is one a profile may pin and the project's selected
    profile pins it, so the effective-value resolver can consult this layer
    unconditionally.
    """
    if key not in PROFILE_SETTING_KEYS:
        return False, None, key
    selected, _ = selected_profile(doc, project)
    if selected is None:
        return False, None, key
    return selected.pin(key)


def _profile_from(name: str, entry: Mapping[str, Any], path: str) -> CostProfile:
    roles_node = entry.get(ROLES_KEY)
    assignments: dict[str, RoleAssignment] = {}
    if isinstance(roles_node, Mapping):
        for role, assignment in roles_node.items():
            if role not in ROLES or not isinstance(assignment, Mapping):
                continue
            assignments[role] = RoleAssignment(
                role=role,
                model=_text(assignment.get(FIELD_MODEL)),
                agent=_text(assignment.get(FIELD_AGENT)),
                effort=_text(assignment.get(FIELD_EFFORT)),
                declared_at=f"{path}.{ROLES_KEY}.{role}",
            )
    return CostProfile(
        name=name,
        assignments=assignments,
        pins=_pins_from(entry),
        declared_at=path,
    )


def _pins_from(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Collect the settings a profile pins, ignoring keys it may not pin."""
    pinned: dict[str, Any] = {}
    for key in PROFILE_SETTING_KEYS:
        setting = SETTINGS[key]
        group = entry.get(setting.group)
        if isinstance(group, Mapping) and setting.leaf in group:
            pinned[key] = group[setting.leaf]
    return pinned


def _text(value: Any) -> str:
    """A stored string, or empty for anything that is not usable as one."""
    return value.strip() if isinstance(value, str) else ""
