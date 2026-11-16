"""Secret masking for what `karox mcp` sends back to its client.

Tool output leaves the machine and lands in a third-party chat log, so likely secrets
are replaced by ``[secret removed: <kind>]`` before a reply goes out. Agent-mode replies
are JSON text, so every rule keeps JSON valid (a replaced span never holds a quote or
half of a backslash escape) and reads the ``\\n``, ``\\r`` and ``\\t`` escapes as the
line breaks and tabs they stand for. No rule has nested quantifiers that can backtrack
badly: a 100k-character reply takes milliseconds.

Not masked on purpose: random-looking strings with no known prefix and no secret-ish
name next to them (git SHAs, UUIDs, lockfile hashes, base64 data), so a coding model
still sees those.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

MARK = "[secret removed: "
MIN_LEN = 8

# A token starts outside a word, and not right after a backslash (that would split a
# JSON escape), but right after a \n, \r or \t escape it does.
_START = r"(?:(?<![A-Za-z0-9_\\-])|(?<=\\[nrt]))"

_TOKEN_RULES = {
    "anthropic": ("Anthropic API key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openrouter": ("OpenRouter API key", r"sk-or-[A-Za-z0-9_-]{20,}"),
    # A digit is required: kebab-case words such as sk-spinner-... are not keys.
    "openai": ("API key", r"sk-(?=[A-Za-z_-]*[0-9])[A-Za-z0-9_-]{20,}"),
    "stripe": ("Stripe key", r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    "github": ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "gitlab": ("GitLab token", r"glpat-[A-Za-z0-9_-]{20,}"),
    "slack": ("Slack token", r"(?:xox[abposr]|xapp)-[A-Za-z0-9-]{10,}"),
    "aws": ("AWS access key id", r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"),
    "google": ("Google API key", r"AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    # Mixed case is required: a long snake_case name after hf_ is code, not a token.
    "huggingface": ("Hugging Face token", r"hf_(?=[a-z0-9]*[A-Z])[A-Za-z0-9]{30,}"),
    "npm": ("npm token", r"npm_[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    "pypi": ("PyPI token", r"pypi-[A-Za-z0-9_-]{50,}"),
    "jwt": ("JWT", r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
}
_TOKENS = re.compile(
    _START + "(?:" + "|".join(f"(?P<{n}>{rx})" for n, (_, rx) in _TOKEN_RULES.items()) + ")"
)

_PEM_NAME = r"(?:[A-Z0-9]{1,20} ){0,4}PRIVATE KEY(?: BLOCK)?-----"
_BEGIN = "-----BEGIN " + _PEM_NAME
_END = "-----END " + _PEM_NAME
_LINE_NO = r"(?:[ \t]*[0-9]{1,7}(?::[ \t]?|\t))?"  # read, cat -n and grep -n number lines
_BREAK = r"[ \t]*(?:\r?\n|\\r\\n|\\n)"
_B64 = r"[A-Za-z0-9+/=]+"
# The body stops at a quote (a JSON string's end) and at the next BEGIN, so headers
# without an END cost a short scan each, not 20000 characters each.
_PEM_BLOCK = re.compile(_BEGIN + r"(?:[^\"\\-]|\\[\s\S]|-(?!----BEGIN )){0,20000}?" + _END)
# A key cut short (an output cap, `head`): the header and the key lines after it.
_PEM_HEAD = re.compile(
    _BEGIN
    + r"(?:"
    + _BREAK
    + _LINE_NO
    + r"[ \t]*(?:"
    + _B64
    + r"[ \t]*(?=\r?\n|\\[rn]|[\"']|$)|[A-Za-z-]+: [^\r\n\\\"]*))*"
)
# Key lines above an END line whose BEGIN is not in the text (`tail`, a read from an
# offset, an output cut in the middle); a match is masked only if END follows it.
_PEM_TAIL = re.compile(
    r"(?:(?<![^\n])|(?<=\\n))(?:" + _LINE_NO + r"[ \t]*" + _B64 + "(?P<brk>" + _BREAK + "))+"
)
_PEM_END = re.compile(_LINE_NO + r"[ \t]*" + _END)
_PRIVATE_KEY = MARK + "private key]"

_AUTH = re.compile(
    r"(?i:authorization)(?:\\{0,3}[\"'])?[ \t]{0,3}[:=][ \t]{0,3}(?:\\{0,3}[\"'])?[ \t]{0,3}"
    r"(?:(?i:bearer|basic|token|digest|bot)[ \t]{1,3})?(?P<cred>[A-Za-z0-9._~+/=:-]{8,})"
)
# Without an Authorization header name: only the capitalised scheme and a token that has
# a digit, so prose ("Bearer authentication") stays.
_BEARER = re.compile(
    _START + r"(?:Bearer|Basic)[ \t]{1,3}(?P<cred>(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{16,})"
)
_URL_CREDS = re.compile(r"://[^\s:/?#@\"'\\<>]{0,200}:(?P<cred>[^\s/?#@\"'\\<>]{1,400})@")

# KEY=value, key: value, "key": "value", key => 'value'. The lookahead keeps the regex
# from trying every word: only names holding a secret-ish word go on.
_ASSIGN = re.compile(
    r"(?:(?<![\w.\\-])|(?<=\\[nrt]))"
    r"(?=[\w.-]{0,60}?(?i:pass|secret|credential|key|token))"
    r"(?P<key>[\w.-]{1,80})(?:\\{0,3}[\"'])?[ \t]{0,3}(?P<sep>=>|:=|[:=](?!=))[ \t]{0,3}"
    r"(?:(?P<q>\\{0,3}[\"'])(?P<qv>[^\"'\\\r\n]{1,2000})(?P=q)"
    r"|(?P<bv>[^\s\"'`\\,;&|<>(){}\[\]]{1,2000}))"
)
_SECRET_WORD = re.compile(
    r"passw(?:or)?d|passphrase|secret|credential|token(?!s|iz)|(?:^|_)pass(?:_|$)"
    r"|(?:api|access|private|auth|encryption|signing|master)_?key"
)
_KINDS = (("pass", "password"), ("secret", "secret"), ("token", "token"), ("api", "API key"))
# A secret-ish name ending in one of these names something about a secret, not the
# secret: token_url, secret_name, password_file, api_key_env (token counts: token(?!s)).
_PLAIN_LAST = frozenset(
    {
        *("file", "path", "dir", "url", "uri", "endpoint", "type", "name", "names", "env"),
        *("var", "header", "prefix", "length", "len", "count", "limit", "prompt", "label"),
        *("help", "field", "format", "kind", "mode", "ttl", "expiry", "expires"),
        *("expiration", "enabled", "required", "regex", "pattern", "schema"),
    }
)
_NAME_PARTS = re.compile(r"[_.\-\s]+|(?<=[a-z0-9])(?=[A-Z])")
_TEMPLATE = re.compile(
    r"^[$%]\{?[A-Za-z_(]|<[^>]*>|\{[^}]*\}|x{4}|\*{3}|\.{3}|your|changeme|placeholder|redacted"
    r"|secret removed",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"[0-9.,_:+-]+")
_WORD = re.compile(r"[A-Za-z_]+")
_DOTTED = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_UNSAFE = re.compile(r"[\s\"\\]")  # exact values with these could cut through JSON escapes


def redact(text: str) -> str:
    """``text`` with likely secrets replaced by ``[secret removed: <kind>]``."""
    if "PRIVATE KEY" in text:
        text = _PEM_BLOCK.sub(_PRIVATE_KEY, text)
        text = _PEM_HEAD.sub(_PRIVATE_KEY, text)
        if "-----END" in text:
            text = _PEM_TAIL.sub(_key_tail, text)
    text = _TOKENS.sub(_token, text)
    text = _AUTH.sub(_credentials, text)
    text = _BEARER.sub(_credentials, text)
    if "://" in text:
        text = _URL_CREDS.sub(_url_password, text)
    return _ASSIGN.sub(_assignment, text)


def secret_name(name: str) -> bool:
    """Whether a variable or key name says its value is a secret (DB_PASSWORD, apiKey,
    client-secret), and not something about one (token_url, secret_name, max_tokens)."""
    parts = [p.lower() for p in _NAME_PARTS.split(name) if p]
    if not parts or parts[-1] in _PLAIN_LAST:
        return False
    return _SECRET_WORD.search("_".join(parts)) is not None


class Redactor:
    """redact() plus exact values known to be secret: this process's secret-looking
    environment variables and the server's own access token."""

    def __init__(self) -> None:
        self._labels: dict[str, str] = {}  # value -> what the marker calls it
        self._exact: re.Pattern[str] | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Redactor:
        redactor = cls()
        for name, value in (os.environ if environ is None else environ).items():
            secret = secret_name(name) or name.upper().endswith("KEY")
            if secret and not _plain(value) and not _path(value):
                redactor.add(value, f"${name}")
        return redactor

    def add(self, value: str, label: str) -> None:
        """Mask ``value`` wherever it appears. Values under MIN_LEN characters would mask
        ordinary text; values with spaces, quotes or backslashes are left to the patterns
        (in JSON text they could cut through an escape)."""
        if len(value) < MIN_LEN or _UNSAFE.search(value):
            return
        self._labels.setdefault(value, label)
        longest_first = sorted(self._labels, key=len, reverse=True)
        self._exact = re.compile("|".join(map(re.escape, longest_first)))

    def __call__(self, text: str) -> str:
        if self._exact is not None:
            text = self._exact.sub(self._exact_mark, text)
        return redact(text)

    def _exact_mark(self, m: re.Match[str]) -> str:
        if _escaped(m.string, m.start()):
            return m.group()
        return f"{MARK}{self._labels[m.group()]}]"


def redact_json(data: Any, mask: Callable[[str], str]) -> Any:
    """``data`` with ``mask`` applied to its JSON text, so a key and its value are seen
    together ("password": "...")."""
    text = json.dumps(data, ensure_ascii=False)
    masked = mask(text)
    if masked == text:
        return data
    try:
        return json.loads(masked)
    except ValueError:  # not expected (the rules keep JSON valid): mask string by string
        return _each_string(data, mask)


def _each_string(value: Any, mask: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list | tuple):
        return [_each_string(v, mask) for v in value]
    if isinstance(value, dict):
        return {mask(str(k)): _each_string(v, mask) for k, v in value.items()}
    return value


def _token(m: re.Match[str]) -> str:
    return f"{MARK}{_TOKEN_RULES[m.lastgroup or 'openai'][0]}]"


def _key_tail(m: re.Match[str]) -> str:
    return _PRIVATE_KEY + m.group("brk") if _PEM_END.match(m.string, m.end()) else m.group()


def _credentials(m: re.Match[str]) -> str:
    if _TEMPLATE.search(m.group("cred")):
        return m.group()
    return _swap(m, "cred", "credentials")


def _url_password(m: re.Match[str]) -> str:
    if _TEMPLATE.search(m.group("cred")):
        return m.group()
    return _swap(m, "cred", "password")


def _assignment(m: re.Match[str]) -> str:
    key = m.group("key")
    group = "qv" if m.group("qv") is not None else "bv"
    value = m.group(group)
    if not secret_name(key) or _plain(value) or value.lower() == key.lower():
        return m.group()
    if group == "bv" and _code(m, key, value):
        return m.group()
    lower = key.lower()
    return _swap(m, group, next((kind for word, kind in _KINDS if word in lower), "secret"))


def _plain(value: str) -> bool:
    """Too short, a template or a placeholder, a number, or an environment variable's
    name: not a secret value."""
    return (
        len(value) < MIN_LEN
        or _TEMPLATE.search(value) is not None
        or _NUMBER.fullmatch(value) is not None
        or _ENV_NAME.fullmatch(value) is not None
    )


def _code(m: re.Match[str], key: str, value: str) -> bool:
    """An unquoted value that is code (a call, an attribute, a variable), not a literal."""
    if m.string.startswith(("(", "["), m.end()) or _DOTTED.fullmatch(value):
        return True
    if not _WORD.fullmatch(value):
        return False  # digits or punctuation: a literal
    # Letters only: a variable name in code, unless the file is config: an env file or
    # compose file (an upper-case key), or YAML (key: value, the key unquoted).
    key_quoted = m.start() > 0 and m.string[m.start() - 1] in "\"'"
    if key.isupper():
        return False
    return not (m.group("sep") == ":" and not key_quoted and value.islower())


def _swap(m: re.Match[str], group: str, kind: str) -> str:
    whole, base = m.group(), m.start()
    return f"{whole[: m.start(group) - base]}{MARK}{kind}]{whole[m.end(group) - base :]}"


def _escaped(text: str, pos: int) -> bool:
    """Whether ``pos`` is right after an odd run of backslashes (inside a JSON escape)."""
    run = 0
    while pos - run > 0 and text[pos - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _path(value: str) -> bool:
    """An existing absolute path (GOOGLE_APPLICATION_CREDENTIALS=/home/me/key.json)."""
    try:
        return os.path.isabs(value) and os.path.exists(value)
    except (OSError, ValueError):
        return False
