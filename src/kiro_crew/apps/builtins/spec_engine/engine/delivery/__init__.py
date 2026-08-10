"""Delivery: running the Delivery_Workflow's configured stage commands.

A workflow is configuration, not plugin code: each stage maps to a list of argv
templates, and the engine substitutes run variables into them and runs them with
no shell. That is what lets a pull-request workflow, an organization's own review
system, and a plain local build all be the same mechanism.

The module boundaries follow the trust boundary:

* :mod:`.templates` parses templates once and substitutes values as single argv
  elements. This is where attacker-authored text is made inert.
* :mod:`.variables` assembles a run's variable set from the run context plus the
  project's custom names.
* :mod:`.workflow` resolves which commands a stage runs, from which
  configuration layer, and answers whether a project configured a workflow at
  all — the zero-configuration case that caps autonomy at execution.
* :mod:`.stages` validates a whole stage, then executes it.
"""

from __future__ import annotations

from .stages import (
    MAX_CAPTURED_CHARS,
    STAGE_TIMEOUT_SETTING,
    TRUNCATION_NOTICE,
    CommandOutcome,
    CommandResult,
    CommandRunner,
    StageExecutor,
    StageOutcome,
    StageResult,
    run_argv,
)
from .templates import (
    VARIABLE_NAME_PATTERN,
    ArgumentTemplate,
    CommandTemplate,
    MissingVariableError,
    TemplateError,
    VariableRef,
    has_value,
)
from .variables import RUN_CONTEXT_VARIABLES, RunContext, VariableError, build_variables
from .workflow import (
    ISOLATE_STAGE,
    STAGES_KEY,
    VARIABLES_KEY,
    ZERO_CONFIG_AUTONOMY_CEILING,
    DeliveryWorkflow,
    StageCommands,
    cap_autonomy,
)

__all__ = [
    "ISOLATE_STAGE",
    "MAX_CAPTURED_CHARS",
    "RUN_CONTEXT_VARIABLES",
    "STAGES_KEY",
    "STAGE_TIMEOUT_SETTING",
    "TRUNCATION_NOTICE",
    "VARIABLES_KEY",
    "VARIABLE_NAME_PATTERN",
    "ZERO_CONFIG_AUTONOMY_CEILING",
    "ArgumentTemplate",
    "CommandOutcome",
    "CommandResult",
    "CommandRunner",
    "CommandTemplate",
    "DeliveryWorkflow",
    "MissingVariableError",
    "RunContext",
    "StageCommands",
    "StageExecutor",
    "StageOutcome",
    "StageResult",
    "TemplateError",
    "VariableError",
    "VariableRef",
    "build_variables",
    "cap_autonomy",
    "has_value",
    "run_argv",
]
