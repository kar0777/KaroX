from karox.core import InvalidCommand, VerificationCommandNotApproved
from karox.proxy_server import BRIDGE_ERROR_MESSAGES, bridge_error_code, bridge_error_result


def test_verification_allowlist_rejection_has_actionable_safe_error_code() -> None:
    exc = VerificationCommandNotApproved(
        "check command is not in the user-approved verification set; private-detail"
    )
    code = bridge_error_code(exc)
    assert code == "verification_not_approved"

    result = bridge_error_result(code)
    assert result.isError is True
    assert result.structuredContent["error_code"] == code
    assert result.structuredContent["error"] == BRIDGE_ERROR_MESSAGES[code]
    assert "karox.command" in result.structuredContent["error"]
    assert "private-detail" not in result.structuredContent["error"]


def test_other_invalid_commands_keep_generic_invalid_request_code() -> None:
    assert bridge_error_code(InvalidCommand("other invalid request")) == "invalid_request"
