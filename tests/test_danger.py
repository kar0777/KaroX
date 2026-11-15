from pathlib import Path

import pytest

from karox6.tools.danger import check_command

CWD = Path("/work/project")
HOME = Path("/home/tester")

DANGEROUS = [
    *[
        (f"rm {flags} {target}", "rm.recursive-force-root-or-home")
        for flags in ("-rf", "-fr", "-r -f", "--recursive --force", "-Rf")
        for target in (
            "/",
            "/*",
            "~",
            "$HOME",
            "${HOME}",
            "/usr",
            "/etc",
            "/bin",
            "/var",
            "/opt",
            "/lib",
            "/boot",
            "/sys",
            "/proc",
            "/dev",
            "/root",
            "/home",
            "/Users",
            "/System",
            "/Library",
            "C:/",
            "/c/",
        )
    ],
    *[
        (f"rm -rf {target}", "rm.recursive-force-cwd-self")
        for target in (".", "./", "*", "./*", "..", "../", "$PWD", "/work/project")
    ],
    ("rm -rf /other/project", "rm.recursive-force-outside-cwd"),
    ("rm -rf ../other", "rm.recursive-force-outside-cwd"),
    ("rm -rf ~/important", "rm.recursive-force-outside-cwd"),
    ('rm -rf "$HOME/notes"', "rm.recursive-force-outside-cwd"),
    ("rm -rf ${HOME}/src", "rm.recursive-force-outside-cwd"),
    ("rm -f .git", "rm.git-metadata"),
    ("rm -r /work/project/.git", "rm.git-metadata"),
    ("find .git -delete", "find.delete-git-metadata"),
    ("find /work/project/.git -exec rm {} ;", "find.delete-git-metadata"),
    ("find / -delete", "find.delete-root-or-home"),
    ("find ~ -exec rm {} ;", "find.delete-root-or-home"),
    *[
        (f"dd if=x of=/dev/{device}", "dd.device-write")
        for device in ("sda", "nvme0n1", "hda", "vda", "xvda", "mmcblk0", "disk1")
    ],
    *[
        (f"{tool} /dev/sda", "mkfs.device")
        for tool in (
            "mkfs",
            "mkfs.ext4",
            "mke2fs",
            "mkswap",
            "wipefs",
            "fdisk",
            "sfdisk",
            "parted",
            "sgdisk",
        )
    ],
    *[
        (f"diskutil {op} disk1", "disk.erase")
        for op in ("eraseDisk", "eraseVolume", "zeroDisk", "randomDisk", "secureErase")
    ],
    *[
        (cmd, "disk.erase")
        for cmd in ("format C:", "diskpart", "Format-Volume", "Clear-Disk", "Initialize-Disk")
    ],
    ("echo x > /dev/sda", "redirect.block-device"),
    ("echo x >> /dev/nvme0n1", "redirect.block-device"),
    ("echo x | tee /dev/vda", "redirect.block-device"),
    *[
        (f"git push {flag}", "git.push-force")
        for flag in (
            "--force",
            "-f",
            "--force-with-lease",
            "--force-if-includes",
            "--mirror",
            "origin +main",
        )
    ],
    *[
        (f'{client} -c "DROP DATABASE test"', "sql.drop-database")
        for client in (
            "psql",
            "mysql",
            "mariadb",
            "sqlcmd",
            "clickhouse-client",
            "mongosh",
            "mongo",
        )
    ],
    ('mysql -e "drop schema foo"', "sql.drop-database"),
    ('mongo --eval "db.dropDatabase()"', "sql.drop-database"),
    ('echo "DROP DATABASE x" | psql', "sql.drop-database"),
    ('psql <<< "DROP DATABASE x"', "sql.drop-database"),
    *[
        (f"{wrapper} rm -rf /", "rm.recursive-force-root-or-home")
        for wrapper in (
            "sudo",
            "sudo -u root",
            "doas",
            "env -i X=1",
            "command",
            "builtin",
            "exec",
            "nice -n 3",
            "nohup",
            "time",
            "timeout 5",
            "X=1",
        )
    ],
    ("echo / | xargs rm -rf", "rm.recursive-force-root-or-home"),
    ('bash -c "rm -rf /"', "rm.recursive-force-root-or-home"),
    ('sh -lc "rm -rf /"', "rm.recursive-force-root-or-home"),
    ('eval "rm -rf /"', "rm.recursive-force-root-or-home"),
    ("echo $(rm -rf /)", "rm.recursive-force-root-or-home"),
    ("echo `rm -rf /`", "rm.recursive-force-root-or-home"),
    ("cd / && rm -rf *", "rm.recursive-force-root-or-home"),
    ('pwsh -NoProfile -Command "Remove-Item C:/ -Recurse"', "powershell.remove-item-root-or-home"),
    ('cmd /c "rd /s C:/"', "cmd.recursive-delete-root-or-home"),
    ('cmd /c "rmdir /s .git"', "cmd.recursive-delete-git-metadata"),
]

SAFE = [
    "rm -rf build",
    "rm -rf /tmp/x",
    "git reset --hard",
    "git clean -fd",
    "git checkout -- file",
    "pip install x",
    "npm publish",
    'python -c "print(1)"',
    'echo "rm -rf /"',
    'grep -r "rm -rf /" .',
    'git commit -m "drop database"',
    "printf 'DROP DATABASE x'",
    "python -c \"print('rm -rf /')\"",
    "rm -rf build dist",
    "rm -rf ./node_modules",
    'rm -rf "$WORKDIR/out"',
    "rm -f file.txt",
    "rm -r src/old",
    "git push",
    "git push origin feature",
    "git push --tags",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "find . -name '*.pyc' -delete",
    "dd if=/dev/zero of=./disk.img bs=1M count=1",
    "docker rm -f x",
    "kubectl delete pod x",
    'psql -c "SELECT 1"',
    'psql -c "DROP TABLE x"',
    "echo /tmp/x | xargs rm -rf",
    "dd if=x of=/dev/null",
    "rm -rf /var/tmp/x",
]


@pytest.mark.parametrize(("command", "rule"), DANGEROUS)
def test_dangerous(command: str, rule: str) -> None:
    result = check_command(command, cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None
    assert result.rule == rule
    assert result.label
    assert result.reason


@pytest.mark.parametrize("command", SAFE)
def test_safe(command: str) -> None:
    assert check_command(command, cwd=CWD, workspace=CWD, home=HOME) is None


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        *[
            (f"{cmd} {target} -Recurse", "powershell.remove-item-root-or-home")
            for cmd in ("Remove-Item", "rm", "del", "rd", "rmdir", "ri")
            for target in ("C:/", "$HOME", "~", "$env:USERPROFILE", "C:/Windows", "C:/Users")
        ],
        ("Remove-Item .git", "powershell.remove-item-git-metadata"),
        ("Remove-Item ./x/.git -Force", "powershell.remove-item-git-metadata"),
        *[
            (f"Remove-Item {target} -Recurse", "powershell.remove-item-cwd-self")
            for target in (".", "*", ".\\*", "/work/project")
        ],
    ],
)
def test_powershell(command: str, rule: str) -> None:
    result = check_command(command, shell="pwsh", cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None
    assert result.rule == rule


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("echo ok\nrm -rf /", "rm.recursive-force-root-or-home"),
        ("echo ok;\nrm -rf /", "rm.recursive-force-root-or-home"),
        ("C:\\bin\\rm.exe -rf C:\\", "rm.recursive-force-root-or-home"),
        ("cmd /c 'del /s %USERPROFILE%'", "cmd.recursive-delete-root-or-home"),
        ("find /usr -delete", "find.delete-root-or-home"),
        ("find . -exec rm -rf / \\;", "rm.recursive-force-root-or-home"),
    ],
)
def test_additional_commands(command: str, rule: str) -> None:
    result = check_command(command, cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None
    assert result.rule == rule


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf $HOMELESS",
        "rm -rf $WORKDIR/.git",
        "echo /other | xargs rm -rf",
        "find . -exec rm -rf {} \\;",
    ],
)
def test_dynamic_targets(command: str) -> None:
    assert check_command(command, cwd=CWD, workspace=CWD, home=HOME) is None


def test_powershell_safe() -> None:
    assert (
        check_command(
            r"Remove-Item .\build -Recurse -Force", shell="pwsh", cwd=CWD, workspace=CWD, home=HOME
        )
        is None
    )
