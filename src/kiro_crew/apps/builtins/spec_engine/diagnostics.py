"""The app-side Doctor surface: the UI panel's server half.

The Doctor is one engine operation with two renderings. This module is the
rendering the Spec_Builder_UI reads, and it exists as its own thin layer for one
reason: the panel needs this app's *recorded readiness state*, and the engine may
not import the app root to get it. So the app reads its own state here and hands
it to :func:`~.engine.diagnosis.diagnose`, which is the only thing that assembles
a diagnostic.

The Engine_MCP_Server's doctor tool calls :func:`doctor_payload` too, through its
own dispatch. That is what makes "identical Findings from every surface" a
property of the code rather than a promise: there is one assembly, and the
surfaces differ only in the envelope they wrap it in.

Readiness is read through :func:`~.readiness.current`, whose absent and corrupt
cases both read as *not ready*. A panel therefore reports a half-registered app
as not operational long after the one-shot enable response has scrolled away,
which is the whole reason that state is recorded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import readiness
from .engine.config import ConfigStore
from .engine.diagnosis import diagnose
from .engine.doctor import DoctorHistory, DoctorReport
from .engine.state import state_root

__all__ = ["default_data_dir", "doctor_payload", "doctor_report"]


def default_data_dir() -> Path:
    """The app data directory the startup hook recorded readiness into.

    Derived from :func:`~.engine.state.state_root` rather than re-composed from
    the data home and the app name, so there is one spelling of where this app's
    data lives and a surface cannot look for the state in a directory nothing
    writes.
    """
    return state_root().parent


def doctor_report(
    *,
    data_dir: Path | str | None = None,
    config: ConfigStore | None = None,
    project: str | None = None,
    history: DoctorHistory | None = None,
) -> DoctorReport:
    """Run the Doctor for this app, with this app's registration state attached.

    *data_dir* is where the readiness state was recorded, which is the app's data
    directory as the host passes it to the startup hook. Absent and corrupt states
    both read as *not ready*, so a panel reports a half-registered app as not
    operational rather than defaulting to health nobody verified.
    """
    resolved = Path(data_dir) if data_dir is not None else default_data_dir()
    return diagnose(
        config if config is not None else ConfigStore(),
        registration=readiness.current(resolved),
        project=project,
        history=history,
    )


def doctor_payload(
    *,
    data_dir: Path | str | None = None,
    config: ConfigStore | None = None,
    project: str | None = None,
    history: DoctorHistory | None = None,
) -> dict[str, Any]:
    """The Doctor report as the JSON object both surfaces serve.

    One serialization, so a panel and a tool cannot render the same report into
    two different shapes and disagree about what the host said.
    """
    return doctor_report(
        data_dir=data_dir, config=config, project=project, history=history
    ).to_json_object()
