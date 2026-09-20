"""Real hot-group imports, isolated from the pytest process and source tree.

The gates are events, not sleeps: an importer must either reach the real
importlib lock or finish before the coordinator releases the candidate exec.
Timeouts fail the test (they never silently let a candidate finish early).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

from _support import SRC, child_environment


_PRELUDE = """
import builtins
import importlib
from importlib import _bootstrap
from pathlib import Path
import sys
import threading
import karox
from karox.hot_worker import HotWorkerSupervisor

assert Path(karox.__file__).resolve().is_relative_to(Path.cwd())
supervisor = HotWorkerSupervisor()
old_worker = supervisor.module()
previous = dict(supervisor._modules)
assert len(previous) == 4
originals = {name: Path(module.__file__).read_text(encoding='utf-8')
             for name, module in previous.items()}

def write(name, source):
    Path(previous[name].__file__).write_text(source, encoding='utf-8')

def prepend(name, code):
    marker = 'from __future__ import annotations\\n'
    assert marker in originals[name]
    write(name, originals[name].replace(marker, marker + code, 1))

def check_group(modules):
    for name, module in modules.items():
        assert sys.modules[name] is module, name
        assert getattr(karox, name.rpartition('.')[2]) is module, name
        assert not getattr(module.__spec__, '_initializing', False), name
    tx = modules['karox.workspace_transaction']
    assert modules['karox.unified_patch'].WorkspaceTransaction is tx.WorkspaceTransaction
    assert modules['karox.workspace_worker'].WorkspaceTransaction is tx.WorkspaceTransaction

# Also cover the package-attribute dependency spelling in the real group.
worker_name = 'karox.workspace_worker'
ordering_probe = '\\nfrom karox import workspace_transaction as _package_dependency\\n'
"""


def _run_actual(tmp_path: Path, body: str) -> None:
    shutil.copytree(SRC / "karox", tmp_path / "karox",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    environment = child_environment(
        config_dir=tmp_path / "config", runtime_dir=tmp_path / "runtime",
        legacy_config_dir=tmp_path / "legacy", PYTHONPATH=str(tmp_path),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", _PRELUDE + textwrap.dedent(body)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name,member", [
    ("karox.workspace_transaction", "WorkspaceTransaction"),
    ("karox.unified_patch", "execute_apply_patch"),
    ("karox.desktop_apps", "execute_desktop_app_action"),
    ("karox.workspace_worker", "execute_tests"),
])
@pytest.mark.parametrize("failure", [None, "RuntimeError", "SystemExit"])
def test_ordinary_imports_never_receive_half_executed_real_group(
    tmp_path: Path, name: str, member: str, failure: str | None,
) -> None:
    body = f"name = {name!r}\nmember = {member!r}\nfailure = {failure!r}\n"
    body += """
attribute = name.rpartition('.')[2]
entered = builtins._hot_entered = threading.Event()
release = builtins._hot_release = threading.Event()
# Preserve the real sources, adding only synchronization and dependency probes.
originals[worker_name] += ordering_probe
pause = ('import builtins\\nbuiltins._hot_entered.set()\\n'
         "assert builtins._hot_release.wait(15), 'exec release timed out'\\n")
if failure:
    pause += f"raise {failure}('publication regression')\\n"
prepend(name, pause)
if name != worker_name:
    write(worker_name, originals[worker_name])

reached_import = threading.Event()
import_done = threading.Event()
from_done = threading.Event()
reload_done = threading.Event()
results, errors = {}, {}
acquire = _bootstrap._ModuleLock.acquire

def observe_acquire(lock):
    if (threading.current_thread().name == 'ordinary-import'
            and lock.name == name and not reached_import.is_set()):
        # Prove contention, not just that this thread was scheduled near import.
        assert lock.owner == threads[0].ident, 'reload does not own the import lock'
        reached_import.set()
    return acquire(lock)

_bootstrap._ModuleLock.acquire = observe_acquire

def reload():
    try:
        results['reload'] = supervisor.module()
    except BaseException as exc:
        errors['reload'] = exc
    finally:
        reload_done.set()

def ordinary_import():
    try:
        results['import'] = importlib.import_module(name)
    except BaseException as exc:
        errors['import'] = exc
    finally:
        import_done.set()
        reached_import.set()  # Detect a broken cached-import fast path too.

def from_import():
    try:
        namespace = {}
        exec(f'from karox import {attribute} as dependency', namespace)
        results['from'] = namespace['dependency']
    except BaseException as exc:
        errors['from'] = exc
    finally:
        from_done.set()

threads = [threading.Thread(target=reload, daemon=True),
           threading.Thread(target=ordinary_import, name='ordinary-import', daemon=True),
           threading.Thread(target=from_import, daemon=True)]
threads[0].start()
try:
    assert entered.wait(10), errors
    assert not reload_done.is_set(), errors
    candidate = sys.modules[name]
    assert not hasattr(candidate, member), 'gate must precede real definitions'
    threads[1].start()
    threads[2].start()
    assert reached_import.wait(10), 'ordinary importer never reached the gate'
    assert from_done.wait(10), 'existing parent attribute must remain importable'
    assert 'from' not in errors, errors
    assert results['from'] is previous[name], 'from-import exposed a candidate during exec'
    assert hasattr(results['from'], member)
    assert not import_done.is_set(), ('cached import did not wait', results, errors)
    assert candidate.__spec__ is not previous[name].__spec__
    assert candidate.__spec__._initializing
    assert not getattr(previous[name].__spec__, '_initializing', False)
finally:
    release.set()
    for thread in threads:
        if thread.ident is not None:
            thread.join(10)
            assert not thread.is_alive(), 'import/reload deadlocked'
    _bootstrap._ModuleLock.acquire = acquire

assert 'import' not in errors, errors
assert not candidate.__spec__._initializing
assert hasattr(results['import'], member)
if failure:
    assert results['import'] is previous[name], 'blocked import did not re-read rollback'
    if failure == 'SystemExit':
        assert isinstance(errors.get('reload'), SystemExit), errors
    else:
        assert not errors, errors
        assert results['reload'] is old_worker
        assert 'RuntimeError' in supervisor._last_reload_error
    assert supervisor._generation == 1
    assert supervisor._reload_count == 0
    assert supervisor._module is old_worker
    check_group(previous)
    # Repair without forcing: neither locks nor negative caches may pin failure.
    for module_name, source in originals.items():
        write(module_name, source + '\\n# repaired generation\\n')
    assert supervisor.module() is not old_worker
else:
    assert not errors, errors
    assert results['import'] is candidate
    assert results['reload'] is not old_worker

check_group(supervisor._modules)
assert supervisor._module._package_dependency is supervisor._modules['karox.workspace_transaction']
assert supervisor.status()['generation'] == 2
assert supervisor.status()['reload_count'] == 1
assert supervisor.status()['last_reload_error'] is None
# Reentrant module locks have actually been released, not merely bypassed by
# now-cached imports. Acquire them from another thread in a second generation.
again = threading.Thread(target=lambda: supervisor.module(force=True), daemon=True)
again.start()
again.join(10)
assert not again.is_alive(), 'a generation leaked its import locks'
assert supervisor._generation == 3
check_group(supervisor._modules)
"""
    _run_actual(tmp_path, body)


def test_reload_does_not_hold_global_lock_while_waiting_for_dependency(tmp_path: Path) -> None:
    _run_actual(tmp_path, """
helper_entered = builtins._helper_entered = threading.Event()
helper_release = builtins._helper_release = threading.Event()
reload_waiting = threading.Event()
Path('_hot_import_leaf.py').write_text('VALUE = 17\\n', encoding='utf-8')
Path('_hot_import_helper.py').write_text(
    'import builtins\\nbuiltins._helper_entered.set()\\n'
    "assert builtins._helper_release.wait(15), 'helper release timed out'\\n"
    'import _hot_import_leaf\\nVALUE = _hot_import_leaf.VALUE\\n', encoding='utf-8')
errors, results = {}, {}
acquire = _bootstrap._ModuleLock.acquire

def observe_acquire(lock):
    if threading.current_thread().name == 'reload' and lock.name == '_hot_import_helper':
        reload_waiting.set()
    return acquire(lock)

_bootstrap._ModuleLock.acquire = observe_acquire

def import_helper():
    try:
        results['helper'] = importlib.import_module('_hot_import_helper')
    except BaseException as exc:
        errors['helper'] = exc

def reload():
    try:
        results['reload'] = supervisor.module()
    except BaseException as exc:
        errors['reload'] = exc

helper = threading.Thread(target=import_helper, daemon=True)
loader = threading.Thread(target=reload, name='reload', daemon=True)
helper.start()
try:
    assert helper_entered.wait(10)
    prepend('karox.workspace_transaction', 'import _hot_import_helper\\n')
    loader.start()
    assert reload_waiting.wait(10), 'reload never tried to import the busy dependency'
finally:
    # The helper must obtain the global import lock for its *next* import before
    # releasing its own module lock. Holding _imp across exec deadlocks here.
    helper_release.set()
    for thread in (helper, loader):
        if thread.ident is not None:
            thread.join(10)
            assert not thread.is_alive(), 'global/module import lock inversion'
    _bootstrap._ModuleLock.acquire = acquire
assert not errors, errors
assert results['helper'].VALUE == 17
assert results['reload'] is not old_worker
assert supervisor._modules['karox.workspace_transaction']._hot_import_helper is results['helper']
check_group(supervisor._modules)
""")


def test_real_group_negative_cache_recovers_when_missing_import_arrives(tmp_path: Path) -> None:
    """Promoted from the peer's standalone real-product adversarial test."""
    _run_actual(tmp_path, """
source = originals[worker_name] + '\\nimport _hot_late_dependency\\n'
write(worker_name, source)
assert supervisor.module() is old_worker
assert 'ModuleNotFoundError' in supervisor.status()['last_reload_error']
check_group(previous)
assert supervisor._generation == 1
Path('_hot_late_dependency.py').write_text('VALUE = 17\\n', encoding='utf-8')
importlib.invalidate_caches()
assert Path(old_worker.__file__).read_text(encoding='utf-8') == source
recovered = supervisor.module()
assert recovered is not old_worker
assert recovered._hot_late_dependency.VALUE == 17
assert supervisor.status()['generation'] == 2
assert supervisor.status()['reload_count'] == 1
assert supervisor.status()['last_reload_error'] is None
check_group(supervisor._modules)
""")


@pytest.mark.parametrize("failure", [None, "KeyboardInterrupt"])
def test_from_import_without_parent_attribute_waits_and_recovers(
    tmp_path: Path, failure: str | None,
) -> None:
    _run_actual(tmp_path, f"failure = {failure!r}\n" + """
name = 'karox.workspace_transaction'
del karox.workspace_transaction
entered = builtins._hot_entered = threading.Event()
release = builtins._hot_release = threading.Event()
reached = threading.Event()
done = threading.Event()
results, errors = {}, {}
pause = ('import builtins\\nbuiltins._hot_entered.set()\\n'
         "assert builtins._hot_release.wait(15), 'exec release timed out'\\n")
if failure:
    pause += f"raise {failure}('missing attribute rollback')\\n"
prepend(name, pause)
acquire = _bootstrap._ModuleLock.acquire

def observe_acquire(lock):
    if (threading.current_thread().name == 'from-import'
            and lock.name == name and not reached.is_set()):
        assert lock.owner == loader.ident, 'reload does not own the import lock'
        reached.set()
    return acquire(lock)

_bootstrap._ModuleLock.acquire = observe_acquire

def reload():
    try:
        results['reload'] = supervisor.module()
    except BaseException as exc:
        errors['reload'] = exc

def importer():
    try:
        from karox import workspace_transaction
        results['from'] = workspace_transaction
    except BaseException as exc:
        errors['from'] = exc
    finally:
        done.set()
        reached.set()

loader = threading.Thread(target=reload, daemon=True)
reader = threading.Thread(target=importer, name='from-import', daemon=True)
loader.start()
try:
    assert entered.wait(10)
    reader.start()
    assert reached.wait(10)
    assert not done.is_set(), 'missing parent attribute leaked an incomplete module'
finally:
    release.set()
    for thread in (loader, reader):
        if thread.ident is not None:
            thread.join(10)
            assert not thread.is_alive(), 'missing-attribute import deadlocked'
    _bootstrap._ModuleLock.acquire = acquire
assert 'from' not in errors, errors
assert hasattr(results['from'], 'WorkspaceTransaction')
if failure:
    assert isinstance(errors.get('reload'), KeyboardInterrupt), errors
    assert results['from'] is previous[name]
    assert not hasattr(karox, 'workspace_transaction'), 'rollback invented an old attribute'
    for module_name, module in previous.items():
        assert sys.modules[module_name] is module
        assert not getattr(module.__spec__, '_initializing', False)
    write(name, originals[name] + '\\n# recovered attribute\\n')
    assert supervisor.module() is not old_worker
else:
    assert not errors, errors
    assert results['from'] is supervisor._modules[name]
check_group(supervisor._modules)
assert supervisor.status()['generation'] == 2
""")


def test_real_group_validation_failure_restores_known_good_and_recovers(tmp_path: Path) -> None:
    _run_actual(tmp_path, """
write(worker_name, originals[worker_name] + '\\nexecute_tests = None\\n')
assert supervisor.module() is old_worker
assert 'misses entry points' in supervisor._last_reload_error
assert supervisor._generation == 1
assert supervisor._reload_count == 0
check_group(previous)
write(worker_name, originals[worker_name] + ordering_probe)
assert supervisor.module() is not old_worker
check_group(supervisor._modules)
assert supervisor._module._package_dependency is supervisor._modules['karox.workspace_transaction']
assert supervisor.status()['generation'] == 2
assert supervisor.status()['last_reload_error'] is None
""")
