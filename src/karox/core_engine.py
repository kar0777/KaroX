"""The vendored karo-core engine as a product runtime.

karo-core is the fork that was measured in the harness race (stage A v4:
8/11 against stock mini's 6/11 on the same model, same grader, equal input
tokens). This module is the product's single entry point into it: it loads the
engine's own packaged protocol (system and instance templates), passes KaroX's
provider settings and an optional policy-aware command runner, and returns a
plain report.

The report deliberately has no VERIFIED state. The engine submits a workspace;
it does not prove the workspace. KaroX's verify discipline stays in the legacy
runtime until the two are reconciled, so nothing here can turn a model's prose
into a verification claim.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# The engine prints a startup banner and loads a global dotenv on import. A
# product import must be silent and must not adopt another tool's config file.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import yaml  # noqa: E402

from .engine import __version__ as engine_version  # noqa: E402
from .engine import package_dir  # noqa: E402
from .engine.agents.default import DefaultAgent  # noqa: E402
from .engine.environments.local import LocalEnvironment, clip_output  # noqa: E402
from .engine.exceptions import LimitsExceeded, Submitted, TimeExceeded  # noqa: E402
from .engine.models.litellm_model import LitellmModel  # noqa: E402

#: A policy-aware command executor: (command, cwd, timeout) -> observation dict
#: with ``output`` and ``returncode``. When absent the engine runs the command
#: itself, exactly as it did in the race.
CommandRunner = Callable[[str, Path, int], dict[str, Any]]
ModelFactory = Callable[["CoreEngineConfig"], Any]

#: The engine's own exit labels, mapped onto the report the CLI prints.
_STATUS_BY_EXIT = {
    "Submitted": "submitted",
    "LimitsExceeded": "limits_exceeded",
    "TimeExceeded": "wall_time_limit",
    "RepeatedFormatError": "format_errors_exhausted",
}


@dataclass(frozen=True)
class CoreEngineConfig:
    repository: Path
    task: str
    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    max_steps: int = 100
    max_seconds: int = 1800
    output_char_limit: int = 50_000
    context_token_budget: int = 160_000
    command_timeout: int = 300
    trajectory_path: Optional[Path] = None
    command_runner: Optional[CommandRunner] = None
    model_factory: Optional[ModelFactory] = None

    @property
    def litellm_model_name(self) -> str:
        """OpenAI-compatible routes need the provider prefix; named ones do not."""
        return f"openai/{self.model}" if self.base_url else self.model


@dataclass
class CoreEngineReport:
    status: str
    submission: str
    steps: int
    context_events: int
    wall_seconds: float
    trajectory_path: Optional[str]
    engine_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "submission": self.submission,
            "steps": self.steps,
            "context_events": self.context_events,
            "wall_seconds": self.wall_seconds,
            "trajectory_path": self.trajectory_path,
            "engine_version": self.engine_version,
        }


class _RunnerLocalEnvironment(LocalEnvironment):
    """LocalEnvironment whose shell goes through an injected runner."""

    def __init__(self, *, runner: CommandRunner, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._runner = runner

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        workdir = cwd or self.config.cwd or os.getcwd()
        output: dict[str, Any]
        try:
            raw = self._runner(command, Path(workdir), timeout or self.config.timeout)
            output = {
                "output": str(raw.get("output") or ""),
                "returncode": int(raw.get("returncode", -1)),
                "exception_info": str(raw.get("exception_info") or ""),
            }
        except Exception as exc:  # noqa: BLE001 - an executor failure is an observation
            output = {
                "output": "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
            }
        output["output"] = clip_output(output["output"], self.config.output_char_limit)
        self._check_finished(output)
        return output


class CoreEngine:
    """Run one task with the vendored karo-core loop."""

    def __init__(self, config: CoreEngineConfig) -> None:
        if not config.repository.is_dir():
            raise ValueError(f"repository is not a directory: {config.repository}")
        if config.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if config.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        self.config = config
        self._base = yaml.safe_load(
            (package_dir / "config" / "default.yaml").read_text(encoding="utf-8")
        )

    # -- construction -----------------------------------------------------

    def _model(self) -> Any:
        if self.config.model_factory is not None:
            return self.config.model_factory(self.config)
        model_cfg = dict(self._base.get("model") or {})
        model_cfg.pop("model_name", None)
        model_kwargs = dict(model_cfg.pop("model_kwargs", {}) or {})
        model_kwargs["max_tokens"] = int(model_kwargs.get("max_tokens") or 32_768)
        if self.config.base_url:
            model_kwargs["api_base"] = self.config.base_url
        if self.config.api_key:
            model_kwargs["api_key"] = self.config.api_key
        # The engine counts cost through litellm's registry. A private alias is
        # not in it, and a missing price must not stop a run.
        model_cfg["cost_tracking"] = "ignore_errors"
        return LitellmModel(
            model_name=self.config.litellm_model_name,
            model_kwargs=model_kwargs,
            **model_cfg,
        )

    def _environment(self) -> LocalEnvironment:
        options: dict[str, Any] = {
            "cwd": str(self.config.repository),
            "timeout": self.config.command_timeout,
            "output_char_limit": self.config.output_char_limit,
        }
        if self.config.command_runner is None:
            return LocalEnvironment(**options)
        return _RunnerLocalEnvironment(runner=self.config.command_runner, **options)

    def _agent(self, model: Any, env: LocalEnvironment) -> DefaultAgent:
        options = dict(self._base.get("agent") or {})
        options.update(
            {
                "step_limit": self.config.max_steps,
                "wall_time_limit_seconds": self.config.max_seconds,
                "cost_limit": 0.0,
                "context_token_budget": self.config.context_token_budget,
                "output_path": self.config.trajectory_path,
            }
        )
        return DefaultAgent(model, env, **options)

    # -- run --------------------------------------------------------------

    def run(self) -> CoreEngineReport:
        started = time.time()
        agent = self._agent(self._model(), self._environment())
        status = "stopped"
        try:
            exit_extra = agent.run(task=self.config.task)
            # The engine turns its own budget limits into an exit message
            # instead of raising, so the status has to come from that message.
            status = _STATUS_BY_EXIT.get(str((exit_extra or {}).get("exit_status") or ""), "stopped")
        except Submitted:
            status = "submitted"
        except TimeExceeded:
            status = "wall_time_limit"
        except LimitsExceeded:
            status = "limits_exceeded"
        except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
            status = f"error: {type(exc).__name__}: {exc}"
        trajectory = None
        if self.config.trajectory_path is not None:
            saved = agent.save(self.config.trajectory_path)
            trajectory = str(saved.get("info", {}).get("config", {}).get("agent", {}).get("output_path") or self.config.trajectory_path)
        last = agent.messages[-1] if agent.messages else {}
        extra = last.get("extra") or {}
        return CoreEngineReport(
            status=status,
            submission=str(extra.get("submission") or last.get("content") or ""),
            steps=agent.n_calls,
            context_events=agent.n_context_events,
            wall_seconds=round(time.time() - started, 1),
            trajectory_path=trajectory,
            engine_version=engine_version,
        )