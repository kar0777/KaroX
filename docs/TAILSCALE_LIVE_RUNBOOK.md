# Tailscale Funnel live verification runbook

This runbook is the only place where a real tailnet result may be promoted from
`pending` to `passed`. Unit tests and mocked CLI output prove the local KaroX
contract; they do not prove that a particular account, node, policy, hostname,
or Tailscale CLI version can publish a Funnel.

## Safety rules

- Use a test tailnet or a node whose existing Serve/Funnel configuration is
  understood.
- Do not run `tailscale serve reset` or `tailscale funnel reset` as part of the
  KaroX test.
- KaroX must refuse to start when it cannot prove that no existing route will be
  replaced.
- Keep the KaroX listener on `127.0.0.1`; do not bind it to a LAN interface.
- Do not paste the OAuth approval password, access token, refresh token, bridge
  credential, or repository payload into this record.
- Do not mark ChatGPT or Claude conformance passed from this runbook alone.

## Preconditions

1. Install a supported Tailscale CLI and daemon.
2. Log in and confirm that the node has a MagicDNS hostname ending in `.ts.net`.
3. Ask the tailnet administrator to permit Funnel for this node when policy
   requires it.
4. Ensure no unrelated Serve/Funnel route is active on the node.
5. Use a disposable repository with no secrets for the first public run.

## Local doctor

Run:

```bash
karox bridge doctor --json
```

Record, without secrets:

- Tailscale `code` and `backend_state`;
- resolved executable path;
- stable `public_url`;
- `route_ownership.known` and `route_ownership.active`;
- KaroX saved-profile registry status.

Expected ready state:

- `code` is `ready`;
- `public_url` is an HTTPS `.ts.net` origin;
- ownership is known;
- no existing route is active.

Expected classified failures must be checked separately where practical:

- `not_installed`;
- `login_required`;
- `machine_approval_required`;
- `hostname_unavailable`;
- `funnel_policy_denied`;
- `route_in_use`;
- `https_port_in_use` or `https_unavailable`.

## Create a disposable saved profile

Use an empty test repository and a high local port that is currently free:

```bash
karox bridge saved create tailscale-live --target-profile chatgpt-web --repository . --tool karox.repo.read_file --tool karox.git.status --tunnel tailscale --deadline-preset standard --language en --port 9876
```

Validate the effective policy before publication:

```bash
karox bridge saved validate tailscale-live --json
karox bridge connect --saved tailscale-live --diagnostics-only
```

The diagnostics must show:

- only the selected repository-scoped tools;
- `karox.checks.run` disabled with an explicit reason when no verification
  allowlist was configured;
- requested and effective deadlines equal;
- `tunnel` equal to `tailscale`;
- `url_stability` equal to `stable_device_hostname`;
- no credential material.

## Start and verify the Funnel

Run:

```bash
karox bridge connect --saved tailscale-live
```

Verify all of the following while the process remains open:

1. KaroX prints the same stable HTTPS origin reported by Tailscale status.
2. The local bridge listens only on `127.0.0.1:9876`.
3. The public MCP URL reaches KaroX through Funnel.
4. OAuth discovery and authorization complete without weakening origin,
   redirect, PKCE, resource, or session binding.
5. The authenticated MCP client discovers `karox.bridge.diagnostics`.
6. Calling diagnostics returns the selected tools, disabled-tool reasons,
   verification allowlist, effective deadline, tunnel type, URL stability, and
   session lifetime without secrets.
7. A read-only repository call succeeds.
8. An unselected or mutating tool is unavailable or denied.

## Ownership and cleanup proof

Before stopping KaroX, capture the current route status. Then terminate the
managed bridge normally with Ctrl+C.

Pass criteria:

- the KaroX foreground Funnel child exits;
- the KaroX bridge child exits;
- the session credential is revoked according to the managed-launcher contract;
- the KaroX route disappears;
- no global reset command is invoked;
- any pre-existing unrelated Tailscale configuration remains unchanged;
- restarting with the same saved profile reuses the same `.ts.net` hostname when
  the tailnet itself keeps that hostname stable.

Also test fail-closed ownership once on a disposable node: create an unrelated
Serve/Funnel route, run the saved KaroX profile, and confirm KaroX reports
`route_in_use` without modifying that route.

## Evidence record

Record:

- UTC date and KaroX commit SHA;
- operating system and Tailscale CLI version;
- redacted doctor output;
- redacted bridge diagnostics;
- public hostname only, not credentials;
- start/stop observations;
- whether an unrelated route survived the refusal test;
- exact failures and exit codes;
- whether ChatGPT or Claude was tested separately.

A live result is `passed` only when every applicable pass criterion above was
observed on a real account. Otherwise record `failed` or `blocked` with the exact
classification and leave product conformance pending.

## Cleanup

Delete only the disposable KaroX profile:

```bash
karox bridge saved delete tailscale-live --json
```

Do not delete or reset unrelated Tailscale routes as part of cleanup.
