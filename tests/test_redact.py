"""Secret masking of MCP replies (karox6.mcp.redact). Every value here is fake."""

import base64
import json
import time
from pathlib import Path

import pytest

from karox6.mcp.redact import MARK, Redactor, redact, redact_json, secret_name

FAKE = {
    "openai": "sk-proj-" + "FAKE0fake" * 5,
    "anthropic": "sk-ant-api03-" + "FAKE0fake" * 6,
    "openrouter": "sk-or-v1-" + "0f" * 32,
    "deepseek": "sk-" + "0f" * 16,
    "github": "ghp_" + "FAKE0fake" * 4,
    "github_oauth": "gho_" + "FAKE0fake" * 4,
    "github_pat": "github_pat_" + "FAKE0fake_" * 8,
    "slack": "xoxb-" + "0000000000-FAKEfakeFAKE",
    "aws": "AKIA" + "FAKE" * 4,
    "aws_session": "ASIA" + "FAKE" * 4,
    "google": "AIza" + "FAKE0fake_" * 3 + "FAKE0",
    "jwt": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJmYWtlIn0.FAKEsignature0",
    "stripe": "sk_live_" + "FAKE0fake" * 3,
    "stripe_restricted": "rk_live_" + "FAKE0fake" * 3,
    "huggingface": "hf_" + "FAKEfake" * 5,
    "gitlab": "glpat-" + "FAKE0fake" * 3,
    "npm": "npm_" + "FAKE0fake" * 4,
    "pypi": "pypi-AgEIcHlwaS5vcmc" + "FAKE0fake" * 6,
}
KINDS = {
    "openai": "API key",
    "anthropic": "Anthropic API key",
    "openrouter": "OpenRouter API key",
    "deepseek": "API key",
    "github": "GitHub token",
    "github_oauth": "GitHub token",
    "github_pat": "GitHub token",
    "slack": "Slack token",
    "aws": "AWS access key id",
    "aws_session": "AWS access key id",
    "google": "Google API key",
    "jwt": "JWT",
    "stripe": "Stripe key",
    "stripe_restricted": "Stripe key",
    "huggingface": "Hugging Face token",
    "gitlab": "GitLab token",
    "npm": "npm token",
    "pypi": "PyPI token",
}
KEY_BODY = "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n" * 3 + "Fake0Q==\n"
PEM = "-----BEGIN RSA PRIVATE KEY-----\n" + KEY_BODY + "-----END RSA PRIVATE KEY-----"
PRIVATE_KEY = MARK + "private key]"


@pytest.mark.parametrize("name", sorted(FAKE))
def test_token_formats(name):
    token, kind = FAKE[name], KINDS[name]
    assert redact(f"value: {token} (end)") == f"value: {MARK}{kind}] (end)"
    assert redact(f"[{token}]") == f"[{MARK}{kind}]]"
    # In JSON text the token can start right after a \n escape.
    assert json.loads(redact(json.dumps(f"first line\n{token}"))) == f"first line\n{MARK}{kind}]"


@pytest.mark.parametrize(
    "text",
    [
        "commit 3f786850e387550fdab836ed7e6dc881de23001b (HEAD -> main)",
        "id 123e4567-e89b-12d3-a456-426614174000 created",
        '"integrity": "sha512-' + "Zm9vYmFy" * 11 + '=="',  # package-lock.json
        'hash = "sha256:' + "0123456789abcdef" * 4 + '"',  # poetry.lock / uv.lock
        "![logo](data:image/png;base64," + base64.b64encode(bytes(range(256)) * 4).decode() + ")",
        "class sk-spinner-circle-bounce-delay-outer",  # kebab case, no digit
        "self.hf_download_model_from_the_hub_with_retries()",  # snake case after hf_
        "the task-0123456789abcdefghijklmnop runs",  # sk- inside a word
        "Bearer authentication and Basic authorization schemes",
        "AKIAFAKEFAKEFAKEFAKEMORE is too long for an access key id",
        "sha256 " + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ],
)
def test_random_looking_text_stays(text):
    assert redact(text) == text


def test_private_key_blocks():
    assert redact(f"key:\n{PEM}\nafter") == f"key:\n{PRIVATE_KEY}\nafter"
    for kind in ("OPENSSH PRIVATE KEY", "EC PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "PRIVATE KEY"):
        block = f"-----BEGIN {kind}-----\n{KEY_BODY}-----END {kind}-----"
        assert redact(block) == PRIVATE_KEY, kind
    pgp = "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nFAKE0lQ\n-----END PGP PRIVATE KEY BLOCK-----"
    assert redact(pgp) == PRIVATE_KEY
    encrypted = PEM.replace("KEY-----\n", "KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-1\n", 1)
    assert redact(encrypted) == PRIVATE_KEY
    # The read tool numbers lines; a service-account JSON holds the key with \n escapes.
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(PEM.splitlines(), 1))
    assert redact(numbered) == f"1: {PRIVATE_KEY}"
    escaped = json.dumps({"private_key": PEM, "client_email": "fake@example.iam"})
    assert json.loads(redact(escaped)) == {
        "private_key": PRIVATE_KEY,
        "client_email": "fake@example.iam",
    }
    # Certificates and public keys are not secret.
    cert = "-----BEGIN CERTIFICATE-----\nMIIBFAKE\n-----END CERTIFICATE-----"
    public = "-----BEGIN PUBLIC KEY-----\nMIIBFAKE\n-----END PUBLIC KEY-----"
    assert redact(cert) == cert
    assert redact(public) == public


def test_partial_private_keys():
    lines = PEM.splitlines()
    head = "\n".join(lines[:3]) + "\n... [1200 chars cut]"
    assert redact(head) == f"{PRIVATE_KEY}\n... [1200 chars cut]"
    tail = "==> id_rsa <==\n" + "\n".join(lines[2:])
    assert redact(tail) == f"==> id_rsa <==\n{PRIVATE_KEY}\n{lines[-1]}"
    numbered_tail = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[2:], 3))
    assert "MIIBOg" not in redact(numbered_tail)
    assert lines[-1] in redact(numbered_tail)
    cut_json = json.dumps({"text": "\n".join(lines[:3])})
    assert json.loads(redact(cut_json)) == {"text": PRIVATE_KEY}


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Authorization: Bearer abc.DEF-123_xyz", "Authorization: Bearer {}"),
        (
            "curl -H 'Authorization: Basic dXNlcjpmYWtlcGFzcw=='",
            "curl -H 'Authorization: Basic {}'",
        ),
        ('{"authorization": "token 0123fakefake"}', '{{"authorization": "token {}"}}'),
        ("proxy-authorization: FAKEfake0123", "proxy-authorization: {}"),
        ("sent Bearer FAKEtoken0123456789 to the API", "sent Bearer {} to the API"),
    ],
)
def test_authorization_headers(text, masked):
    assert redact(text) == masked.format(MARK + "credentials]")


@pytest.mark.parametrize(
    "text",
    [
        'headers = {"Authorization": f"Bearer {token}"}',
        'curl -H "Authorization: Bearer $GITHUB_TOKEN"',
        "Authorization: Bearer <token>",
        "Authorization: Bearer ${API_TOKEN}",
        "authorization: str",
    ],
)
def test_authorization_templates_stay(text):
    assert redact(text) == text


def test_url_credentials():
    pw = MARK + "password]"
    assert redact("postgres://admin:fakepass0@db:5432/app") == f"postgres://admin:{pw}@db:5432/app"
    assert redact("redis://:fake0pass@cache:6379") == f"redis://:{pw}@cache:6379"
    remote = "https://x-access-token:FAKE0fake0FAKE@github.com/o/r.git"
    assert redact(remote) == f"https://x-access-token:{pw}@github.com/o/r.git"
    for kept in (
        "postgres://user:${DB_PASSWORD}@db/app",
        "postgres://user:<password>@db/app",
        "https://example.com:8443/path?q=a@b",
        "ssh://git@github.com:22/o/r.git",
        'f"postgresql://{user}:{password}@{host}"',
    ):
        assert redact(kept) == kept


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("DB_PASSWORD=fake-pass-0\nDEBUG=1", "DB_PASSWORD={password}\nDEBUG=1"),
        ("DB_PASSWORD=supersecret", "DB_PASSWORD={password}"),  # an env file: letters only
        ("export API_TOKEN='Fake 0 token'", "export API_TOKEN='{token}'"),
        ("password: supersecretpw", "password: {password}"),  # YAML
        ("  POSTGRES_PASSWORD: example0pw", "  POSTGRES_PASSWORD: {password}"),
        ('{"client_secret": "FAKEfake0123"}', '{{"client_secret": "{secret}"}}'),
        ("SECRET_KEY = 'django-insecure-fake0'", "SECRET_KEY = '{secret}'"),
        ("GET /cb?access_token=FAKE0fake0&state=1", "GET /cb?access_token={token}&state=1"),
        ("'db_password' => 'fake0pass0'", "'db_password' => '{password}'"),
        ("karox --password=fake0pass0", "karox --password={password}"),
        ("apiKey: FAKE0fake0", "apiKey: {api}"),
        ("//registry.npmjs.org/:_authToken=FAKE0fake0", "//registry.npmjs.org/:_authToken={token}"),
        ("+AWS_SECRET_ACCESS_KEY=FAKE0fake0/FAKE", "+AWS_SECRET_ACCESS_KEY={secret}"),  # a diff
        ("-SMTP_PASS=fake0pass0", "-SMTP_PASS={password}"),
        ("x-api-key: FAKE0fake0", "x-api-key: {api}"),
    ],
)
def test_assignments(text, masked):
    kinds = {k: f"{MARK}{v}]" for k, v in (("password", "password"), ("secret", "secret"))}
    kinds.update(token=MARK + "token]", api=MARK + "API key]")
    assert redact(text) == masked.format(**kinds)


@pytest.mark.parametrize(
    "text",
    [
        "PASSWORD=\nDB_HOST=localhost",  # empty value: the next line is not its value
        "PASSWORD=''",
        "use_password: true",
        "secret: null",
        "API_KEY=${OPENAI_API_KEY}",
        "API_KEY=$OPENAI_API_KEY",
        "set API_KEY=%OPENAI_API_KEY%",
        "token: <your token>",
        "password: xxxxxxxxxx",
        "password = '********'",
        "API_KEY=your-api-key-here",
        "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
        'api_key = os.environ["OPENAI_API_KEY"]',
        "SECRET_KEY = settings.SECRET_KEY",
        "token = get_access_token()",
        "client = Client(password=user_password, token=token)",
        "password: SecretStr",
        "access_token: Optional[str] = None",
        "{password: password}",
        '{"token": access_token_value}',
        '"max_tokens": 12345678, "input_tokens": 98765432',
        "tokenizer: gpt2-tokenizer-v1234",
        "token_url = https://example.com/oauth/token",
        "secret_name: my-secret-0",
        "password_file: /run/secrets/db-password",
        'api_key_env = "OPENROUTER_API_KEY"',
        "PWD=/home/user/project0",
        "if token == expected_token_0:",
        "password reset link sent",
    ],
)
def test_non_secret_assignments_stay(text):
    assert redact(text) == text


def test_secret_name():
    for name in ("DB_PASSWORD", "apiKey", "client-secret", "GITHUB_TOKEN", "x-api-key", "pass"):
        assert secret_name(name), name
    for name in ("max_tokens", "tokenizer", "token_url", "secretName", "PWD", "bypass", "HOME"):
        assert not secret_name(name), name


def test_environment_values():
    env = {
        "MY_SERVICE_TOKEN": "Fake0value0for0tests",
        "LLM_KEY": "anotherFake0value",
        "HOME": "Fake0value0not0secret",  # not a secret-looking name
        "SHORT_SECRET": "abc",  # too short to mask safely
        "SPACED_PASSWORD": "two words",
        "TOKENIZERS_PARALLELISM": "false",
    }
    redactor = Redactor.from_env(env)
    text = "a Fake0value0for0tests b anotherFake0value c Fake0value0not0secret d abc e two words"
    assert redactor(text) == (
        f"a {MARK}$MY_SERVICE_TOKEN] b {MARK}$LLM_KEY] c Fake0value0not0secret d abc e two words"
    )
    redactor.add("Fake0server0token0", "KaroX access token")
    assert redactor("url /mcp/Fake0server0token0") == f"url /mcp/{MARK}KaroX access token]"
    # Patterns apply as well, and a value right after a backslash belongs to an escape.
    assert redactor(FAKE["github"]) == MARK + "GitHub token]"
    tricky = Redactor()
    tricky.add("nFake0value0", "x")
    assert tricky(json.dumps("\nFake0value0")) == json.dumps("\nFake0value0")
    assert tricky(json.dumps("nFake0value0")) == json.dumps(MARK + "x]")


def test_existing_paths_in_secret_variables_are_not_masked(tmp_path):
    path = str(tmp_path / "credentials.json")
    (tmp_path / "credentials.json").write_text("{}")
    redactor = Redactor.from_env({"GOOGLE_APPLICATION_CREDENTIALS": path})
    assert redactor(f"using {path}") == f"using {path}"


def test_json_stays_valid():
    data = {
        "log": [f"cat .env\nOPENAI_API_KEY={FAKE['openai']}\nDB_PASSWORD=fake0pass0", PEM],
        "events": [{"arguments": json.dumps({"password": "fake0pass0", "q": 'say "hi"\\'})}],
        "nested": {"token": "FAKE0fake0", "url": "https://u:fake0pw@host/x", "n": 12345678},
        "final_text": f"tab\t{FAKE['github']}\r\nAuthorization: Bearer {FAKE['jwt']}",
    }
    for text in (json.dumps(data), json.dumps(data, indent=1, ensure_ascii=False)):
        masked = redact(text)
        json.loads(masked)
        for value in (FAKE["openai"], "fake0pass0", "FAKE0fake0", "fake0pw", "MIIBOg"):
            assert value not in masked
    assert redact_json(data, redact)["nested"] == {
        "token": MARK + "token]",
        "url": f"https://u:{MARK}password]@host/x",
        "n": 12345678,
    }


def test_redact_json_falls_back_to_each_string():
    # Masked as one text, the "value" after TOKEN= would swallow JSON syntax up to the
    # next quote; the reply is then masked string by string instead.
    calls = []

    def mask(text):
        calls.append(text)
        return redact(text)

    data = ["export TOKEN=", 12345678, 87654321, True, "x", "PASSWORD=fake0pass0"]
    assert redact_json(data, mask) == [*data[:5], "PASSWORD=" + MARK + "password]"]
    assert len(calls) > 1


def test_speed():
    code = Path(__file__).read_text(encoding="utf-8")
    texts = {
        "mixed": ((code + PEM + json.dumps(FAKE)) * 20)[:100_000],
        "base64": base64.b64encode(bytes(range(256)) * 293).decode()[:100_000],
        "json": json.dumps({"log": [f"{i}: token=FAKE{i}fake KEY=v" for i in range(4000)]}),
        "headers": "-----BEGIN PRIVATE KEY-----\n" * 3500,
        "assignments": "password=" * 11_000,
        "words": "token key secret password " * 4000,
    }
    redactor = Redactor.from_env({"MY_TOKEN": "Fake0value0for0tests"})
    for name, text in texts.items():
        started = time.perf_counter()
        redactor(text[:100_000])
        assert time.perf_counter() - started < 0.25, name
