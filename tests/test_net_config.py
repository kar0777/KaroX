"""Corporate-network settings: private CA bundles, the OS trust store, SOCKS."""

from __future__ import annotations

import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from karox.net_config import NetworkConfigError, client_options, socks_problem, tls_verify


def test_defaults_leave_httpx_alone() -> None:
    assert client_options({}) == {}
    assert tls_verify({"KAROX_TRUST_STORE": "certifi"}) is True


def test_a_missing_ca_bundle_is_named() -> None:
    with pytest.raises(NetworkConfigError, match="KAROX_CA_BUNDLE"):
        tls_verify({"KAROX_CA_BUNDLE": "/no/such/ca.pem"})


def test_a_non_pem_bundle_is_refused(tmp_path: Path) -> None:
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(NetworkConfigError, match="PEM"):
        tls_verify({"KAROX_CA_BUNDLE": str(bogus)})


def test_an_unknown_trust_mode_is_refused() -> None:
    with pytest.raises(NetworkConfigError):
        tls_verify({"KAROX_TRUST_STORE": "yes please"})


def test_system_trust_without_truststore_explains_the_fix(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_truststore)
    with pytest.raises(NetworkConfigError, match="truststore"):
        tls_verify({"KAROX_TRUST_STORE": "system"})


def test_a_socks_proxy_without_socksio_is_explained(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_socksio(name, *args, **kwargs):
        if name == "socksio":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_socksio)
    problem = socks_problem({"ALL_PROXY": "socks5://127.0.0.1:1080"})
    assert problem is not None and "ALL_PROXY" in problem and "socksio" in problem
    assert socks_problem({"HTTPS_PROXY": "http://proxy:3128"}) is None


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _has_openssl() -> bool:
    try:
        return subprocess.run(["openssl", "version"], capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _has_openssl(), reason="openssl CLI not available")
def test_a_private_ca_bundle_is_trusted_on_top_of_public_roots(tmp_path: Path) -> None:
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
             "-days", "2", "-subj", "/CN=KaroX Test CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign",
             "-addext", "subjectKeyIdentifier=hash", cwd=tmp_path)
    (tmp_path / "ext.cnf").write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "subjectKeyIdentifier=hash\n",
        encoding="utf-8",
    )
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr",
             "-subj", "/CN=127.0.0.1", cwd=tmp_path)
    _openssl("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "srv.pem", "-days", "2", "-extfile", "ext.cnf", cwd=tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "srv.pem"), str(tmp_path / "srv.key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{server.server_address[1]}/"
    try:
        with httpx.Client() as plain:
            with pytest.raises(httpx.ConnectError):
                plain.get(url)
        options = client_options({"KAROX_CA_BUNDLE": str(tmp_path / "ca.pem")})
        with httpx.Client(**options) as trusted:
            assert trusted.get(url).text == "ok"
    finally:
        server.shutdown()
        server.server_close()


def test_provider_reports_a_broken_network_setting_as_transport(monkeypatch) -> None:
    from karox.providers import OpenAIChatCompletionsProvider, ProviderError, ProviderErrorKind

    monkeypatch.setenv("KAROX_CA_BUNDLE", "/no/such/bundle.pem")
    provider = OpenAIChatCompletionsProvider("https://api.example/v1")
    with pytest.raises(ProviderError) as caught:
        provider._http_client()
    assert caught.value.kind is ProviderErrorKind.TRANSPORT
    assert "KAROX_CA_BUNDLE" in str(caught.value)
