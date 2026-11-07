"""Detached dev-command/check job tools."""

from __future__ import annotations

from typing import Any, Dict, List
from .command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from .core import (
    InvalidCommand,
)
from .hot_worker import hot_worker_supervisor
from .models import EvidenceRecord


class CoreJobToolsMixin:
    """Methods composed into ExtendedCoreRuntime; see core_tools.py."""

    def _repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_repo(self, arguments, deadline_seconds)

    def _prepare_dev_command(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> tuple[List[str], float]:
        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list):
            raise InvalidCommand("dev.command argv must be an array")
        # Full developer access broadens the local development surface; it does
        # not remove KaroX's global no-push/no-publish/no-auth/no-deploy
        # invariants. Keep one shared guard with durable command jobs so a long
        # command cannot gain powers that the synchronous path would refuse.
        try:
            argv = list(validate_developer_command_argv(raw_argv))
        except DeveloperCommandBlocked as exc:
            raise InvalidCommand(str(exc)) from exc

        raw_timeout = arguments.get("timeout_seconds", deadline_seconds)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise InvalidCommand("dev.command timeout_seconds must be a number")
        timeout = float(raw_timeout)
        if not 0 < timeout <= float(deadline_seconds):
            raise InvalidCommand("dev.command timeout_seconds must be positive and within the request deadline")
        return argv, timeout

    def _dev_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        argv, timeout = self._prepare_dev_command(arguments, deadline_seconds)
        result = self._run(argv, timeout, inherit_environment=True)
        display_argv = result["argv"]
        result["_evidence"] = [
            EvidenceRecord(
                kind="command",
                summary=("Passed" if result.get("exit_code") == 0 else "Failed")
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result.get("exit_code"),
                metadata={
                    "timed_out": bool(result.get("timed_out")),
                    "repository_scoped": True,
                    "shell": False,
                },
            )
        ]
        return result

    def _tests_run(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_tests(self, arguments, deadline_seconds)

    def _job_store(self) -> Any:
        from .command_jobs import CommandJobStore, repository_job_root
        from .paths import runtime_dir

        return CommandJobStore(repository_job_root(runtime_dir(), self.repository))

    @staticmethod
    def _job_launch_argv(argv: List[str]) -> List[str]:
        from .core_process import _is_karox_module_command, _karox_module_launch
        from .process_launcher import resolve_executable

        source = _karox_module_launch(argv) if _is_karox_module_command(argv) else list(argv)
        return resolve_executable(source)

    def _prepare_dev_command_start(self, arguments: Dict[str, Any]) -> tuple[List[str], float]:
        from .command_jobs import job_timeout

        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv:
            raise InvalidCommand("dev.command_start argv must be a non-empty array")
        try:
            argv = list(validate_developer_command_argv(raw_argv))
        except DeveloperCommandBlocked as exc:
            raise InvalidCommand(str(exc)) from exc
        return argv, job_timeout(arguments.get("timeout_seconds"))

    def _dev_command_start(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        import os

        del deadline_seconds
        argv, timeout = self._prepare_dev_command_start(arguments)
        # Same environment as dev.command: Full developer access behaves like
        # a terminal this KaroX process opened.
        environment = os.environ.copy()
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        payload = self._job_store().start(
            kind="dev",
            request_id=arguments.get("request_id"),
            argv=argv,
            launch_argv=self._job_launch_argv(argv),
            cwd=self.repository,
            timeout_seconds=timeout,
            environment=environment,
        )
        payload["next"] = "poll dev.command_status or dev.command_logs with this job_id"
        return payload

    def _prepare_checks_start(self, arguments: Dict[str, Any]) -> tuple[List[str], float]:
        from .command_jobs import MAX_TIMEOUT_SECONDS, job_timeout

        plan = self._prepare_check({"argv": arguments.get("argv")}, MAX_TIMEOUT_SECONDS)
        if plan.probe:
            raise InvalidCommand(
                "checks.start runs only the approved verification commands; run "
                "a probe with checks.run"
            )
        if plan.inline_code is not None:
            raise InvalidCommand(
                "checks.start does not run inline interpreter code; write the snippet "
                "to a scratch file in the repository and start '<interpreter> <file>'"
            )
        return list(plan.argv), job_timeout(arguments.get("timeout_seconds"))

    def _checks_start(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        from .core_process import _is_karox_module_command
        from .security import child_process_environment

        del deadline_seconds
        argv, timeout = self._prepare_checks_start(arguments)
        environment = child_process_environment()
        if _is_karox_module_command(argv):
            environment["PYTHONSAFEPATH"] = "1"
        payload = self._job_store().start(
            kind="check",
            request_id=arguments.get("request_id"),
            argv=argv,
            launch_argv=self._job_launch_argv(argv),
            cwd=self.repository,
            timeout_seconds=timeout,
            environment=environment,
        )
        payload["next"] = "poll checks.status or checks.logs with this job_id"
        return payload

    def _job_status(self, kind: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        job_id = arguments.get("job_id")
        store = self._job_store()
        if job_id is None:
            return {"jobs": store.list(kind=kind)}
        return store.status(job_id, kind=kind)

    def _dev_command_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_status("dev", arguments)

    def _checks_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_status("check", arguments)

    def _job_logs(self, kind: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from .command_jobs import DEFAULT_LOG_LIMIT

        return self._job_store().logs(
            arguments.get("job_id"),
            kind=kind,
            limit=arguments.get("limit", DEFAULT_LOG_LIMIT),
            offset=arguments.get("offset"),
        )

    def _dev_command_logs(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_logs("dev", arguments)

    def _checks_logs(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_logs("check", arguments)

    def _dev_command_cancel(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_store().cancel(arguments.get("job_id"), kind="dev")

    def _checks_cancel(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        return self._job_store().cancel(arguments.get("job_id"), kind="check")

    def _runtime_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        if arguments:
            raise InvalidCommand("runtime.status takes no arguments")
        return hot_worker_supervisor().status()

    def _prepare_repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> None:
        del deadline_seconds
        action = arguments.get("action")
        payload = arguments.get("payload")
        shellish = sorted(
            key
            for key in ("command", "argv", "cmd", "shell", "script", "scripts")
            if key in arguments or (isinstance(payload, dict) and key in payload)
        )
        if shellish:
            raise InvalidCommand(
                "repo.command is a patch/batch tool, not a shell; it never runs "
                f"commands. Remove {', '.join(shellish)} and use checks.run for the "
                "approved verification, tests.run for extra tests, or dev.command "
                "for a developer command. Batch shape: action=batch with "
                'payload={"operations":[{"op":"write|delete|move|mkdir", ...}]}.'
            )
        if action not in {"apply_patch", "batch"} or not isinstance(payload, dict):
            raise InvalidCommand(
                "repo.command requires action=apply_patch or action=batch with an "
                'object payload. apply_patch takes {"patch": "..."}; batch takes '
                '{"operations": [{"op": "write|delete|move|mkdir", ...}]}. This '
                "tool edits files; it is not a shell. For a command use "
                "checks.run, tests.run, or dev.command."
            )
        try:
            hot_worker_supervisor().validate_repo(arguments)
        except InvalidCommand as exc:
            # The worker already names the offending field. Restating the one
            # canonical shape here stops the next call from being another
            # invented key: the benchmark run sent 37 payload variants, each
            # refused with only the field name it guessed wrong.
            if action == "apply_patch":
                # A patch failure is about hunk counts or one diff; telling the
                # model about the batch shape here sent a 3D run into retrying
                # the wrong form with the wrong guidance.
                raise InvalidCommand(
                    f"{exc} apply_patch takes one unified diff in "
                    'payload.patch; fix the @@ hunk counts instead of changing '
                    'the tool shape. Use action=batch only when you mean '
                    '{"operations": [...]}.'
                ) from exc
            raise InvalidCommand(
                f"{exc} The only batch shape is action=batch with payload="
                '{"operations": [{"op": "write|delete|move|mkdir", ...}], '
                '"dry_run": false}; an operation accepts op, path, content, '
                "source, destination, expected_sha256, missing_ok, overwrite "
                "or allow_secret_literal, depending on op."
            ) from exc

    @staticmethod
    def _prepare_tests_run(arguments: Dict[str, Any]) -> None:
        if arguments.get("suite", "focused") not in {"focused", "full", "split"}:
            raise InvalidCommand("tests.run suite must be focused, full, or split")
