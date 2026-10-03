"""Thin Harbor adapter for the KaroX agent (the fourth harness arm).

Harbor runs agents INSIDE the task container. This adapter therefore:

1. uploads the ``karox`` wheel (built from the working tree) into the container
   and installs it for the agent user;
2. configures the race gateway as an ``openai_compatible_chat`` provider via
   environment variables only — no StepFun credentials ever enter the container,
   only the local gateway key;
3. runs ``karox agent run`` non-interactively against ``/app`` (the task
   working directory convention for Harbor tasks), streaming its log to the
   agent logs dir like every other installed agent.

Import from Harbor with a custom-agent import path, e.g.:

    harbor run -a "/mnt/d/проекты/KaroX-v5/bench/harness_race/harbor_agents/karo_agent.py:Karo@"

(see harbor run --help: ``--agent module.path:ClassName``).
"""
from __future__ import annotations

import ipaddress
import json
import shlex
from typing import Union
from pathlib import Path
from typing import Annotated, override
from urllib.parse import urlsplit

from pydantic import Field

try:  # inside Harbor (WSL)
    from harbor.agents.capabilities import AgentCapabilities
    from harbor.agents.installed.base import BaseInstalledAgent
    from harbor.agents.options import Cli, InstalledAgentOptions
    from harbor.agents.model_connection import ModelConnectionSpec
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
except ImportError as exc:  # outside Harbor: keep the module importable for tests
    _IMPORT_ERROR = exc

    class BaseInstalledAgent:  # type: ignore[no-redef]
        def __init_subclass__(cls, **kwargs):
            raise ImportError(f"harbor is not importable on this host: {_IMPORT_ERROR}")


#: Default container-side paths.
_WHEEL_TARGET = "/tmp/karox_runtime-5.0.0rc4-py3-none-any.whl"
_AGENT_LOG = "/logs/agent/karo.txt"
_VENV = "/tmp/karo-venv"
_PY = _VENV + "/bin/python"
_RELAY_PORT = 3999  # loopback relay inside the container, when needed
_RELAY_PATH = "/tmp/karo_relay.py"

#: Resolve the container's own default gateway (Harbor may use any docker
#: network, so 172.17.0.1 must not be assumed) from /proc/net/route.
_GW_RESOLVER = (
    "python3 -c \""
    "r = open('/proc/net/route').read().splitlines(); "
    "l = [x.split() for x in r if x.split() and x.split()[1] == '00000000'][0]; "
    "print('.'.join(str(int(l[2][i:i+2], 16)) for i in (6, 4, 2, 0)))\""
)

#: Karo refuses provider credentials over anything but HTTPS or loopback HTTP
#: (a real credential-guard, not to be bypassed). A container cannot reach the
#: host gateway via its own loopback, so when the gateway address is not
#: loopback we start THIS tiny asyncio relay inside the container: Karo speaks
#: to 127.0.0.1:<port> (loopback, policy satisfied), the relay forwards raw
#: bytes to the gateway over the docker bridge. No credentials are altered,
#: logged, or exposed to any shared segment.
_RELAY_SCRIPT = r'''
import asyncio, sys

LISTEN_HOST, LISTEN_PORT = "127.0.0.1", int(sys.argv[1])
TARGET_HOST, TARGET_PORT = sys.argv[2].rsplit(":", 1)
TARGET_PORT = int(TARGET_PORT)


async def pump(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except OSError:
        pass
    finally:
        try:
            # propagate a half-close only: the response must keep flowing
            # after the request side EOFs
            writer.write_eof()
        except (OSError, NotImplementedError):
            try:
                writer.close()
            except OSError:
                pass


async def handle(reader, writer):
    try:
        ur, uw = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError:
        writer.close(); return
    try:
        await asyncio.gather(pump(reader, uw), pump(ur, writer))
    finally:
        for w in (writer, uw):
            try: w.close()
            except OSError: pass


async def main():
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


asyncio.run(main())
'''

#: Environment variables the controller hands to the adapter (--agent-env).
#: GATEWAY_BASE_URL must be reachable FROM the task container (the WSL docker
#: bridge address when the gateway runs inside WSL).
_ENV_GATEWAY_URL = "KARO_RACE_GATEWAY_URL"
_ENV_GATEWAY_KEY = "KARO_RACE_GATEWAY_KEY"


class KaroOptions(InstalledAgentOptions):
    max_steps: Annotated[int | None, Cli("--max-steps")] = Field(
        default=None, description="Step budget for karox agent run."
    )
    max_seconds: Annotated[int | None, Cli("--max-seconds")] = Field(
        default=None, description="Wall-clock budget for karox agent run."
    )
    effort_level: Annotated[str | None, Cli("--effort-level")] = Field(
        default="high", description="Karo effort ladder level."
    )
    semantic_shadow: Annotated[bool, Cli("--semantic-shadow")] = Field(
        default=False,
        description="Enable KaroX semantic-shadow telemetry for diagnostic runs.",
    )
    task_dir: Annotated[str | None, Cli("--task-dir")] = Field(
        default="/app", description="Working directory of the task inside the container."
    )
    verify_command: Annotated[
        Union[str, list[str], list[list[str]], None], Cli("--verify-command")
    ] = Field(
        default=None, description="Verification command Karo must run (argv list or JSON string)."
    )
    wheel_path: Annotated[str | None, Cli("--wheel-path")] = Field(
        default=None, description="Host path of the karox wheel to install in the container."
    )


class Karo(BaseInstalledAgent):
    """Run KaroX inside a Harbor task container against the race gateway."""

    capabilities = AgentCapabilities()
    MODEL_CONNECTION = ModelConnectionSpec(passthrough=True)

    options_model = KaroOptions

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        wheel = getattr(self.options, "wheel_path", None)
        self._wheel_path = Path(wheel) if wheel else self._find_wheel()

    @staticmethod
    def _find_wheel() -> Path | None:
        """Locate a built karox wheel next to this module's repository."""
        here = Path(__file__).resolve()
        for base in here.parents:
            dist = base / "dist"
            if dist.is_dir():
                wheels = sorted(dist.glob("karox-*.whl"))
                if wheels:
                    return wheels[-1]
        return None

    @staticmethod
    @override
    def name() -> str:
        return "karo"

    @override
    def get_version_command(self) -> str | None:
        # best-effort only; the CLI flag is `--version`
        return None

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("python3",))
        if not self._wheel_path or not self._wheel_path.is_file():
            raise RuntimeError(
                "karox wheel not found; build it with `python -m build --wheel` "
                "in the KaroX repository and pass wheel_path=/abs/path.whl"
            )
        await environment.upload_file(str(self._wheel_path), _WHEEL_TARGET)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=f"chown {shlex.quote(str(environment.default_user))} {_WHEEL_TARGET}",
            )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"python3 -m venv {_VENV} && "
                f"{_PY} -m pip install --quiet --force-reinstall {shlex.quote(_WHEEL_TARGET)} && "
                f"{_PY} -c 'import karox; print(karox.__file__)'"
            ),
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        options: KaroOptions = self.options  # type: ignore[assignment]
        base_url = self._extra_env.get(_ENV_GATEWAY_URL) or ""
        api_key = self._extra_env.get(_ENV_GATEWAY_KEY) or ""
        if not base_url:
            raise ValueError(
                f"gateway base URL missing: set {_ENV_GATEWAY_URL} via --agent-env"
            )
        parts = urlsplit(base_url)
        karox_base_url, relay_active = await self._ensure_loopback_reachable(
            parts, base_url
        )

        flags = ""
        if options.max_steps is not None:
            flags += f" --max-steps {int(options.max_steps)}"
        if options.max_seconds is not None:
            flags += f" --max-seconds {int(options.max_seconds)}"
        if options.effort_level:
            flags += f" --effort-level {shlex.quote(options.effort_level)}"
        if options.semantic_shadow:
            flags += " --semantic-shadow"
        # karox agent run REQUIRES an approved verification command, and the CLI
        # accepts it only as a JSON array of arguments (a bare shell string is
        # rejected). Accept either shape from the job config and normalize.
        for verify in self._verification_commands(options.verify_command):
            flags += f" --verification-command {shlex.quote(json.dumps(verify))}"

        task_dir = shlex.quote(options.task_dir or "/app")
        escaped = shlex.quote(instruction)
        env = {
            _ENV_GATEWAY_URL: base_url,
            "KAROX_PROVIDER_RACEGW_API_KEY": api_key,
            # persist Karo's own state next to the agent logs for post-mortem
            "KAROX_VNEXT_CONFIG_DIR": "/logs/agent/karox-config",
            "KAROX_VNEXT_RUNTIME_DIR": "/logs/agent/karox-runtime",
        }
        # The relay must live inside the SAME exec session as the agent run: a
        # background process from a previous docker exec session does not
        # reliably survive. It dies when the session ends, exactly when the
        # agent no longer needs it.
        relay_up = ""
        if relay_active:  # relay is needed for non-loopback gateways
            relay_up = (
                f"cat > {_RELAY_PATH} <<'KARO_RELAY_EOF'\n{_RELAY_SCRIPT}\nKARO_RELAY_EOF\n"
                f"GW=$({_GW_RESOLVER}) && "
                f"python3 {_RELAY_PATH} {int(_RELAY_PORT)} "
                f'"$GW:{parts.port or (443 if parts.scheme == "https" else 80)}" '
                f">/tmp/karo_relay.log 2>&1 & "
                f"RPID=$! ; "
                f"python3 -c \"import socket, time\n"
                f"for _ in range(50):\n"
                f"    try:\n"
                f"        s = socket.create_connection(('127.0.0.1', {int(_RELAY_PORT)}), 1)\n"
                f"        s.close(); break\n"
                f"    except OSError:\n"
                f"        time.sleep(0.2)\n"
                f"else:\n"
                f"    raise SystemExit(1)\" || "
                f"(echo RELAY_FAILED >&2; cat /tmp/karo_relay.log >&2; exit 1) && "
            )
        await self.exec_as_agent(
            environment,
            command=(
                f"{relay_up}"
                f"cd {task_dir} && "
                f"{_PY} -m karox.cli provider add racegw "
                "--adapter openai_compatible_chat "
                f'--base-url {shlex.quote(karox_base_url + "/v1")} '
                "--credential-ref env:KAROX_PROVIDER_RACEGW_API_KEY "
                "--timeout-seconds 900 --max-transport-retries 2 --json && "
                f"{_PY} -m karox.cli model add racegw step-5-bench-karo "
                "--context-window 256000 --max-output-tokens 32768 "
                '--tools true --streaming true --structured-output unknown '
                '--vision unknown --provenance "harbor race alias" --json && '
                f"{_PY} -m karox.cli agent run "
                f"--repository {task_dir} "
                f"--route racegw/step-5-bench-karo "
                f"{flags} "
                f"--task {escaped} "
                "--stream --json "
                f"2>&1 | tee {_AGENT_LOG} ; "
                + ("RC=$${PIPESTATUS[0]} ; kill $RPID 2>/dev/null ; exit $RC".replace("$$", "$")
                   if relay_active else "")
            ),
            env=env,
        )

    @staticmethod
    def _verification_commands(value) -> list[list[str]]:
        """Normalize the job's verification setting to a list of argv lists.

        Accepted shapes: a shell-style string, a JSON array string, one argv
        list, or a list of argv lists. The first entry is the exact command the
        run must pass; later entries (for example a ``pytest *`` prefix rule)
        widen what Karo may run for focused checks, which the other harnesses
        can do freely through their own shells.
        """
        verify = value or ["python3", "-m", "pytest", "-q"]
        if isinstance(verify, str):
            text = verify.strip()
            verify = json.loads(text) if text.startswith("[") else shlex.split(text)
        if verify and all(isinstance(item, str) for item in verify):
            verify = [verify]
        commands = [list(item) for item in verify]
        if not commands or not all(
            item and all(isinstance(arg, str) and arg for arg in item) for item in commands
        ):
            raise ValueError("verify_command must be argv strings or a list of argv lists")
        return commands

    async def _ensure_loopback_reachable(self, parts, base_url: str) -> tuple[str, bool]:
        """Return (base URL for Karo, relay needed?).

        Loopback/HTTPS gateways pass through unchanged. For other addresses
        Karo's credential guard (HTTPS-or-loopback only) requires an in-container
        loopback relay; return the relay's loopback URL and signal that the
        command must start it in-session.
        """
        host = (parts.hostname or "").lower()
        if parts.scheme == "https" or host == "localhost":
            return base_url, False
        try:
            if ipaddress.ip_address(host).is_loopback:
                return base_url, False
        except ValueError:
            pass
        return f"http://127.0.0.1:{int(_RELAY_PORT)}", True

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        pass
