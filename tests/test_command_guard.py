from __future__ import annotations

import sys

import pytest

from _support import SRC  # noqa: F401

from karox.command_guard import DeveloperCommandBlocked, validate_developer_command_argv


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "push", "origin", "HEAD"],
        ["git", "commit", "-m", "bypass guarded commit"],
        ["cmd", "/c", "echo hidden"],
        ["powershell", "-Command", "Write-Output hidden"],
        ["npm", "publish"],
        ["pnpm", "login"],
        ["yarn", "npm", "publish"],
        ["twine", "upload", "dist/pkg.whl"],
        ["cargo", "publish"],
        ["gh", "auth", "login"],
        ["gh", "release", "create", "v1"],
        ["vercel", "deploy", "--prod"],
        ["netlify", "deploy", "--prod"],
        ["docker", "push", "example/image"],
        ["kubectl", "apply", "-f", "deployment.yaml"],
        ["terraform", "apply", "-auto-approve"],
        [sys.executable, "-c", "import os; os.system('git push origin HEAD')"],
        ["node", "-e", "require('child_process').execSync('npm publish')"],
    ],
)
def test_global_remote_side_effect_actions_are_blocked(argv: list[str]) -> None:
    with pytest.raises(DeveloperCommandBlocked):
        validate_developer_command_argv(argv)


@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-m", "pytest", "-q"],
        [sys.executable, "-c", "print('local')"],
        ["node", "-e", "console.log('local')"],
        ["npm", "install"],
        ["npm", "run", "build"],
        ["pip", "install", "build"],
        ["vercel", "status"],
        ["git", "status", "--short"],
        ["git", "-C", ".", "log", "-1"],
        ["gh", "pr", "view", "123"],
        ["docker", "build", "."],
    ],
)
def test_local_development_and_read_only_status_commands_remain_allowed(argv: list[str]) -> None:
    assert list(validate_developer_command_argv(argv)) == argv
