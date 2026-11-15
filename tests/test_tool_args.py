import json

import pytest

from karox6.tools.base import ToolArgsError, parse_arguments

SCHEMA = {
    "properties": {
        "n": {"type": "integer"},
        "flag": {"type": "boolean"},
        "items": {"type": "array"},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["n"],
}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{", "valid JSON"),
        ("[]", "JSON object"),
        ("{}", "missing required"),
        ('{"n": null}', "must not be null"),
        ('{"n": 1, "mode": "c"}', "one of"),
        ('{"n": true}', "integer"),
    ],
)
def test_invalid(raw, message):
    with pytest.raises(ToolArgsError, match=message):
        parse_arguments(raw, SCHEMA)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"n": "120"}, {"n": 120}),
        ({"n": 1, "flag": "true"}, {"n": 1, "flag": True}),
        ({"n": 1, "items": "[1, 2]"}, {"n": 1, "items": [1, 2]}),
        ({"n": 1, "flag": None}, {"n": 1}),
        ({"n": 1, "extra": "x"}, {"n": 1, "extra": "x"}),
    ],
)
def test_coercion(value, expected):
    assert parse_arguments(json.dumps(value), SCHEMA) == expected


def test_multiple_errors():
    with pytest.raises(ToolArgsError) as exc:
        parse_arguments('{"n": "bad", "flag": "bad", "mode": "bad"}', SCHEMA)
    assert all(x in str(exc.value) for x in ("'n'", "'flag'", "'mode'"))
