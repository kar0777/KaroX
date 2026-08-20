"""Explicit release-acceptance harness for the non-editable KaroX wheel.

This file intentionally does not match unittest/pytest discovery naming. Run it
explicitly after building ``dist/karox_runtime-*.whl``. The child interpreter
installs that wheel into a disposable venv, runs outside the checkout with
PYTHONPATH removed, and exercises the packaged MCP/autonomy surfaces only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_INSTALLED_SCRIPT = r'''
from __future__ import annotations
import json, os, sys, time
from pathlib import Path
import anyio, karox
from karox.autonomy_runtime import AUTONOMY_TOOL_NAMES, AutonomyRuntime
from karox.hosted_bridge import CompositeHostedBridge, CoreToolBridge
from karox.hosted_tools_runtime import CHECKS_CANCEL, CHECKS_LOGS, CHECKS_START, CHECKS_STATUS, HostedToolsRuntime
from karox.models import AccessProfile, Origin, OriginKind
from karox.paths import session_dir
from karox.proxy_server import build_proxy_asgi_app, wire_tool_name
from karox.sessions import SessionStore

repo = Path(os.environ["KAROX_ACCEPTANCE_REPO"]).resolve()
source_root = Path(os.environ["KAROX_ACCEPTANCE_SOURCE_ROOT"]).resolve()
venv_root = Path(os.environ["KAROX_ACCEPTANCE_VENV"]).resolve()
module_path = Path(karox.__file__).resolve()
assert source_root not in module_path.parents, (source_root, module_path)
assert venv_root in module_path.parents, (venv_root, module_path)
assert Path.cwd().resolve() != source_root
assert "PYTHONPATH" not in os.environ

store = SessionStore(session_dir())
session_id = "installed-wheel-autonomy-acceptance"
store.create(repo, "Verify installed-wheel high-level tools", access_profile=AccessProfile.WORKSPACE_WRITE, branch="main", session_id=session_id)
origin = Origin(OriginKind.HOSTED_CLIENT, "installed-wheel-acceptance")
check_argv = (sys.executable, "-c", "import time; print('installed-wheel-managed-check', flush=True); time.sleep(30)")
verification_commands = (check_argv,)
core_tools = ("karox.repo.read_file", "karox.repo.search", "karox.git.status")
managed_tools = (CHECKS_START, CHECKS_STATUS, CHECKS_LOGS, CHECKS_CANCEL)

def build_runtime():
    core = CoreToolBridge(repo, store, session_id, core_tools, hosted_origin=origin, verification_commands=verification_commands)
    hosted = HostedToolsRuntime(repo, store, session_id, managed_tools, access_profile=AccessProfile.WORKSPACE_WRITE, hosted_origin=origin, verification_commands=verification_commands)
    operations = CompositeHostedBridge((core, hosted))
    autonomy = AutonomyRuntime(repo, store, session_id, tuple(sorted(AUTONOMY_TOOL_NAMES)), access_profile=AccessProfile.WORKSPACE_WRITE, hosted_origin=origin, connection_profile="installed-wheel-acceptance", verification_commands=verification_commands, operation_runtime=operations, client_kind="installed-wheel-acceptance")
    return core, hosted, autonomy, CompositeHostedBridge((core, hosted, autonomy))

async def asgi_request(app, payload):
    body = json.dumps(payload).encode("utf-8")
    scope = {"type":"http","asgi":{"version":"3.0","spec_version":"2.3"},"http_version":"1.1","method":"POST","path":"/mcp","raw_path":b"/mcp","root_path":"","scheme":"http","query_string":b"","headers":[(b"host",b"127.0.0.1:8765"),(b"authorization",b"Bearer installed-wheel-synthetic-token"),(b"content-type",b"application/json"),(b"accept",b"application/json, text/event-stream")],"client":("127.0.0.1",54321),"server":("127.0.0.1",8765)}
    pending = [{"type":"http.request","body":body,"more_body":False}]
    state = {"status":0,"chunks":[]}
    async def receive(): return pending.pop(0) if pending else {"type":"http.disconnect"}
    async def send(message):
        if message["type"] == "http.response.start": state["status"] = message["status"]
        elif message["type"] == "http.response.body": state["chunks"].append(message.get("body",b""))
    await app(scope, receive, send)
    raw = b"".join(state["chunks"]).decode("utf-8")
    assert state["status"] == 200, (state["status"], raw)
    return json.loads(raw)

async def drive(app, payloads):
    send_to_app, recv_for_app = anyio.create_memory_object_stream(10)
    send_from_app, recv_from_app = anyio.create_memory_object_stream(10)
    results=[]
    async with send_to_app, recv_for_app, send_from_app, recv_from_app, anyio.create_task_group() as group:
        group.start_soon(app, {"type":"lifespan"}, recv_for_app.receive, send_from_app.send)
        await send_to_app.send({"type":"lifespan.startup"}); assert (await recv_from_app.receive())["type"] == "lifespan.startup.complete"
        for payload in payloads: results.append(await asgi_request(app,payload))
        await send_to_app.send({"type":"lifespan.shutdown"}); assert (await recv_from_app.receive())["type"] == "lifespan.shutdown.complete"
    return results

def run_payloads(runtime,payloads):
    app=build_proxy_asgi_app(runtime,"installed-wheel-synthetic-token")
    return anyio.run(drive,app,payloads)
def call_payload(i,name,args): return {"jsonrpc":"2.0","id":i,"method":"tools/call","params":{"name":wire_tool_name(name),"arguments":args}}
def structured(response):
    result=response["result"]; assert not result.get("isError"), response; return result.get("structuredContent") or {}

core, hosted, autonomy, composite = build_runtime()
app = build_proxy_asgi_app(composite, "installed-wheel-synthetic-token")
initialize, listed = run_payloads(composite,[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"installed-wheel-acceptance","version":"1"}}},{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}])
assert initialize["result"]["serverInfo"]["name"] == "karox-proxy", initialize
wire_names={item["name"] for item in listed["result"]["tools"]}
required_internal=set(core_tools)|set(managed_tools)|set(AUTONOMY_TOOL_NAMES)
required_wire={wire_tool_name(name) for name in required_internal}
assert required_wire.issubset(wire_names), sorted(required_wire-wire_names)

low_read, low_git = run_payloads(composite,[call_payload(3,"karox.repo.read_file",{"path":"README.md"}),call_payload(4,"karox.git.status",{})])
assert "installed wheel fixture" in json.dumps(structured(low_read)).lower()
assert structured(low_git).get("ok",True) is not False

bootstrap_args={"objective":"Verify installed wheel autonomy"}
first_bootstrap,replay_bootstrap=run_payloads(composite,[call_payload(5,"karox.task.bootstrap",bootstrap_args),call_payload(6,"karox.task.bootstrap",bootstrap_args)])
first_boot=structured(first_bootstrap); replayed_boot=structured(replay_bootstrap)
assert first_boot["idempotent_replay"] is False, first_boot
assert replayed_boot["idempotent_replay"] is True, replayed_boot

status_response,resume_response,inspect_response,plan_response=run_payloads(composite,[call_payload(7,"karox.task.status",{}),call_payload(8,"karox.task.resume",{}),call_payload(9,"karox.repo.inspect",{"goal":"README fixture","depth":"focused"}),call_payload(10,"karox.task.execute_plan",{"operations":[{"operation_id":"read-readme","action":"read","inputs":{"path":"README.md"}}],"stop_on_error":True})])
assert structured(status_response).get("ok",True) is not False
assert structured(resume_response).get("ok",True) is not False
assert structured(inspect_response).get("ok",True) is not False
assert structured(plan_response).get("ok",True) is not False

held=store.acquire(session_id,"acceptance-external-holder",ttl_seconds=60.0)
try:
    (blocked,)=run_payloads(composite,[call_payload(11,"karox.task.bootstrap",{"objective":"Lease must block this mutation"})])
    assert blocked["result"].get("isError") is True, blocked
finally: store.release(held)
(recovered,)=run_payloads(composite,[call_payload(12,"karox.task.bootstrap",{"objective":"Lease must block this mutation"})])
assert structured(recovered)["idempotent_replay"] is False

(start_response,)=run_payloads(composite,[call_payload(13,CHECKS_START,{"kind":"check","argv":list(check_argv),"timeout_seconds":60})])
start=structured(start_response); job_id=start["job_id"]; assert job_id
for _ in range(50):
    (status_wire,)=run_payloads(composite,[call_payload(14,CHECKS_STATUS,{"job_id":job_id})]); job_status=structured(status_wire)
    if job_status.get("state") in {"running","passed","failed","cancelled","timed_out"}: break
    time.sleep(0.05)
(log_wire,)=run_payloads(composite,[call_payload(15,CHECKS_LOGS,{"job_id":job_id,"limit":40})]); assert structured(log_wire).get("ok",True) is not False
(cancel_wire,)=run_payloads(composite,[call_payload(16,CHECKS_CANCEL,{"job_id":job_id})]); assert structured(cancel_wire).get("ok",True) is not False

core2,hosted2,autonomy2,composite2=build_runtime(); app2=build_proxy_asgi_app(composite2,"installed-wheel-synthetic-token")
post_status,post_resume,post_replay,post_job=run_payloads(composite2,[call_payload(17,"karox.task.status",{}),call_payload(18,"karox.task.resume",{}),call_payload(19,"karox.task.bootstrap",bootstrap_args),call_payload(20,CHECKS_STATUS,{"job_id":job_id})])
assert structured(post_status).get("ok",True) is not False
assert structured(post_resume).get("ok",True) is not False
assert structured(post_replay)["idempotent_replay"] is True
assert structured(post_job).get("job_id") == job_id

(checkpoint_wire,)=run_payloads(composite2,[call_payload(21,"karox.task.checkpoint",{"updates":{"current_phase":{"value":"installed-wheel-acceptance-passed"}}})])
assert structured(checkpoint_wire).get("ok",True) is not False
assert wire_tool_name("karox.checks.run_affected") in wire_names
print(json.dumps({"ok":True,"installed_module":str(module_path),"tool_count":len(wire_names),"required_tools":sorted(required_internal),"idempotent_replay":True,"lease_blocked":True,"restart_recovery":True,"managed_job_id":job_id},sort_keys=True))
'''


def test_installed_wheel_autonomy_acceptance() -> None:
    wheels=sorted((ROOT/"dist").glob("karox_runtime-*.whl"),key=lambda p:p.stat().st_mtime)
    assert wheels, "build the wheel before running installed-wheel acceptance"
    wheel=wheels[-1].resolve()
    with tempfile.TemporaryDirectory(prefix="karox-wheel-autonomy-") as raw_temp:
        temp=Path(raw_temp).resolve(); venv=temp/"venv"; repository=temp/"fixture-repo"; outside=temp/"outside-checkout"; runtime=temp/"runtime"; config=temp/"config"
        repository.mkdir(); outside.mkdir(); runtime.mkdir(); config.mkdir(); (repository/"README.md").write_text("Installed wheel fixture\n",encoding="utf-8")
        subprocess.run(["git","init","-b","main",str(repository)],check=True,capture_output=True)
        subprocess.run(["git","-C",str(repository),"config","user.email","acceptance@example.invalid"],check=True)
        subprocess.run(["git","-C",str(repository),"config","user.name","KaroX Acceptance"],check=True)
        subprocess.run(["git","-C",str(repository),"add","README.md"],check=True)
        subprocess.run(["git","-C",str(repository),"commit","-m","fixture"],check=True,capture_output=True)
        subprocess.run([sys.executable,"-m","venv","--system-site-packages",str(venv)],check=True,capture_output=True,text=True)
        venv_python=venv/("Scripts/python.exe" if os.name=="nt" else "bin/python")
        install=subprocess.run([str(venv_python),"-m","pip","install","--disable-pip-version-check","--no-deps","--force-reinstall",str(wheel)],check=True,capture_output=True,text=True,timeout=120)
        assert "Successfully installed" in install.stdout
        env=os.environ.copy(); env.pop("PYTHONPATH",None); env.update({"KAROX_VNEXT_RUNTIME_DIR":str(runtime),"KAROX_VNEXT_CONFIG_DIR":str(config),"KAROX_ACCEPTANCE_REPO":str(repository),"KAROX_ACCEPTANCE_SOURCE_ROOT":str(ROOT),"KAROX_ACCEPTANCE_VENV":str(venv)})
        result=subprocess.run([str(venv_python),"-c",_INSTALLED_SCRIPT],cwd=outside,env=env,capture_output=True,text=True,timeout=150)
        assert result.returncode==0,f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        lines=[line for line in result.stdout.splitlines() if line.strip().startswith("{")]; assert lines,result.stdout
        evidence=json.loads(lines[-1]); assert evidence["ok"] is True; assert evidence["idempotent_replay"] is True; assert evidence["lease_blocked"] is True; assert evidence["restart_recovery"] is True
        assert Path(evidence["installed_module"]).resolve().is_relative_to(venv)
