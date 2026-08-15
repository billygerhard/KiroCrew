"""Run context variables, and the custom variables a project adds to them.

A stage command is written against names, not positions, so the same configured
workflow serves every run. The run context supplies what the engine knows about
the run; a project adds its own names for whatever its commands need that the
engine has no opinion about (a deployment environment, a reviewer group, an
artifact bucket).

The two sets are not peers. Run context names are reserved: a project variable
called ``branch_name`` would silently redirect every push in the workflow, which
is exactly the kind of override that reads as harmless in a configuration file.
A collision is refused by name rather than resolved by precedence, because
either precedence order is a trap for somebody.

Blank values are dropped rather than carried. That is what turns an unset item
identifier into "this variable has no value", which the template layer refuses
to render, instead of an empty argument a program interprets on its own terms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .templates import VARIABLE_NAME_PATTERN

#: Variable names the engine owns. A project may not redefine these.
RUN_CONTEXT_VARIABLES: tuple[str, ...] = (
    "spec_name",
    "spec_type",
    "workspace_path",
    "isolated_path",
    "base_branch",
    "branch_name",
    "item_id",
    "item_url",
    "review_title",
    "review_summary",
    "review_url",
)


class VariableError(ValueError):
    """Raised when the variable set for a run cannot be built."""


@dataclass(frozen=True)
class RunContext:
    """What the engine knows about one run, as stage command variables.

    Every field beyond the spec identity defaults to empty, because they are
    genuinely absent in ordinary cases: an interactive run has no triggering
    item, and a workflow that raises no review artifact has no review title. An
    absent field is not an error here. It becomes one only when a stage command
    actually references it, and then it is reported as that stage refusing to
    run rather than as a command that ran with a piece missing.
    """

    spec_name: str
    spec_type: str
    workspace_path: str
    #: Workspace the isolate stage creates for this run, empty until one is
    #: planned. Kept apart from ``workspace_path``, which stays the tree the
    #: stage commands run *in*: a worktree is added by the repository that will
    #: hold it, and a run with no isolated workspace must not silently render a
    #: command against the project's own tree.
    isolated_path: str = ""
    base_branch: str = ""
    branch_name: str = ""
    item_id: str = ""
    item_url: str = ""
    review_title: str = ""
    review_summary: str = ""
    #: Address of the review artifact this run's submit stage raised, learned
    #: from what that command printed. Empty until a submit actually raised one:
    #: a link-artifact writeback names the artifact, and a run with no artifact
    #: has no link to give, so the variable is absent rather than a branch name
    #: standing in for a URL.
    review_url: str = ""

    def to_variables(self) -> dict[str, str]:
        """Return the non-blank run context values, keyed by variable name."""
        candidates = {
            "spec_name": self.spec_name,
            "spec_type": self.spec_type,
            "workspace_path": self.workspace_path,
            "isolated_path": self.isolated_path,
            "base_branch": self.base_branch,
            "branch_name": self.branch_name,
            "item_id": self.item_id,
            "item_url": self.item_url,
            "review_title": self.review_title,
            "review_summary": self.review_summary,
            "review_url": self.review_url,
        }
        return {name: value for name, value in candidates.items() if value and value.strip()}


def build_variables(
    context: RunContext,
    custom: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the variable set for a run: run context plus custom project names.

    Raises ``VariableError`` for a custom name that collides with a run context
    name or that no template could reference, since a variable nothing can name
    is configuration that looks applied and is not.
    """
    values = context.to_variables()
    for name, raw in (custom or {}).items():
        if name in RUN_CONTEXT_VARIABLES:
            raise VariableError(
                f"project variable {name!r} collides with a run context variable; "
                "choose another name"
            )
        if not isinstance(name, str) or not VARIABLE_NAME_PATTERN.fullmatch(name):
            raise VariableError(
                f"project variable {name!r} is not a name a command template can reference"
            )
        if not isinstance(raw, str):
            raise VariableError(f"project variable {name!r} must be a string")
        if raw.strip():
            values[name] = raw
    return values
