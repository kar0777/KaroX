"""Finding local model servers, and the catalog/preset additions for 4.5 A."""

from __future__ import annotations

import contextlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from karox import cli
from karox.local_runtimes import RuntimeKind, scan_local_runtimes
from karox.provider_catalog import provider_catalog
from karox.provider_presets import provider_preset


def _listing(owner: str, *ids: str) -> bytes:
    return json.dumps(
        {"object": "list", "data": [{"id": item, "object": "model", "owned_by": owner} for item in ids]}
    ).encode()


@contextlib.contextmanager
def _server(status: int, body: bytes, content_type: str = "application/json") -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/v1/models":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_finds_servers_names_them_and_skips_what_is_not_a_model_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A proxy in the environment must not swallow loopback probes.
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("ALL_PROXY", f"http://127.0.0.1:{_closed_port()}")
    with _server(200, _listing("library", "llama3.1:8b", "qwen2.5-coder:7b")) as ollama, _server(
        200, _listing("llamacpp", "model.gguf")
    ) as llama_on_vllm_port, _server(
        200, b"<html>dev server</html>", "text/html"
    ) as html, _server(404, b"{}") as missing, _server(200, b'{"object":"list","data":[]}') as empty:
        kinds = (
            RuntimeKind("ollama", "Ollama", ollama, ("library",)),
            RuntimeKind("vllm", "vLLM", llama_on_vllm_port, ("vllm",)),
            RuntimeKind("llama-cpp", "llama.cpp server", html, ("llamacpp",)),
            RuntimeKind("sglang", "SGLang", missing, ("sglang",)),
            RuntimeKind("lm-studio", "LM Studio", empty, ("organization_owner",)),
            RuntimeKind("closed", "Closed", _closed_port(), ()),
        )
        found = scan_local_runtimes(kinds=kinds, timeout=2.0)
    by_url = {item.base_url: item for item in found}
    assert set(by_url) == {
        f"http://127.0.0.1:{ollama}/v1",
        f"http://127.0.0.1:{llama_on_vllm_port}/v1",
        f"http://127.0.0.1:{empty}/v1",
    }
    first = by_url[f"http://127.0.0.1:{ollama}/v1"]
    assert (first.runtime_id, first.identified_by) == ("ollama", "owned_by")
    assert first.models == ("llama3.1:8b", "qwen2.5-coder:7b")
    # llama.cpp says who it is even on vLLM's usual port.
    second = by_url[f"http://127.0.0.1:{llama_on_vllm_port}/v1"]
    assert (second.runtime_id, second.identified_by) == ("llama-cpp", "owned_by")
    # No models loaded: still a server, named by its port.
    third = by_url[f"http://127.0.0.1:{empty}/v1"]
    assert (third.runtime_id, third.identified_by, third.models) == ("lm-studio", "port", ())
    assert first.setup_argv()[-2:] == ("--model", "llama3.1:8b")
    assert "--base-url" in first.setup_argv()


def test_two_servers_of_one_runtime_get_distinct_registry_ids() -> None:
    with _server(200, _listing("llamacpp", "a.gguf")) as one, _server(
        200, _listing("llamacpp", "b.gguf")
    ) as two:
        found = scan_local_runtimes(
            kinds=(
                RuntimeKind("llama-cpp", "llama.cpp server", one, ("llamacpp",)),
                RuntimeKind("vllm", "vLLM", two, ("vllm",)),
            ),
            timeout=2.0,
        )
    assert [item.runtime_id for item in found] == ["llama-cpp", f"llama-cpp-{two}"]


def test_cli_local_reports_finds_and_misses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KAROX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("KAROX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("karox.local_runtimes.scan_local_runtimes", lambda **_: [])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "local"]) == 0
    assert "No local model server answered" in output.getvalue()
    assert "11434" in output.getvalue()

    with _server(200, _listing("library", "llama3.1:8b")) as port:
        found = scan_local_runtimes(
            kinds=(RuntimeKind("ollama", "Ollama", port, ("library",)),), timeout=2.0
        )
    monkeypatch.setattr("karox.local_runtimes.scan_local_runtimes", lambda **_: found)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "local", "--json"]) == 0
    payload = json.loads(output.getvalue())
    assert payload["found"][0]["runtime_id"] == "ollama"
    assert payload["found"][0]["setup_argv"][3] == "local-openai"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["provider", "local"]) == 0
    text = output.getvalue()
    assert "Ollama:" in text and "provider setup local-openai" in text and "llama3.1:8b" in text


def test_azure_connects_through_its_v1_endpoint_and_reads_the_key_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    azure = provider_catalog().provider("azure")
    assert azure.installable
    assert azure.url_variables == ("AZURE_RESOURCE_NAME",)
    assert azure.key_env == ("AZURE_API_KEY",)
    monkeypatch.setenv("AZURE_RESOURCE_NAME", "my-resource")
    values = cli._endpoint_variables(azure, {})
    assert azure.resolved_base_url(values) == "https://my-resource.openai.azure.com/openai/v1"
    # An explicit --var wins over the environment.
    assert cli._endpoint_variables(azure, {"AZURE_RESOURCE_NAME": "other"}) == {
        "AZURE_RESOURCE_NAME": "other"
    }


def test_russian_presets_are_honest_about_their_state() -> None:
    yandex = provider_preset("yandex-ai-studio")
    assert yandex.installable and yandex.status == "experimental"
    assert yandex.base_url == "https://llm.api.cloud.yandex.net/v1"
    assert "gpt://" in yandex.setup_note
    gigachat = provider_preset("gigachat")
    assert not gigachat.installable
    assert "KAROX_CA_BUNDLE" in gigachat.setup_note
