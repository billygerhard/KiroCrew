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
* :mod:`.integration` holds the integration floor: the protected branch set, the
  workflow ceiling on autonomy, and the two independent gates an unattended merge
  must both pass.
* :mod:`.isolation` gives each run a working tree of its own and refuses to hand
  one tree to two runs.
* :mod:`.flow` orders the stages — isolate before execution, publish only after
  verification, fix rounds bounded by the retry limit.
"""

from __future__ import annotations

from .flow import (
    DELIVERY_FLOW_STAGES,
    EVENT_FIX_DISPATCH,
    EVENT_INTEGRATION,
    EVENT_PUBLISHED,
    EVENT_STAGE,
    MAX_ADDRESS_CHARS,
    MAX_DEPLOYMENT_ADDRESSES,
    PUBLISH_STAGE,
    SUBMIT_STAGE,
    VERIFY_RETRY_LIMIT_SETTING,
    VERIFY_STAGE,
    AuditRecorder,
    DeliveryOutcome,
    DeliveryPipeline,
    DeliveryRun,
    FixDispatch,
    FixTaskDispatcher,
    VerifyAttempt,
)
from .integration import (
    PROTECTED_BRANCHES_FIELD,
    REASON_DELIVERY_FAILED,
    REASON_LADDER,
    REASON_NO_TARGET,
    REASON_POSTURE,
    REASON_VERIFY,
    DeliveryAuthority,
    IntegrationDecision,
    ProtectedBranches,
    evaluate_integration,
    resolve_authority,
    resolve_protected_branches,
)
from .isolation import (
    BRANCH_PREFIX,
    GIT_ISOLATE_COMMANDS,
    ISOLATED_PATH_VARIABLE,
    MAX_SLUG_CHARS,
    WORKTREE_KIND,
    WorkspaceBroker,
    WorkspaceClaim,
    WorkspaceLedger,
    WorkspacePlan,
    git_isolate_commands,
    isolated_context,
    plan_workspace,
    slugify,
)
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
    "BRANCH_PREFIX",
    "DELIVERY_FLOW_STAGES",
    "EVENT_FIX_DISPATCH",
    "EVENT_INTEGRATION",
    "EVENT_PUBLISHED",
    "EVENT_STAGE",
    "GIT_ISOLATE_COMMANDS",
    "ISOLATED_PATH_VARIABLE",
    "ISOLATE_STAGE",
    "MAX_ADDRESS_CHARS",
    "MAX_CAPTURED_CHARS",
    "MAX_DEPLOYMENT_ADDRESSES",
    "MAX_SLUG_CHARS",
    "PROTECTED_BRANCHES_FIELD",
    "PUBLISH_STAGE",
    "REASON_DELIVERY_FAILED",
    "REASON_LADDER",
    "REASON_NO_TARGET",
    "REASON_POSTURE",
    "REASON_VERIFY",
    "RUN_CONTEXT_VARIABLES",
    "STAGES_KEY",
    "STAGE_TIMEOUT_SETTING",
    "SUBMIT_STAGE",
    "TRUNCATION_NOTICE",
    "VARIABLES_KEY",
    "VARIABLE_NAME_PATTERN",
    "VERIFY_RETRY_LIMIT_SETTING",
    "VERIFY_STAGE",
    "WORKTREE_KIND",
    "ZERO_CONFIG_AUTONOMY_CEILING",
    "ArgumentTemplate",
    "AuditRecorder",
    "CommandOutcome",
    "CommandResult",
    "CommandRunner",
    "CommandTemplate",
    "DeliveryAuthority",
    "DeliveryOutcome",
    "DeliveryPipeline",
    "DeliveryRun",
    "DeliveryWorkflow",
    "FixDispatch",
    "FixTaskDispatcher",
    "IntegrationDecision",
    "MissingVariableError",
    "ProtectedBranches",
    "RunContext",
    "StageCommands",
    "StageExecutor",
    "StageOutcome",
    "StageResult",
    "TemplateError",
    "VariableError",
    "VariableRef",
    "VerifyAttempt",
    "WorkspaceBroker",
    "WorkspaceClaim",
    "WorkspaceLedger",
    "WorkspacePlan",
    "build_variables",
    "cap_autonomy",
    "evaluate_integration",
    "git_isolate_commands",
    "has_value",
    "isolated_context",
    "plan_workspace",
    "resolve_authority",
    "resolve_protected_branches",
    "run_argv",
    "slugify",
]
