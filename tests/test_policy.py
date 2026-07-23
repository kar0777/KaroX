from __future__ import annotations

import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied


class CapabilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = Origin(OriginKind.USER, "local-user")
        self.agent = Origin(OriginKind.NATIVE_AGENT, "agent")

    def test_profiles_grant_only_their_declared_capabilities(self) -> None:
        read_only = CapabilityPolicy(AccessProfile.READ_ONLY)
        self.assertTrue(read_only.decide(self.user, Capability.REPO_READ).allowed)
        self.assertTrue(read_only.decide(self.user, Capability.GIT_READ).allowed)
        self.assertFalse(read_only.decide(self.user, Capability.REPO_WRITE).allowed)

        elevated = CapabilityPolicy(AccessProfile.ELEVATED)
        self.assertTrue(elevated.decide(self.user, Capability.DESKTOP_INPUT).allowed)
        self.assertFalse(elevated.decide(self.user, Capability.GIT_PUSH).allowed)
        self.assertFalse(elevated.decide(self.user, Capability.PACKAGE_PUBLISH).allowed)

    def test_non_user_origins_are_deny_by_default_and_grants_are_profile_bounded(self) -> None:
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        self.assertFalse(policy.decide(self.agent, Capability.REPO_READ).allowed)
        policy.set_grants(
            self.agent, {Capability.REPO_READ, Capability.REPO_WRITE}
        )
        self.assertTrue(policy.decide(self.agent, Capability.REPO_READ).allowed)
        self.assertFalse(policy.decide(self.agent, Capability.REPO_WRITE).allowed)
        policy.set_denies(self.agent, {Capability.REPO_READ})
        self.assertFalse(policy.decide(self.agent, Capability.REPO_READ).allowed)

    def test_explicit_token_is_origin_bound_expiring_and_one_shot(self) -> None:
        policy = CapabilityPolicy(AccessProfile.ELEVATED)
        token = "approval-token-unguessable"
        other = Origin(OriginKind.USER, "other-user")
        with patch("karox.policy.time.time", return_value=100.0):
            policy.add_token(token, self.user, {Capability.GIT_PUSH}, ttl_seconds=10)
        with patch("karox.policy.time.time", return_value=101.0):
            self.assertFalse(
                policy.decide(other, Capability.GIT_PUSH, token).allowed
            )
            self.assertTrue(policy.require(self.user, Capability.GIT_PUSH, token).allowed)
            with self.assertRaises(PolicyDenied):
                policy.require(self.user, Capability.GIT_PUSH, token)

        expired = "expired-token-unguessable"
        with patch("karox.policy.time.time", return_value=200.0):
            policy.add_token(expired, self.user, {Capability.GIT_PUSH}, ttl_seconds=5)
        with patch("karox.policy.time.time", return_value=206.0):
            decision = policy.decide(self.user, Capability.GIT_PUSH, expired)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "approval token expired")
        self.assertNotIn(expired, policy.explicit_tokens)


if __name__ == "__main__":
    unittest.main()

