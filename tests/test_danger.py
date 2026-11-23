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


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        # A quote inside double quotes is literal; the substitution still runs.
        ("echo \"'$(rm -rf ~)'\"", "rm.recursive-force-root-or-home"),
        ('echo "$( (rm -rf /) )"', "rm.recursive-force-root-or-home"),
        ("echo $(echo $(rm -rf /))", "rm.recursive-force-root-or-home"),
        # bash drops unquoted backslashes: r\m is rm.
        (r"r\m -rf /", "rm.recursive-force-root-or-home"),
        (r"\rm -rf /", "rm.recursive-force-root-or-home"),
        (r"sudo r\m -rf /", "rm.recursive-force-root-or-home"),
        (r"g\it push -f", "git.push-force"),
        (r"rm -rf \/", "rm.recursive-force-root-or-home"),
        ("rm -rf //", "rm.recursive-force-root-or-home"),
        ("git push -uf origin main", "git.push-force"),
        ("git push origin main -fu", "git.push-force"),
        # An unset variable expands to nothing.
        ("rm -rf $HOME/$unset", "rm.recursive-force-root-or-home"),
        ("rm -rf $BUILD_DIR/", "rm.recursive-force-root-or-home"),
        ('rm -rf "$STEAMROOT"/*', "rm.recursive-force-root-or-home"),
        ("rm -rf ${PREFIX}/usr", "rm.recursive-force-root-or-home"),
    ],
)
def test_evasions(command: str, rule: str) -> None:
    result = check_command(command, cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None, command
    assert result.rule == rule


@pytest.mark.parametrize(
    "command",
    [
        "echo '$(rm -rf /)'",
        "echo '`rm -rf /`'",
        r'echo "\$(rm -rf /)"',
        "rm -rf ${OUT:?}/",
        "rm -rf ${OUT:-build}/",
        "rm -rf $BUILD/out",
        'rm -rf "$TMP_DIR"',
        "git push -u origin feature",
        "git push --follow-tags",
        "git push -o ci.skip",
        "echo $((1 + 2))",
    ],
)
def test_evasion_lookalikes_are_safe(command: str) -> None:
    assert check_command(command, cwd=CWD, workspace=CWD, home=HOME) is None


def test_powershell_subexpression() -> None:
    result = check_command(
        'Write-Output "$(Remove-Item C:/ -Recurse)"',
        shell="pwsh",
        cwd=CWD,
        workspace=CWD,
        home=HOME,
    )
    assert result is not None
    assert result.rule == "powershell.remove-item-root-or-home"


def test_powershell_safe() -> None:
    assert (
        check_command(
            r"Remove-Item .\build -Recurse -Force", shell="pwsh", cwd=CWD, workspace=CWD, home=HOME
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf tests/__pycache__ 2>/dev/null",
        "rm -rf /tmp/x 2>/dev/null; true",
        "rm -rf build 2>/dev/null",
        "rm -rf build 2> /dev/null",
        "rm -rf build 2>>/dev/null",
        "rm -rf build 2>> /dev/null",
        "rm -rf build >/dev/null",
        "rm -rf build > /dev/null",
        "rm -rf build >>/dev/null",
        "rm -rf build >> /dev/null",
        "rm -rf build </dev/null",
        "rm -rf build < /dev/null",
        "rm -rf build &>/dev/null",
        "rm -rf build &> /dev/null",
        "rm -rf build &>>/dev/null",
        "rm -rf build &>> /dev/null",
        "rm -rf build 2>&1",
        "rm -rf build 2>& 1",
        "rm -rf build >&2",
        "rm -rf build >& 2",
        "rm -rf build 0<&3",
        "rm -rf build 0<& 3",
        "rm -rf build 1>&2 2>/dev/null",
        "rm -rf build >/dev/null 2>&1",
        "rm -rf build 2>&-",
        "rm -rf build >|/dev/null",
        "rm -rf build 3<>/dev/null",
        "rm -rf build <<<word",
        "rm -rf build <<< word",
        "rm -rf build <<</",
        "rm -rf build <<< /",
        "rm -rf build >/etc/out",
        "rm -rf build </etc/passwd",
        "2>/dev/null rm -rf build",
        "sudo rm -rf build 2>/dev/null",
        "find . -name '*.pyc' -delete 2>/dev/null",
        "find . -exec rm -rf build {} \\; 2>/dev/null",
        "fdisk -l 2>/dev/null",
        "parted -l 2> /dev/null",
        "git push origin feature 2>/dev/null",
        "git push origin feature > +log",
        'psql -c "SELECT 1" > "drop database.log"',
        'cmd /c "rd /s build 2>nul"',
        'cmd /c "rd /s build 2> .git"',
        "head -c 512 < /dev/sda",
    ],
)
def test_redirections_are_not_arguments_safe(command: str) -> None:
    assert check_command(command, cwd=CWD, workspace=CWD, home=HOME) is None


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("rm -rf / 2>/dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf / 2> /dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf ~ >/dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf ~ &>/dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf .. 2>&1", "rm.recursive-force-cwd-self"),
        ("rm -rf .. 2>& 1", "rm.recursive-force-cwd-self"),
        ("2>/dev/null rm -rf /", "rm.recursive-force-root-or-home"),
        ("rm 2>/dev/null -rf /", "rm.recursive-force-root-or-home"),
        ("rm -rf *>/dev/null", "rm.recursive-force-cwd-self"),
        ("rm -rf />/dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf ~>/dev/null", "rm.recursive-force-root-or-home"),
        ("rm -rf ..>/dev/null", "rm.recursive-force-cwd-self"),
        ('rm -rf / <<< "safe"', "rm.recursive-force-root-or-home"),
        ('rm -rf build 2>"" /', "rm.recursive-force-root-or-home"),
        ("cd / 2>/dev/null && rm -rf *", "rm.recursive-force-root-or-home"),
        ("2>/dev/null cd / && rm -rf *", "rm.recursive-force-root-or-home"),
        ("find / -delete 2>/dev/null", "find.delete-root-or-home"),
        ("fdisk /dev/sda 2>/dev/null", "mkfs.device"),
        ("git push -f origin main 2>/dev/null", "git.push-force"),
        ("echo x >/dev/sda", "redirect.block-device"),
        ("echo x 2>/dev/sda", "redirect.block-device"),
        ("echo x &>/dev/sda", "redirect.block-device"),
        ("echo x &>>/dev/nvme0n1", "redirect.block-device"),
        (">/dev/sda", "redirect.block-device"),
        ('psql <<<"DROP DATABASE x"', "sql.drop-database"),
        ('sudo psql <<< "DROP DATABASE x"', "sql.drop-database"),
        ('cmd /c "rd /s C:/ 2>nul"', "cmd.recursive-delete-root-or-home"),
    ],
)
def test_redirections_preserve_real_detections(command: str, rule: str) -> None:
    result = check_command(command, cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None, command
    assert result.rule == rule


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item -Recurse x 2>$null",
        "Remove-Item -Recurse x 2> $null",
        "Remove-Item -Recurse x *>$null",
        "Remove-Item -Recurse x *> $null",
        "Remove-Item -Recurse x *>&1",
        "Remove-Item -Recurse x 2>&1",
        "Remove-Item -Recurse x > .git",
        "Remove-Item -Recurse x 2> C:/",
        "Remove-Item -Recurse x 3>>C:/log.txt",
    ],
)
def test_powershell_redirections_safe(command: str) -> None:
    assert check_command(command, shell="pwsh", cwd=CWD, workspace=CWD, home=HOME) is None


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("Remove-Item -Recurse C:/ 2>$null", "powershell.remove-item-root-or-home"),
        ("Remove-Item -Recurse ~ *>$null", "powershell.remove-item-root-or-home"),
        ("Remove-Item -Recurse * 2>$null", "powershell.remove-item-cwd-self"),
        ("Remove-Item -Recurse * > out.txt", "powershell.remove-item-cwd-self"),
        ("Remove-Item -Recurse .git 2>$null", "powershell.remove-item-git-metadata"),
    ],
)
def test_powershell_redirections_dangerous(command: str, rule: str) -> None:
    result = check_command(command, shell="pwsh", cwd=CWD, workspace=CWD, home=HOME)
    assert result is not None, command
    assert result.rule == rule
