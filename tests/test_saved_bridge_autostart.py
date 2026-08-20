"""The zero-install Windows revival layer for saved bridges.

A saved bridge is kept alive by its owner and a sibling supervisor. When both die
in the same event -- the launching terminal is closed as a job, the user logs off,
the machine reboots -- ``desired_running`` stays true and nothing restarts the
bridge. These tests cover the layer that closes that gap using only what ships
with every Windows: a per-user five-minute watch task registered through
``%SystemRoot%\\System32\\schtasks.exe`` from an XML definition, plus a
Startup-folder script. No installer, no admin rights, and no manual user setup.

The definition is what makes the task trustworthy rather than merely present --
``TaskDefinitionTests`` covers the settings a ``schtasks`` command line cannot
express, and the command line remains as the fallback for a Windows that refuses
XML registration.

The tests never touch the real Task Scheduler: the process runner is injected.
``test_a_test_process_never_touches_the_machine`` asserts that property directly.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from _support import SRC  # noqa: F401

from karox import saved_bridge_autostart as autostart

PROFILE = "hyperagent-auto-88868ab08b-workspace_write"
FAKE_PYTHON = r"C:\Users\tester\venv\Scripts\pythonw.exe"
FAKE_SCHTASKS = r"C:\Windows\System32\schtasks.exe"


class _Runner:
    """Records ``schtasks`` invocations and answers ``/Query`` from a fixture.

    A registered task is remembered twice, because the module reads it back two
    ways: ``/Query /FO LIST /V`` to recognise its own command, and ``/Query /XML``
    to see whether the settings a command line cannot express are already there.
    """

    def __init__(self, known: Optional[dict[str, str]] = None) -> None:
        self.calls: list[list[str]] = []
        self.known = dict(known or {})
        self.known_xml: dict[str, str] = {}
        self.definitions: list[str] = []
        self.refuse_xml = False

    def remember(self, task: str, command: str, *, settings: bool = True) -> None:
        """Pretend ``task`` exists, optionally with the settings this module needs."""
        self.known[task] = f"Task To Run: {command}"
        # Each marker already contains its value and the start of the closing tag.
        body = "".join(f"  {marker}/x>\n" for marker in autostart._REQUIRED_TASK_SETTINGS)
        self.known_xml[task] = f"<Task>\n{body if settings else ''}</Task>"

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if args and args[0] == "/Query":
            name = args[args.index("/TN") + 1]
            source = self.known_xml if "/XML" in args else self.known
            if name in source:
                return 0, source[name]
            return 1, "ERROR: The system cannot find the file specified."
        if args and args[0] == "/Create" and "/XML" in args:
            # The definition is deleted as soon as schtasks has read it, so this is
            # the only moment a test can see what was actually registered.
            path = Path(args[args.index("/XML") + 1])
            self.definitions.append(path.read_bytes().decode("utf-16"))
            if self.refuse_xml:
                return 1, "ERROR: Access is denied."
        return 0, "SUCCESS"

    def created(self) -> list[list[str]]:
        return [call for call in self.calls if call and call[0] == "/Create"]

    def command_line_creates(self) -> list[list[str]]:
        return [call for call in self.created() if "/TR" in call]

    def deleted(self) -> list[list[str]]:
        return [call for call in self.calls if call and call[0] == "/Delete"]


def _value(args: list[str], flag: str) -> Optional[str]:
    if flag not in args:
        return None
    index = args.index(flag)
    return args[index + 1] if index + 1 < len(args) else None


class _AutostartTestCase(unittest.TestCase):
    """Every test runs against an injected runner, interpreter and Startup folder.

    The Startup folder, the runtime directory and the config directory are all
    redirected for *every* test: the real ones belong to the developer running the
    suite and already hold this machine's own bridges.
    """

    def setUp(self) -> None:
        self.runner = _Runner()
        self.startup = tempfile.TemporaryDirectory()
        self.addCleanup(self.startup.cleanup)
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)
        self.config = tempfile.TemporaryDirectory()
        self.addCleanup(self.config.cleanup)
        patches = (
            patch.object(autostart, "_run_schtasks", side_effect=self._run),
            patch.object(autostart, "schtasks_path", return_value=FAKE_SCHTASKS),
            patch.object(autostart, "windowless_python", return_value=FAKE_PYTHON),
            patch.object(autostart, "startup_dir", return_value=Path(self.startup.name)),
            patch.object(autostart, "runtime_dir", return_value=Path(self.runtime.name)),
            patch.object(autostart, "config_dir", return_value=Path(self.config.name)),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _run(self, args: list[str]) -> tuple[int, str]:
        return self.runner(args)


class TaskRegistrationTests(_AutostartTestCase):
    def test_registration_creates_a_five_minute_watch_task_and_a_logon_script(self) -> None:
        report = autostart.register_autostart(PROFILE)

        self.assertTrue(report["registered"])
        self.assertEqual(report["mechanism"], "schtasks+startup_folder")
        created = self.runner.created()
        self.assertEqual(len(created), 1)
        (definition,) = self.runner.definitions
        self.assertIn(f"<Interval>PT{autostart.WATCH_INTERVAL_MINUTES}M</Interval>", definition)
        self.assertEqual(autostart.WATCH_INTERVAL_MINUTES, 5)
        self.assertTrue(autostart.startup_script_path(PROFILE).exists())

    def test_the_logon_script_is_what_covers_reboot(self) -> None:
        # A logon trigger is available from XML but not from ``/SC ONLOGON``, so the
        # Startup folder is what still covers logon when only the command line took.
        autostart.register_autostart(PROFILE)

        script = autostart.startup_script_path(PROFILE)
        raw = script.read_bytes()
        body = raw.decode("utf-8")
        self.assertIn(FAKE_PYTHON, body)
        self.assertIn(f'--saved "{PROFILE}"', body)
        self.assertNotIn("ONLOGON", str(self.runner.calls))
        # ``cmd.exe`` reads this file: every line ends CRLF, with no stray CR.
        self.assertNotIn(b"\r\r", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_tasks_are_per_user_and_never_ask_for_elevation(self) -> None:
        autostart.register_autostart(PROFILE)

        for call in self.runner.created():
            self.assertIn("/F", call)
            # A stored password or SYSTEM account would need admin rights and
            # would make the bridge run outside the user's session.
            self.assertNotIn("/RU", call)
            self.assertNotIn("/RP", call)
        for definition in self.runner.definitions:
            self.assertNotIn("HighestAvailable", definition)
            self.assertNotIn("<UserId>S-1-5-18", definition)

    def test_the_command_line_fallback_asks_for_no_privilege_either(self) -> None:
        self.runner.refuse_xml = True

        autostart.register_autostart(PROFILE)

        (call,) = self.runner.command_line_creates()
        self.assertEqual(_value(call, "/RL"), "LIMITED")
        self.assertEqual(_value(call, "/SC"), "MINUTE")
        self.assertEqual(_value(call, "/MO"), str(autostart.WATCH_INTERVAL_MINUTES))
        self.assertNotIn("/RU", call)
        self.assertNotIn("/RP", call)

    def test_the_task_name_is_flat_and_namespaced_by_the_profile_digest(self) -> None:
        task = autostart.autostart_task_name(PROFILE)
        other = autostart.autostart_task_name("chatgpt-dev")

        self.assertTrue(task.startswith(autostart.TASK_PREFIX))
        self.assertNotEqual(task, other)
        # A Task Scheduler *folder* cannot be created without extra rights.
        self.assertNotIn("\\", task)
        # The digest, not the raw name: a profile name may be long or awkward.
        self.assertNotIn(PROFILE, task)

        autostart.register_autostart(PROFILE)
        self.assertEqual({_value(call, "/TN") for call in self.runner.created()}, {task})

    def test_the_task_command_runs_the_entry_point_and_carries_no_secret(self) -> None:
        self.runner.refuse_xml = True

        autostart.register_autostart(PROFILE)

        command = _value(self.runner.command_line_creates()[0], "/TR") or ""
        script = autostart.startup_script_path(PROFILE).read_text(encoding="utf-8")
        for text in (command, script, self.runner.definitions[0]):
            self.assertIn(FAKE_PYTHON, text)
            self.assertIn("-m karox.saved_bridge_autostart", text)
            self.assertIn(f'--saved "{PROFILE}"', text)
            # A scheduled task's command line and a Startup script are readable
            # by this user; only the profile name may appear in them.
            for forbidden in ("Bearer", "--token", "password", "os-keyring"):
                self.assertNotIn(forbidden, text)

    def test_registration_is_idempotent_when_the_task_already_matches(self) -> None:
        command = autostart.autostart_command(PROFILE)
        self.runner.remember(autostart.autostart_task_name(PROFILE), command)

        report = autostart.register_autostart(PROFILE)

        self.assertTrue(report["registered"])
        self.assertEqual(self.runner.created(), [])
        self.assertIn("already_registered", report["reason"])

    def test_a_second_ensure_changes_nothing_at_all(self) -> None:
        # The watchdog calls this every five minutes; a repeat that reported work
        # it did not do would make the layer impossible to audit from its report.
        first = autostart.register_autostart(PROFILE)
        task = autostart.autostart_task_name(PROFILE)
        self.runner.remember(task, autostart.autostart_command(PROFILE))
        script = autostart.startup_script_path(PROFILE)
        stamp = script.stat().st_mtime_ns

        second = autostart.register_autostart(PROFILE)

        self.assertNotEqual(first["created"], [])
        self.assertEqual(second["created"], [])
        self.assertEqual(second["reason"], "already_registered")
        self.assertEqual(second["mechanism"], "schtasks+startup_folder")
        self.assertEqual(script.stat().st_mtime_ns, stamp)

    def test_a_task_pointing_at_a_moved_interpreter_is_repaired(self) -> None:
        task = autostart.autostart_task_name(PROFILE)
        stale = f'"C:\\old\\pythonw.exe" -m karox.saved_bridge_autostart --saved "{PROFILE}"'
        self.runner.remember(task, stale)

        report = autostart.register_autostart(PROFILE)

        self.assertEqual(len(self.runner.created()), 1)
        self.assertIn(task, report["created"])

    def test_a_task_that_would_not_run_on_battery_is_upgraded_in_place(self) -> None:
        # Exactly what the command-line path leaves behind, and what an installed
        # older KaroX registered: the right command, crippled settings.
        task = autostart.autostart_task_name(PROFILE)
        self.runner.remember(task, autostart.autostart_command(PROFILE), settings=False)

        report = autostart.register_autostart(PROFILE)

        self.assertIn(task, report["created"])
        self.assertEqual(len(self.runner.definitions), 1)

    def test_a_windows_that_will_not_show_its_xml_is_left_alone(self) -> None:
        # ``/Query /XML`` failing is not evidence of a bad task, and rewriting a
        # working task every five minutes would be worse than one stale setting.
        task = autostart.autostart_task_name(PROFILE)
        self.runner.known[task] = f"Task To Run: {autostart.autostart_command(PROFILE)}"

        report = autostart.register_autostart(PROFILE)

        self.assertEqual(self.runner.created(), [])
        self.assertIn("already_registered", report["reason"])

    def test_a_command_too_long_for_the_command_line_still_registers_from_xml(self) -> None:
        long_python = "C:\\" + ("d" * autostart.MAX_TASK_COMMAND_CHARS) + "\\pythonw.exe"
        with patch.object(autostart, "windowless_python", return_value=long_python):
            report = autostart.register_autostart(PROFILE)

        # ``/TR`` truncates past 261 characters; an XML definition has no such limit.
        self.assertEqual(report["mechanism"], "schtasks+startup_folder")
        self.assertEqual(self.runner.command_line_creates(), [])
        self.assertIn(long_python, self.runner.definitions[0])

    def test_a_command_that_would_not_fit_registers_the_script_only(self) -> None:
        self.runner.refuse_xml = True
        long_python = "C:\\" + ("d" * autostart.MAX_TASK_COMMAND_CHARS) + "\\pythonw.exe"
        with patch.object(autostart, "windowless_python", return_value=long_python):
            report = autostart.register_autostart(PROFILE)

        self.assertEqual(report["mechanism"], "startup_folder")
        self.assertEqual(self.runner.command_line_creates(), [])
        self.assertTrue(report["registered"])
        self.assertIn("command_too_long", report["reason"])

    def test_a_refused_definition_falls_back_to_the_command_line(self) -> None:
        self.runner.refuse_xml = True

        report = autostart.register_autostart(PROFILE)

        self.assertEqual(report["mechanism"], "schtasks+startup_folder")
        self.assertEqual(len(self.runner.command_line_creates()), 1)
        self.assertIn(autostart.autostart_task_name(PROFILE), report["created"])

    def test_a_denied_schtasks_still_leaves_the_bridge_covered_at_logon(self) -> None:
        with patch.object(autostart, "_run_schtasks", return_value=(1, "ERROR: Access is denied.")):
            report = autostart.register_autostart(PROFILE)

        self.assertEqual(report["mechanism"], "startup_folder")
        self.assertTrue(report["registered"])
        self.assertIn("schtasks_failed", report["reason"])
        self.assertTrue(autostart.startup_script_path(PROFILE).exists())


class TaskDefinitionTests(_AutostartTestCase):
    """The XML exists for the settings a ``schtasks`` command line cannot express.

    Measured on a task this module used to create from the command line: "do not
    start on batteries" and no execution time limit at all, which the XML schema
    then defaults to 72 hours. A watchdog that skips every unplugged laptop, and
    that Windows may hard-terminate together with the bridge tree it revived, does
    not meet the promise this layer exists for.
    """

    def _definition(self) -> str:
        autostart.register_autostart(PROFILE)
        return self.runner.definitions[0]

    def test_the_watchdog_runs_on_battery_and_is_never_stopped_for_running_long(self) -> None:
        definition = self._definition()

        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", definition)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", definition)
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", definition)
        self.assertIn("<AllowHardTerminate>false</AllowHardTerminate>", definition)

    def test_a_tick_missed_while_the_machine_slept_is_caught_up(self) -> None:
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", self._definition())

    def test_a_slow_tick_never_stacks_on_the_next_one(self) -> None:
        definition = self._definition()

        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", definition)

    def test_the_definition_also_carries_the_logon_trigger_schtasks_denies(self) -> None:
        with patch.dict(os.environ, {"USERDOMAIN": "MONSTERPC", "USERNAME": "ekono"}):
            definition = self._definition()

        self.assertIn("<LogonTrigger>", definition)
        self.assertIn("<UserId>MONSTERPC\\ekono</UserId>", definition)

    def test_an_unknown_account_still_gets_the_five_minute_trigger(self) -> None:
        with patch.dict(os.environ, {"USERNAME": ""}):
            definition = self._definition()

        # A logon trigger without a user means "any user", which needs admin
        # rights; the Startup script covers logon instead.
        self.assertNotIn("<LogonTrigger>", definition)
        self.assertIn("<Interval>PT5M</Interval>", definition)

    def test_the_definition_is_valid_xml_that_escapes_the_paths_it_carries(self) -> None:
        odd = Path(self.runtime.name) / "R&D"
        with patch.object(autostart, "runtime_dir", return_value=odd):
            definition = autostart.task_definition_xml(PROFILE)

        root = ElementTree.fromstring(definition)
        arguments = root.find(".//{*}Arguments")
        self.assertIsNotNone(arguments)
        self.assertIn("R&D", str(arguments.text))
        self.assertIn("&amp;", definition)

    def test_schtasks_is_handed_utf16_because_that_is_what_it_declares(self) -> None:
        seen: list[bytes] = []

        def capture(args: list[str]) -> tuple[int, str]:
            if args[0] == "/Create" and "/XML" in args:
                seen.append(Path(args[args.index("/XML") + 1]).read_bytes())
            return 0, "SUCCESS"

        with patch.object(autostart, "_run_schtasks", side_effect=capture):
            autostart.register_autostart(PROFILE)

        (raw,) = seen
        self.assertEqual(raw[:2], b"\xff\xfe")
        self.assertIn('encoding="UTF-16"', raw.decode("utf-16"))

    def test_no_definition_file_is_left_behind_afterwards(self) -> None:
        self.runner.refuse_xml = True

        autostart.register_autostart(PROFILE)

        # The registered task is the durable artefact; a leftover file on disk
        # would only invite editing the copy that no longer matters.
        self.assertFalse(autostart.task_definition_path(PROFILE).exists())

    def test_removal_takes_an_interrupted_definition_with_it(self) -> None:
        definition = autostart.task_definition_path(PROFILE)
        definition.parent.mkdir(parents=True, exist_ok=True)
        definition.write_bytes(b"\xff\xfe")

        report = autostart.remove_autostart(PROFILE)

        self.assertFalse(definition.exists())
        self.assertIn(str(definition), report["deleted"])


class UnsafeProfileNameTests(_AutostartTestCase):
    def test_a_name_that_could_break_out_of_the_command_is_refused(self) -> None:
        for name in ('evil" & calc.exe & "', "with space", "semi;colon", "", "a" * 200):
            with self.subTest(name=name):
                report = autostart.register_autostart(name)
                self.assertFalse(report["registered"])
                self.assertEqual(report["reason"], "unsupported_profile_name")
        self.assertEqual(self.runner.calls, [])

    def test_the_real_profile_names_are_accepted(self) -> None:
        for name in (PROFILE, "chatgpt-dev", "notion-auto-8d8f45c236-workspace_write"):
            with self.subTest(name=name):
                self.assertTrue(autostart._profile_name_is_safe(name))


class RemovalTests(_AutostartTestCase):
    def test_removal_deletes_the_watch_task(self) -> None:
        task = autostart.autostart_task_name(PROFILE)

        report = autostart.remove_autostart(PROFILE)

        deleted = self.runner.deleted()
        self.assertEqual({_value(call, "/TN") for call in deleted}, {task})
        for call in deleted:
            self.assertIn("/F", call)
        self.assertTrue(report["removed"])

    def test_removal_also_clears_the_startup_folder_script(self) -> None:
        script = autostart.startup_script_path(PROFILE)
        script.write_text("@echo off\n", encoding="utf-8")

        report = autostart.remove_autostart(PROFILE)

        self.assertFalse(script.exists())
        self.assertIn(str(script), report["deleted"])

    def test_removing_what_was_never_registered_is_not_an_error(self) -> None:
        report = autostart.remove_autostart(PROFILE)

        self.assertTrue(report["removed"])
        self.assertFalse(autostart.startup_script_path(PROFILE).exists())


class StatusTests(_AutostartTestCase):
    def test_status_reports_both_mechanisms_when_registered(self) -> None:
        command = autostart.autostart_command(PROFILE)
        task = autostart.autostart_task_name(PROFILE)
        self.runner.known = {task: f"Task To Run: {command}"}
        autostart.startup_script_path(PROFILE).write_text("@echo off\n", encoding="utf-8")

        status = autostart.autostart_status(PROFILE)

        self.assertTrue(status["registered"])
        self.assertEqual(status["mechanism"], "schtasks+startup_folder")
        self.assertEqual(status["tasks"], [task])
        self.assertEqual(status["watch_interval_minutes"], autostart.WATCH_INTERVAL_MINUTES)

    def test_status_names_the_startup_script_when_that_is_all_there_is(self) -> None:
        # The degraded shape a locked-down account ends up with: no task, but the
        # logon script still covers every reboot.
        script = autostart.startup_script_path(PROFILE)
        script.write_text("@echo off\n", encoding="utf-8")

        status = autostart.autostart_status(PROFILE)

        self.assertTrue(status["registered"])
        self.assertEqual(status["mechanism"], "startup_folder")
        self.assertEqual(status["tasks"], [])
        self.assertEqual(status["startup_script"], str(script))

    def test_status_is_honest_when_nothing_is_registered(self) -> None:
        status = autostart.autostart_status(PROFILE)

        self.assertFalse(status["registered"])
        self.assertEqual(status["mechanism"], "none")
        self.assertEqual(status["tasks"], [])
        self.assertIsNone(status["startup_script"])

    def test_status_never_changes_anything(self) -> None:
        autostart.autostart_status(PROFILE)

        self.assertEqual(self.runner.created(), [])
        self.assertEqual(self.runner.deleted(), [])


class RuntimeDirPinTests(_AutostartTestCase):
    """An OS-launched process inherits none of the session's environment.

    So it must be *told* where this user's state lives rather than re-deriving it:
    a watchdog that derives a different directory reads ``desired_running`` from an
    empty store and exits 0 having revived nothing, which looks like success.
    """

    def test_both_mechanisms_hand_the_state_directory_forward(self) -> None:
        autostart.register_autostart(PROFILE)

        pinned = f'--runtime-dir "{self.runtime.name}"'
        definition = self.runner.definitions[0]
        script = autostart.startup_script_path(PROFILE).read_text(encoding="utf-8")
        for text in (definition, script):
            self.assertIn(pinned, text)

    def test_the_command_line_fallback_hands_it_forward_too(self) -> None:
        self.runner.refuse_xml = True

        autostart.register_autostart(PROFILE)

        command = _value(self.runner.command_line_creates()[0], "/TR") or ""
        self.assertIn(f'--runtime-dir "{self.runtime.name}"', command)

    def test_the_pin_file_records_every_path_the_bridge_needs(self) -> None:
        report = autostart.register_autostart(PROFILE)

        pin = autostart.pin_path(PROFILE)
        self.assertTrue(pin.exists())
        self.assertIn(str(pin), report["created"])
        payload = json.loads(pin.read_text(encoding="utf-8"))
        self.assertEqual(payload["saved_profile"], PROFILE)
        self.assertEqual(payload["runtime_dir"], self.runtime.name)
        self.assertEqual(payload["config_dir"], self.config.name)
        # The config directory cannot fit in a 261-character task command, so it
        # travels in this file instead -- inside the directory the task is told.
        self.assertTrue(pin.is_relative_to(Path(self.runtime.name)))

    def test_removal_takes_the_pin_with_it(self) -> None:
        autostart.register_autostart(PROFILE)

        report = autostart.remove_autostart(PROFILE)

        self.assertFalse(autostart.pin_path(PROFILE).exists())
        self.assertIn(str(autostart.pin_path(PROFILE)), report["deleted"])

    def test_the_task_command_still_fits(self) -> None:
        # The pin made the command longer; a real profile must still take the
        # task route rather than silently degrading to the Startup folder.
        for name in (PROFILE, "chatgpt-dev"):
            with self.subTest(name=name):
                self.assertLessEqual(
                    len(autostart.autostart_command(name)), autostart.MAX_TASK_COMMAND_CHARS
                )


class PinAdoptionTests(_AutostartTestCase):
    """What ``--runtime-dir`` does inside the process the OS launches."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(setattr, autostart, "_pinned_runtime_dir", None)
        cleaned = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "KAROX_RUNTIME_DIR",
                "KAROX_VNEXT_RUNTIME_DIR",
                "KAROX_CONFIG_DIR",
                "KAROX_VNEXT_CONFIG_DIR",
            }
        }
        item = patch.dict("os.environ", cleaned, clear=True)
        item.start()
        self.addCleanup(item.stop)

    def test_the_recorded_directories_are_adopted_before_state_is_read(self) -> None:
        autostart.register_autostart(PROFILE)

        adopted = autostart._apply_pin(self.runtime.name, PROFILE)

        self.assertEqual(os.environ["KAROX_RUNTIME_DIR"], self.runtime.name)
        self.assertEqual(os.environ["KAROX_CONFIG_DIR"], self.config.name)
        self.assertEqual(adopted["runtime_dir"], self.runtime.name)
        self.assertEqual(adopted["config_dir"], self.config.name)

    def test_a_pin_from_another_profile_is_ignored(self) -> None:
        autostart.register_autostart(PROFILE)
        pin = autostart.pin_path(PROFILE)
        payload = json.loads(pin.read_text(encoding="utf-8"))
        payload["saved_profile"] = "someone-else"
        pin.write_text(json.dumps(payload), encoding="utf-8")

        adopted = autostart._apply_pin(self.runtime.name, PROFILE)

        self.assertIsNone(adopted["config_dir"])
        self.assertNotIn("KAROX_CONFIG_DIR", os.environ)

    def test_an_adopted_pin_is_not_mistaken_for_a_sandbox(self) -> None:
        # The guard exists so a redirected runtime dir cannot register machine
        # entries. A value this module pinned itself is the opposite case: it is
        # the directory the registering bridge asked the task to use.
        with patch.object(autostart, "_under_test", return_value=False), patch.object(
            autostart, "_is_windows", return_value=True
        ):
            autostart._apply_pin(self.runtime.name, PROFILE)
            supported, reason = autostart.os_autostart_supported()

            self.assertTrue(supported)
            self.assertEqual(reason, "supported")

            os.environ["KAROX_RUNTIME_DIR"] = r"C:\somewhere\else"
            blocked, blocked_reason = autostart.os_autostart_supported()

        self.assertFalse(blocked)
        self.assertEqual(blocked_reason, "runtime_dir_override")

    def test_no_pin_argument_changes_nothing(self) -> None:
        adopted = autostart._apply_pin("", PROFILE)

        self.assertEqual(adopted, {"runtime_dir": None, "config_dir": None})
        self.assertNotIn("KAROX_RUNTIME_DIR", os.environ)


class MachineSafetyTests(unittest.TestCase):
    """The guarded entry points must never mutate the machine from a test run."""

    def setUp(self) -> None:
        # Another test's leaked path override must not decide this verdict.
        cleaned = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "KAROX_VNEXT_RUNTIME_DIR",
                "KAROX_RUNTIME_DIR",
                "KAROX_OS_AUTOSTART",
            }
        }
        item = patch.dict("os.environ", cleaned, clear=True)
        item.start()
        self.addCleanup(item.stop)

    def test_a_test_process_never_touches_the_machine(self) -> None:
        # This assertion is meaningful because it runs inside the very process
        # that would otherwise create real Task Scheduler entries.
        supported, reason = autostart.os_autostart_supported()
        self.assertFalse(supported)
        self.assertEqual(reason, "test_runner" if os.name == "nt" else "not_windows")

    def test_the_guarded_entries_report_the_reason_and_do_nothing(self) -> None:
        with patch.object(autostart, "register_autostart") as register, patch.object(
            autostart, "remove_autostart"
        ) as remove:
            ensured = autostart.ensure_saved_bridge_autostart(PROFILE)
            removed = autostart.remove_saved_bridge_autostart(PROFILE)

        register.assert_not_called()
        remove.assert_not_called()
        for report in (ensured, removed):
            self.assertFalse(report["supported"])
            self.assertIn(report["reason"], {"test_runner", "not_windows"})

    def test_a_redirected_runtime_dir_is_treated_as_a_sandbox(self) -> None:
        with patch.dict(
            "os.environ", {"KAROX_VNEXT_RUNTIME_DIR": r"C:\temp\sandbox"}, clear=False
        ), patch.object(autostart, "_under_test", return_value=False), patch.object(
            autostart, "_is_windows", return_value=True
        ):
            supported, reason = autostart.os_autostart_supported()

        self.assertFalse(supported)
        self.assertEqual(reason, "runtime_dir_override")

    def test_autostart_can_be_switched_off_by_the_user(self) -> None:
        with patch.dict("os.environ", {"KAROX_OS_AUTOSTART": "off"}, clear=False), patch.object(
            autostart, "_under_test", return_value=False
        ), patch.object(autostart, "_is_windows", return_value=True):
            supported, reason = autostart.os_autostart_supported()

        self.assertFalse(supported)
        self.assertEqual(reason, "disabled_by_env")

    def test_disabling_future_autostart_does_not_block_legacy_cleanup(self) -> None:
        cleanup = {"removed": True, "deleted": ["legacy-task"]}
        with patch.dict(
            "os.environ", {"KAROX_OS_AUTOSTART": "off"}, clear=False
        ), patch.object(
            autostart, "_under_test", return_value=False
        ), patch.object(
            autostart, "_is_windows", return_value=True
        ), patch.object(
            autostart, "_runtime_dir_overridden", return_value=False
        ), patch.object(
            autostart, "remove_autostart", return_value=cleanup
        ) as remove:
            report = autostart.remove_saved_bridge_autostart(PROFILE)

        remove.assert_called_once_with(PROFILE)
        self.assertTrue(report["supported"])
        self.assertEqual(report["deleted"], ["legacy-task"])


class LegacyAutostartRetirementTests(_AutostartTestCase):
    """Old logon/watchdog entries retire themselves instead of starting KaroX."""

    def _lines(self) -> list[dict[str, object]]:
        raw = autostart.log_path(PROFILE).read_text(encoding="utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_default_legacy_tick_removes_itself_and_never_starts_supervisor(self) -> None:
        cleanup = {"removed": True, "deleted": ["legacy-task", "legacy-startup"]}
        with patch.object(
            autostart, "remove_autostart", return_value=cleanup
        ) as remove, patch(
            "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor"
        ) as ensure:
            code = autostart.main(["--saved", PROFILE])

        self.assertEqual(code, 0)
        remove.assert_called_once_with(PROFILE)
        ensure.assert_not_called()
        (entry,) = self._lines()
        self.assertEqual(entry["outcome"], "legacy_autostart_retired")
        self.assertEqual(entry["deleted"], 2)

    def test_cleanup_failure_is_logged_and_never_starts_supervisor(self) -> None:
        with patch.object(
            autostart,
            "remove_autostart",
            side_effect=RuntimeError("scheduler unavailable"),
        ), patch(
            "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor"
        ) as ensure:
            code = autostart.main(["--saved", PROFILE])

        self.assertEqual(code, 1)
        ensure.assert_not_called()
        (entry,) = self._lines()
        self.assertEqual(entry["outcome"], "legacy_autostart_cleanup_failed")
        self.assertIn("RuntimeError: scheduler unavailable", str(entry["error"]))

    def test_an_unwritable_log_never_blocks_cleanup(self) -> None:
        with patch.object(
            autostart, "remove_autostart", return_value={"deleted": []}
        ), patch.object(autostart, "log_path", side_effect=OSError("read-only")):
            code = autostart.main(["--saved", PROFILE])

        self.assertEqual(code, 0)

    def test_the_log_is_bounded_and_keeps_the_newest_half(self) -> None:
        target = autostart.log_path(PROFILE)
        target.parent.mkdir(parents=True, exist_ok=True)
        filler = [f'{{"outcome": "tick", "n": {index}}}\n'.encode() for index in range(4000)]
        target.write_bytes(b"".join(filler))
        self.assertGreater(target.stat().st_size, autostart.LOG_MAX_BYTES)

        autostart._log(PROFILE, "legacy_autostart_retired", deleted=1)

        lines = self._lines()
        self.assertEqual(len(lines), 2001)
        self.assertEqual(lines[0]["n"], 2000)
        self.assertEqual(lines[-1]["outcome"], "legacy_autostart_retired")

    def test_status_reports_the_newest_runs_so_it_explains_itself(self) -> None:
        for index in range(7):
            autostart._log(PROFILE, "tick", n=index)

        runs = autostart.autostart_status(PROFILE)["last_runs"]
        self.assertEqual([entry["n"] for entry in runs], [2, 3, 4, 5, 6])

    def test_status_mode_does_not_cleanup_or_start_anything(self) -> None:
        buffer = io.StringIO()
        with patch.object(
            autostart, "autostart_status", return_value={"registered": False, "tasks": []}
        ), patch.object(autostart, "remove_autostart") as remove, patch(
            "karox.saved_bridge_supervisor.ensure_saved_bridge_supervisor"
        ) as ensure, contextlib.redirect_stdout(buffer):
            code = autostart.main(["--saved", PROFILE, "--status"])

        self.assertEqual(code, 0)
        self.assertFalse(json.loads(buffer.getvalue())["registered"])
        remove.assert_not_called()
        ensure.assert_not_called()

    def test_explicit_register_remains_a_developer_only_opt_in(self) -> None:
        buffer = io.StringIO()
        report = {"registered": True, "mechanism": "test"}
        with patch.object(
            autostart, "ensure_saved_bridge_autostart", return_value=report
        ) as register, patch.object(autostart, "remove_autostart") as remove, contextlib.redirect_stdout(buffer):
            code = autostart.main(["--saved", PROFILE, "--register"])

        self.assertEqual(code, 0)
        register.assert_called_once_with(PROFILE)
        remove.assert_not_called()
        self.assertTrue(json.loads(buffer.getvalue())["registered"])

    def test_removal_takes_the_run_log_with_it(self) -> None:
        autostart._log(PROFILE, "legacy_autostart_retired", deleted=1)
        log = autostart.log_path(PROFILE)
        self.assertTrue(log.exists())

        report = autostart.remove_autostart(PROFILE)

        self.assertFalse(log.exists())
        self.assertIn(str(log), report["deleted"])


if __name__ == "__main__":
    unittest.main()
