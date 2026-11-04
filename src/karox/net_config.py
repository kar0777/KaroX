"""TLS trust and proxy checks shared by KaroX's provider HTTP clients.

httpx already honours ``HTTP(S)_PROXY``/``ALL_PROXY``/``NO_PROXY`` and
``SSL_CERT_FILE``. Three corporate-network cases still failed with an
unhelpful error:

* a TLS-inspecting proxy whose root CA lives only in the Windows/macOS system
  store -- ``KAROX_TRUST_STORE=system`` verifies against the OS store through
  the optional ``truststore`` package (what pip itself does);
* a private CA bundle that must be trusted *in addition to* the public roots --
  ``KAROX_CA_BUNDLE=/path/ca.pem`` adds it on top of certifi's roots
  (``SSL_CERT_FILE`` replaces them instead);
* a ``socks5://`` proxy without the optional ``socksio`` package -- httpx
  raises an ImportError deep inside client construction; this module names
  the variable and the fix instead.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

ENV_CA_BUNDLE = "KAROX_CA_BUNDLE"
ENV_TRUST_STORE = "KAROX_TRUST_STORE"
_PROXY_VARIABLES = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_SOCKS_SCHEMES = ("socks5://", "socks5h://", "socks4://", "socks4a://", "socks://")


class NetworkConfigError(ValueError):
    """The network settings in the environment cannot work as configured."""


def socks_problem(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    for name in _PROXY_VARIABLES:
        value = str(env.get(name) or "").strip().lower()
        if not value.startswith(_SOCKS_SCHEMES):
            continue
        try:
            import socksio  # noqa: F401
        except ImportError:
            return (
                f"{name} points to a SOCKS proxy, but the optional 'socksio' package "
                "is not installed; install it next to KaroX (pipx inject <venv> socksio, "
                "or pip install \"httpx[socks]\")"
            )
        return None
    return None


def tls_verify(environ: Optional[Mapping[str, str]] = None) -> Union[bool, ssl.SSLContext]:
    """``True`` for httpx's default verification, or a configured SSL context."""

    env = os.environ if environ is None else environ
    bundle = str(env.get(ENV_CA_BUNDLE) or "").strip()
    if bundle:
        path = Path(bundle).expanduser()
        if not path.is_file():
            raise NetworkConfigError(f"{ENV_CA_BUNDLE} does not point to a readable file")
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:  # pragma: no cover - certifi ships with httpx
            context = ssl.create_default_context()
        try:
            context.load_verify_locations(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise NetworkConfigError(
                f"{ENV_CA_BUNDLE} is not a PEM certificate bundle ({type(exc).__name__})"
            ) from exc
        return context
    mode = str(env.get(ENV_TRUST_STORE) or "").strip().lower()
    if mode in {"", "default", "certifi"}:
        return True
    if mode == "system":
        try:
            import truststore
        except ImportError as exc:
            raise NetworkConfigError(
                f"{ENV_TRUST_STORE}=system needs the optional 'truststore' package; "
                "install it next to KaroX (pipx inject <venv> truststore)"
            ) from exc
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    raise NetworkConfigError(f"{ENV_TRUST_STORE} must be 'system' or 'certifi'")


def client_options(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Keyword arguments for ``httpx.Client`` that apply the settings above."""

    problem = socks_problem(environ)
    if problem:
        raise NetworkConfigError(problem)
    verify = tls_verify(environ)
    return {} if verify is True else {"verify": verify}


__all__ = [
    "ENV_CA_BUNDLE",
    "ENV_TRUST_STORE",
    "NetworkConfigError",
    "client_options",
    "socks_problem",
    "tls_verify",
]
