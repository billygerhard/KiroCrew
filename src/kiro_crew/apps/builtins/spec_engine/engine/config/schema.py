"""The configuration schema: sections, vocabularies, and document validation.

``validate_config_document`` is the gate the single write path runs before
anything reaches disk, and the same function the doctor calls to report
configuration problems as findings. One implementation, so a document the doctor
calls clean is exactly a document the write path would accept.

Validation is closed rather than permissive: an unknown key is an error, not a
key that is quietly kept. Configuration here carries autonomy levels, budget
ceilings, and the commands the app is allowed to run, so a misspelled key that
survives silently reads as an applied restriction that was never applied.

Two objects are deliberately **config-only** — no engine or tool call may
mutate them, only a configuration surface a human is looking at:

* the **autonomy policy** (per source: how far a run proceeds unattended), and
* the **delivery workflow** (the commands each delivery stage runs).

Both live under paths listed in ``CONFIG_ONLY_PATHS``. Capability bindings join
them: a provider bound at runtime by the thing it would be judging is not a
binding, it is an escalation. ``config_only_paths`` reports which of these a
patch touches so the write path can require an operator-confirmed surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from kiro_crew.effort import EFFORT_LEVELS

from .settings import SETTING_GROUPS, SETTINGS, Scope, Setting

#: Schema version of the persisted document. Bumped only when a migration ships.
VERSION_KEY = "version"
CURRENT_VERSION = 1

SECTION_CAPABILITIES = "capabilities"
SECTION_QUALITY_GATES = "quality_gates"
SECTION_COST_PROFILES = "cost_profiles"
SECTION_PROJECTS = "projects"
SECTION_SOURCES = "sources"
SECTION_WORKFLOW = "workflow"

SECTIONS: tuple[str, ...] = (
    SECTION_CAPABILITIES,
    SECTION_QUALITY_GATES,
    SECTION_COST_PROFILES,
    SECTION_PROJECTS,
    SECTION_SOURCES,
    SECTION_WORKFLOW,
)

#: Capability names that may be bound to an external provider.
DELEGABLE_CAPABILITIES: tuple[str, ...] = (
    "analysis",
    "authoring",
    "review",
    "implementation",
    "validation_rules",
    "watch_sources",
    "model_catalog",
)

#: Capabilities that always execute in the engine. Naming one in ``capabilities``
#: is refused rather than ignored: a delegated phase gate or claim ledger would
#: move the guarantees the engine exists to make outside the engine.
ENGINE_FLOOR_CAPABILITIES: tuple[str, ...] = (
    "format_validation",
    "phase_gates",
    "autonomy_resolution",
    "budget_enforcement",
    "claim_ledger",
    "audit_log",
)

#: How a delegated capability is reached.
TRANSPORTS: tuple[str, ...] = ("builtin", "mcp", "command")

#: Delivery stages, in the order the pipeline runs them by default. A workflow
#: may declare its own order so verify-class gates can sit before submit, after
#: it, or both.
DELIVERY_STAGES: tuple[str, ...] = ("isolate", "submit", "verify", "publish", "teardown")

#: Where a quality gate runs relative to raising the review artifact. ``both``
#: exists because a gate position is not a property of the check: an analyzer
#: worth running before a human sees the change is usually worth re-running on
#: the artifact, and expressing that as two gates would put one check in the
#: audit record under two names with two independently editable severities.
GATE_POSITION_PRE_SUBMIT = "pre_submit"
GATE_POSITION_POST_SUBMIT = "post_submit"
GATE_POSITION_BOTH = "both"
GATE_POSITIONS: tuple[str, ...] = (
    GATE_POSITION_PRE_SUBMIT,
    GATE_POSITION_POST_SUBMIT,
    GATE_POSITION_BOTH,
)

#: A blocking gate stops the flow and dispatches fix tasks; an advisory gate is
#: recorded and surfaced without stopping the run.
GATE_SEVERITY_BLOCKING = "blocking"
GATE_SEVERITY_ADVISORY = "advisory"
GATE_SEVERITIES: tuple[str, ...] = (GATE_SEVERITY_BLOCKING, GATE_SEVERITY_ADVISORY)

#: The autonomy ladder, least to most autonomous. Strictly ordered, and an
#: enabled level implies every level below it.
AUTONOMY_LEVELS: tuple[str, ...] = ("authoring", "execution", "delivery", "integration")

#: Trust classes for authored content, least trusted last. Derived per authored
#: element from that element's own author, never inherited from its container.
SUBMITTER_CLASSES: tuple[str, ...] = ("maintainer", "member", "contributor", "external")

#: The least-trusted class, used whenever an author cannot be determined.
LEAST_TRUSTED_CLASS = SUBMITTER_CLASSES[-1]

#: Spec document plans.
SPEC_TYPES: tuple[str, ...] = ("feature", "bugfix", "quick")

#: Work roles a cost profile may assign a model and effort to.
ROLES: tuple[str, ...] = ("design", "review", "implement", "analysis", "setup")

#: Settings a cost profile may pin beside its role assignments: the tasks it
#: dispatches at once and the credits one of its runs may spend. Both are
#: ordinary settings, so the registry keeps ownership of their types and bounds
#: and a profile cannot invent a limit the rest of the app does not understand.
PROFILE_SETTING_KEYS: tuple[str, ...] = (
    "concurrency.wave_max_tasks",
    "budget.run_ceiling_credits",
)

#: Setting groups a profile object may carry, derived from the keys above so the
#: two cannot disagree about which containers are accepted. A group outside this
#: set is still accepted as a container and refused leaf by leaf, so an operator
#: who pins the wrong setting is told which setting rather than which section.
PROFILE_SETTING_GROUPS: frozenset[str] = frozenset(
    key.split(".", 1)[0] for key in PROFILE_SETTING_KEYS
)

#: Points at which the app may write back to the tracker that supplied an item.
ITEM_LIFECYCLE_EVENTS: tuple[str, ...] = (
    "claimed",
    "awaiting_review",
    "delivery_submitted",
    "completed",
    "failed",
    "refused",
)

#: Wildcard key accepted wherever a policy is keyed by class or spec type.
WILDCARD_KEY = "default"

#: Fields a project entry may carry besides setting groups.
PROJECT_FIELDS: tuple[str, ...] = (
    "path",
    "cost_profile",
    "base_branch",
    "protected_branches",
    "variables",
    "intake",
    SECTION_WORKFLOW,
)

#: Fields a watch source entry may carry besides setting groups. These names and
#: ``SETTING_GROUPS`` must stay disjoint: a project or source entry holds both,
#: so a name in both would make one of them unreachable.
SOURCE_FIELDS: tuple[str, ...] = (
    "enabled",
    "poll",
    "field_map",
    "project",
    "base_branch",
    "preset",
    "maintainers",
    "spec_types",
    "autonomy",
    "intake",
    "spend_cap",
    "feedback",
)

#: Paths no engine or tool call may write. ``*`` matches one path segment.
CONFIG_ONLY_PATHS: tuple[str, ...] = (
    SECTION_SOURCES,
    SECTION_WORKFLOW,
    SECTION_CAPABILITIES,
    f"{SECTION_PROJECTS}.*.{SECTION_WORKFLOW}",
    # The second place in this document that holds argv the delivery pipeline
    # executes, on the run's workspace with the run's substituted variables. The
    # workflow above is fenced for exactly that reason, and leaving gates open
    # would not be a gap beside the fence but a way through it: a command refused
    # at a workflow stage is accepted as a gate and runs on the next delivery.
    # Declared advisory it would run and stop nothing, so the run still reports
    # passed.
    SECTION_QUALITY_GATES,
    # Intake guidance is text the engine puts in a headless run's seed beside the
    # watched item, so a tool that could write it could write the run's own
    # instructions. The source-level copy is already covered by the whole
    # ``sources`` section above; this is the project-level one.
    f"{SECTION_PROJECTS}.*.intake",
    # Not a whole section, but the same kind of thing as one. This switch
    # co-gates unattended integration into a protected destination alongside
    # the autonomy ladder, and integration is the one stage a mistake cannot
    # undo. A ladder that no tool can widen is worth little if the second gate
    # on the same action stays writable from a surface no operator confirmed.
    "delivery.auto_integrate",
    f"{SECTION_PROJECTS}.*.delivery.auto_integrate",
)


@dataclass(frozen=True)
class ConfigError:
    """One configuration problem, addressed by its dotted path in the document."""

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.path}: {self.message}"


class ConfigValidationError(ValueError):
    """Raised when a document, or a value read out of one, fails validation."""

    def __init__(self, errors: Iterable[ConfigError]) -> None:
        self.errors: tuple[ConfigError, ...] = tuple(errors)
        detail = "; ".join(str(e) for e in self.errors) or "invalid configuration"
        super().__init__(detail)


def config_only_paths(patch: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the config-only paths *patch* would write, in document order."""
    touched: list[str] = []
    for pattern in CONFIG_ONLY_PATHS:
        touched.extend(_matches(patch, pattern.split(".")))
    return tuple(sorted(set(touched)))


def _matches(node: Any, segments: Sequence[str], prefix: str = "") -> list[str]:
    if not isinstance(node, Mapping):
        return []
    head, rest = segments[0], segments[1:]
    keys = list(node.keys()) if head == "*" else ([head] if head in node else [])
    found: list[str] = []
    for key in keys:
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if not rest:
            found.append(path)
        else:
            found.extend(_matches(node[key], rest, path))
    return found


def validate_config_document(doc: Any) -> tuple[ConfigError, ...]:
    """Return every problem in *doc*, empty when the document is valid."""
    errors: list[ConfigError] = []
    if not isinstance(doc, Mapping):
        return (ConfigError("", "configuration must be an object"),)
    for key, value in doc.items():
        if not isinstance(key, str):
            errors.append(ConfigError(str(key), "configuration keys must be strings"))
        elif key == VERSION_KEY:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(ConfigError(key, "expected a positive integer"))
        elif key in SETTING_GROUPS:
            _check_group(errors, key, value, Scope.APP, key)
        elif key == SECTION_CAPABILITIES:
            _check_capabilities(errors, value, key)
        elif key == SECTION_QUALITY_GATES:
            _check_quality_gates(errors, value, key)
        elif key == SECTION_COST_PROFILES:
            _check_cost_profiles(errors, value, key)
        elif key == SECTION_WORKFLOW:
            _check_workflow(errors, value, key)
        elif key == SECTION_PROJECTS:
            _check_named(errors, value, key, _check_project)
        elif key == SECTION_SOURCES:
            _check_named(errors, value, key, _check_source)
        else:
            errors.append(ConfigError(key, "unknown configuration key"))
    return tuple(errors)


def stored_value(
    doc: Mapping[str, Any],
    setting: Setting,
    scope: Scope,
    *,
    project: str | None = None,
    source: str | None = None,
) -> tuple[bool, Any, str]:
    """Read *setting* at *scope*, returning ``(present, raw value, dotted path)``.

    The path is returned even when the value is absent so a caller reporting an
    invalid stored value can name where it lives.
    """
    if scope is Scope.APP:
        container: Any = doc
        prefix = ""
    elif scope is Scope.PROJECT:
        container = _entry(doc, SECTION_PROJECTS, project)
        prefix = f"{SECTION_PROJECTS}.{project}."
    else:
        container = _entry(doc, SECTION_SOURCES, source)
        prefix = f"{SECTION_SOURCES}.{source}."
    path = f"{prefix}{setting.key}"
    if not isinstance(container, Mapping):
        return False, None, path
    group = container.get(setting.group)
    if not isinstance(group, Mapping) or setting.leaf not in group:
        return False, None, path
    return True, group[setting.leaf], path


def _entry(doc: Mapping[str, Any], section: str, name: str | None) -> Any:
    if name is None:
        return None
    node = doc.get(section)
    if not isinstance(node, Mapping):
        return None
    return node.get(name)


# --- section validators ----------------------------------------------------


def _check_group(
    errors: list[ConfigError], group: str, value: Any, scope: Scope, path: str
) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object of settings"))
        return
    for leaf, raw in value.items():
        leaf_path = f"{path}.{leaf}"
        setting = SETTINGS.get(f"{group}.{leaf}")
        if setting is None:
            errors.append(ConfigError(leaf_path, "unknown setting"))
            continue
        if not setting.allows(scope):
            allowed = ", ".join(sorted(s.value for s in setting.scopes))
            errors.append(
                ConfigError(
                    leaf_path, f"not overridable at {scope.value} scope (allowed: {allowed})"
                )
            )
            continue
        try:
            setting.coerce(raw)
        except ValueError as exc:
            errors.append(ConfigError(leaf_path, str(exc)))


def _check_capabilities(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by capability"))
        return
    for name, binding in value.items():
        entry_path = f"{path}.{name}"
        if name in ENGINE_FLOOR_CAPABILITIES:
            errors.append(
                ConfigError(entry_path, "engine-floor capability cannot be bound to a provider")
            )
            continue
        if name not in DELEGABLE_CAPABILITIES:
            errors.append(ConfigError(entry_path, "unknown capability"))
            continue
        if not isinstance(binding, Mapping):
            errors.append(ConfigError(entry_path, "expected an object"))
            continue
        transport = binding.get("transport")
        if transport not in TRANSPORTS:
            errors.append(ConfigError(f"{entry_path}.transport", _one_of_message(TRANSPORTS)))
        for key in binding:
            if key not in ("transport", "command", "env", "timeout_s"):
                errors.append(ConfigError(f"{entry_path}.{key}", "unknown capability field"))
        if transport in ("mcp", "command"):
            _check_argv(errors, binding.get("command"), f"{entry_path}.command", required=True)
        elif "command" in binding:
            errors.append(
                ConfigError(
                    f"{entry_path}.command", "only the mcp and command transports take a command"
                )
            )
        if "env" in binding:
            _check_str_map(errors, binding["env"], f"{entry_path}.env")
        if "timeout_s" in binding:
            _check_positive_int(errors, binding["timeout_s"], f"{entry_path}.timeout_s")


def _check_quality_gates(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(ConfigError(path, "expected a list of gates"))
        return
    seen: set[str] = set()
    for index, gate in enumerate(value):
        entry_path = f"{path}[{index}]"
        if not isinstance(gate, Mapping):
            errors.append(ConfigError(entry_path, "expected an object"))
            continue
        for key in gate:
            if key not in ("name", "position", "severity", "commands"):
                errors.append(ConfigError(f"{entry_path}.{key}", "unknown gate field"))
        name = gate.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(ConfigError(f"{entry_path}.name", "expected a non-empty string"))
        elif name in seen:
            errors.append(ConfigError(f"{entry_path}.name", "duplicate gate name"))
        else:
            seen.add(name)
        if gate.get("position") not in GATE_POSITIONS:
            errors.append(ConfigError(f"{entry_path}.position", _one_of_message(GATE_POSITIONS)))
        if gate.get("severity") not in GATE_SEVERITIES:
            errors.append(ConfigError(f"{entry_path}.severity", _one_of_message(GATE_SEVERITIES)))
        _check_command_list(errors, gate.get("commands"), f"{entry_path}.commands")


def _check_cost_profiles(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by profile name"))
        return
    for name, profile in value.items():
        entry_path = f"{path}.{name}"
        if not isinstance(profile, Mapping):
            errors.append(ConfigError(entry_path, "expected an object"))
            continue
        for key in profile:
            if key == "roles":
                continue
            if key in SETTING_GROUPS:
                _check_profile_settings(errors, key, profile[key], f"{entry_path}.{key}")
            else:
                errors.append(ConfigError(f"{entry_path}.{key}", "unknown profile field"))
        roles = profile.get("roles")
        if not isinstance(roles, Mapping):
            errors.append(ConfigError(f"{entry_path}.roles", "expected an object keyed by role"))
            continue
        for role, assignment in roles.items():
            role_path = f"{entry_path}.roles.{role}"
            if role not in ROLES:
                errors.append(ConfigError(role_path, _one_of_message(ROLES)))
                continue
            if not isinstance(assignment, Mapping):
                errors.append(ConfigError(role_path, "expected an object"))
                continue
            for key in assignment:
                if key not in ("agent", "model", "effort"):
                    errors.append(ConfigError(f"{role_path}.{key}", "unknown role field"))
            model = assignment.get("model")
            if not isinstance(model, str) or not model.strip():
                errors.append(ConfigError(f"{role_path}.model", "expected a non-empty string"))
            if "agent" in assignment and not isinstance(assignment["agent"], str):
                errors.append(ConfigError(f"{role_path}.agent", "expected a string"))
            if "effort" in assignment and assignment["effort"] not in EFFORT_LEVELS:
                errors.append(ConfigError(f"{role_path}.effort", _one_of_message(EFFORT_LEVELS)))


def _check_profile_settings(errors: list[ConfigError], group: str, value: Any, path: str) -> None:
    """Validate the settings a profile pins beside its role assignments.

    Only the keys in :data:`PROFILE_SETTING_KEYS` may be pinned, and each one is
    coerced by its own registry entry. A profile that could pin any setting would
    become a second, undocumented configuration layer for limits the rest of the
    app resolves elsewhere.
    """
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object of settings"))
        return
    for leaf, raw in value.items():
        leaf_path = f"{path}.{leaf}"
        key = f"{group}.{leaf}"
        if key not in PROFILE_SETTING_KEYS:
            errors.append(
                ConfigError(
                    leaf_path,
                    "a cost profile may pin only: " + ", ".join(PROFILE_SETTING_KEYS),
                )
            )
            continue
        try:
            SETTINGS[key].coerce(raw)
        except ValueError as exc:
            errors.append(ConfigError(leaf_path, str(exc)))


def _check_workflow(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object"))
        return
    for key in value:
        if key not in ("preset", "stages"):
            errors.append(ConfigError(f"{path}.{key}", "unknown workflow field"))
    if "preset" in value and not isinstance(value["preset"], str):
        errors.append(ConfigError(f"{path}.preset", "expected a string"))
    stages = value.get("stages")
    if stages is None:
        return
    if not isinstance(stages, Mapping):
        errors.append(ConfigError(f"{path}.stages", "expected an object keyed by stage"))
        return
    for stage, commands in stages.items():
        stage_path = f"{path}.stages.{stage}"
        if stage not in DELIVERY_STAGES:
            errors.append(ConfigError(stage_path, _one_of_message(DELIVERY_STAGES)))
            continue
        _check_command_list(errors, commands, stage_path)


def _check_named(
    errors: list[ConfigError],
    value: Any,
    path: str,
    check: Any,
) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by name"))
        return
    for name, entry in value.items():
        entry_path = f"{path}.{name}"
        if not isinstance(name, str) or not name.strip():
            errors.append(ConfigError(entry_path, "name must be a non-empty string"))
            continue
        if not isinstance(entry, Mapping):
            errors.append(ConfigError(entry_path, "expected an object"))
            continue
        check(errors, entry, entry_path)


def _check_project(errors: list[ConfigError], entry: Mapping[str, Any], path: str) -> None:
    for key, value in entry.items():
        field_path = f"{path}.{key}"
        if key in SETTING_GROUPS:
            _check_group(errors, key, value, Scope.PROJECT, field_path)
        elif key == SECTION_WORKFLOW:
            _check_workflow(errors, value, field_path)
        elif key in ("path", "cost_profile", "base_branch"):
            if not isinstance(value, str) or not value.strip():
                errors.append(ConfigError(field_path, "expected a non-empty string"))
        elif key == "protected_branches":
            _check_str_list(errors, value, field_path)
        elif key == "variables":
            _check_str_map(errors, value, field_path)
        elif key == "intake":
            _check_intake(errors, value, field_path)
        else:
            errors.append(ConfigError(field_path, "unknown project field"))
    # A project must be locatable: every later phase (isolate, stage commands,
    # spec directory resolution) works from this path.
    if "path" not in entry:
        errors.append(ConfigError(f"{path}.path", "required project field is missing"))


def _check_source(errors: list[ConfigError], entry: Mapping[str, Any], path: str) -> None:
    for key, value in entry.items():
        field_path = f"{path}.{key}"
        if key in SETTING_GROUPS:
            _check_group(errors, key, value, Scope.SOURCE, field_path)
        elif key == "enabled":
            if not isinstance(value, bool):
                errors.append(ConfigError(field_path, "expected true or false"))
        elif key == "poll":
            _check_argv(errors, value, field_path, required=True)
        elif key == "field_map":
            _check_str_map(errors, value, field_path)
        elif key in ("project", "base_branch", "preset"):
            if not isinstance(value, str) or not value.strip():
                errors.append(ConfigError(field_path, "expected a non-empty string"))
        elif key == "maintainers":
            _check_str_list(errors, value, field_path)
        elif key == "spec_types":
            _check_spec_type_map(errors, value, field_path)
        elif key == "autonomy":
            _check_autonomy(errors, value, field_path)
        elif key == "intake":
            _check_intake(errors, value, field_path)
        elif key == "spend_cap":
            _check_spend_cap(errors, value, field_path)
        elif key == "feedback":
            _check_feedback(errors, value, field_path)
        else:
            errors.append(ConfigError(field_path, "unknown source field"))
    if "poll" not in entry:
        errors.append(ConfigError(f"{path}.poll", "required source field is missing"))


def _check_autonomy(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by submitter class"))
        return
    classes = set(SUBMITTER_CLASSES) | {WILDCARD_KEY}
    types = set(SPEC_TYPES) | {WILDCARD_KEY}
    for klass, by_type in value.items():
        class_path = f"{path}.{klass}"
        if klass not in classes:
            errors.append(ConfigError(class_path, _one_of_message(tuple(sorted(classes)))))
            continue
        if not isinstance(by_type, Mapping):
            errors.append(ConfigError(class_path, "expected an object keyed by spec type"))
            continue
        for spec_type, level in by_type.items():
            level_path = f"{class_path}.{spec_type}"
            if spec_type not in types:
                errors.append(ConfigError(level_path, _one_of_message(tuple(sorted(types)))))
            elif level not in AUTONOMY_LEVELS:
                errors.append(ConfigError(level_path, _one_of_message(AUTONOMY_LEVELS)))


def _check_spec_type_map(errors: list[ConfigError], value: Any, path: str) -> None:
    """Validate a classification-to-spec-type map, optionally keyed by class first.

    Two shapes are accepted because two things are being said. The flat shape
    (``bug: bugfix``) says what kind of work a classification is, which is a
    property of the tracker. The nested shape (``external: {bug: quick}``) says
    what kind of work the engine will *do* about it depending on who asked, which
    is a trust decision. An operator who needs only the first should not have to
    write a class dimension they have no rule for.
    """
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by classification"))
        return
    classes = set(SUBMITTER_CLASSES) | {WILDCARD_KEY}
    for key, entry in value.items():
        entry_path = f"{path}.{key}"
        if not isinstance(key, str) or not key.strip():
            errors.append(ConfigError(entry_path, "classification must be a non-empty string"))
        elif isinstance(entry, Mapping):
            # A nested object is only meaningful under a submitter class: nesting
            # under a classification would key the inner map by nothing.
            if key not in classes:
                errors.append(
                    ConfigError(
                        entry_path,
                        "a nested map must be keyed by submitter class, one of: "
                        + ", ".join(sorted(classes)),
                    )
                )
                continue
            _check_flat_spec_types(errors, entry, entry_path)
        elif entry not in SPEC_TYPES:
            errors.append(ConfigError(entry_path, _one_of_message(SPEC_TYPES)))


def _check_flat_spec_types(errors: list[ConfigError], value: Mapping[str, Any], path: str) -> None:
    for classification, spec_type in value.items():
        entry_path = f"{path}.{classification}"
        if not isinstance(classification, str) or not classification.strip():
            errors.append(ConfigError(entry_path, "classification must be a non-empty string"))
        elif spec_type not in SPEC_TYPES:
            errors.append(ConfigError(entry_path, _one_of_message(SPEC_TYPES)))


def _check_intake(errors: list[ConfigError], value: Any, path: str) -> None:
    """Validate intake guidance: one block of text per spec type.

    The guidance reaches a run's seed, so it is accepted only as text and only
    under a spec type the engine plans for. ``default`` covers every type.
    """
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by spec type"))
        return
    types = set(SPEC_TYPES) | {WILDCARD_KEY}
    for spec_type, guidance in value.items():
        entry_path = f"{path}.{spec_type}"
        if spec_type not in types:
            errors.append(ConfigError(entry_path, _one_of_message(tuple(sorted(types)))))
        elif not isinstance(guidance, str) or not guidance.strip():
            errors.append(ConfigError(entry_path, "expected non-empty guidance text"))


def _check_spend_cap(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object"))
        return
    for key in value:
        if key not in ("credits", "period_days"):
            errors.append(ConfigError(f"{path}.{key}", "unknown spend cap field"))
    credits = value.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits <= 0:
        errors.append(ConfigError(f"{path}.credits", "expected a positive number"))
    _check_positive_int(errors, value.get("period_days"), f"{path}.period_days")


def _check_feedback(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object keyed by lifecycle event"))
        return
    for event, commands in value.items():
        event_path = f"{path}.{event}"
        if event not in ITEM_LIFECYCLE_EVENTS:
            errors.append(ConfigError(event_path, _one_of_message(ITEM_LIFECYCLE_EVENTS)))
            continue
        _check_command_list(errors, commands, event_path)


# --- leaf helpers ----------------------------------------------------------


def _check_command_list(errors: list[ConfigError], value: Any, path: str) -> None:
    """Validate a list of argv templates.

    Commands are lists of argument templates rather than shell strings: the
    engine substitutes variables into argv elements and never hands a string to
    a shell, so the schema has no place to accept one.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(ConfigError(path, "expected a list of commands, each a list of arguments"))
        return
    if not value:
        errors.append(ConfigError(path, "expected at least one command"))
        return
    for index, command in enumerate(value):
        _check_argv(errors, command, f"{path}[{index}]", required=True)


def _check_argv(errors: list[ConfigError], value: Any, path: str, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(ConfigError(path, "expected a list of arguments"))
        return
    if not value:
        errors.append(ConfigError(path, "expected at least one argument"))
        return
    for index, argument in enumerate(value):
        if not isinstance(argument, str) or not argument:
            errors.append(ConfigError(f"{path}[{index}]", "expected a non-empty string"))


def _check_str_list(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(ConfigError(path, "expected a list of strings"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(ConfigError(f"{path}[{index}]", "expected a non-empty string"))


def _check_str_map(errors: list[ConfigError], value: Any, path: str) -> None:
    if not isinstance(value, Mapping):
        errors.append(ConfigError(path, "expected an object of strings"))
        return
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(ConfigError(path, "keys must be non-empty strings"))
        elif not isinstance(item, str):
            errors.append(ConfigError(f"{path}.{key}", "expected a string"))


def _check_positive_int(errors: list[ConfigError], value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(ConfigError(path, "expected a positive integer"))


def _one_of_message(choices: Iterable[str]) -> str:
    return "expected one of: " + ", ".join(choices)
