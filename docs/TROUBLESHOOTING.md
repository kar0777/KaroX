# Secure credentials on headless Linux

KaroX never falls back to a plaintext keyring or writes passwords/API tokens to
its configuration. Provider environment references remain an explicit, less
protected provider-only option; they do **not** repair bridge credential storage.
`keyrings.alt`, encrypted-file plugins, a D-Bus socket alone, and a successful
keyring import are not evidence that durable OS storage is usable.

## Diagnose without changing the host

```sh
karox credential doctor --json
karox credential setup --json
```

`setup` without an action is a read-only recovery plan. `doctor` checks that a
Secret Service is **already running** and its default durable collection exists
and is unlocked. Neither command installs software, starts D-Bus/a service,
creates a collection, unlocks it, or reads a credential. Missing/locked storage
is unavailable, not green. The CLI exits nonzero in these cases. A successful
ordinary doctor is only an unlocked-collection metadata check, not a write test,
a check that every saved reference exists, or an encryption-at-rest audit.

KaroX accepts an inherited `DBUS_SESSION_BUS_ADDRESS` pointing to a local Unix
socket owned by the current user in a private directory. Without that variable,
it can discover the existing `/run/user/<uid>/bus`. It does not autolaunch a bus,
read another process's environment, save addresses in config, or silently switch
away from a stale explicit address. TCP, abstract sockets, symlinked/private-dir
violations and multi-address buses are deliberately unsupported. For stale
shell environments, review the address locally; unset it **only if you intend
to use your existing canonical OS user bus**, then rerun doctor. Do not copy bus
addresses from another user or run `sudo karox`.

## Debian/Ubuntu: user-approved OS setup

A minimal server/container often has neither a user session bus nor Secret
Service. A Python `keyring` install alone cannot supply them.

1. **Administrator decision:** review installing the distribution's
   `dbus-user-session` and `gnome-keyring` packages. A conventional administrator
   command is `sudo apt install dbus-user-session gnome-keyring`, but KaroX does
   not run it. PAM integration (`libpam-gnome-keyring`), login policies, systemd
   user services and lingering are separate security decisions for the host
   administrator. No KaroX helper alters PAM, enables lingering, or changes an
   OS/login password. Do not disable keyring encryption to avoid a prompt.
2. Log in as the same OS user that will own the bridge, with a persistent OS
   user session bus. Log out/back in if required by the approved OS setup.
3. **Explicit activation consent:** on that already-existing user bus, run:

   ```sh
   karox credential setup --activate --consent
   ```

   This sends D-Bus `StartServiceByName` for its registered Secret Service only.
   It does not start a missing session bus. A locked/missing collection still
   reports unavailable after activation.
4. **Local user takeover:** with GNOME Keyring installed, run:

   ```sh
   karox credential setup --unlock --consent
   ```

   This asks twice, without echo, for the **login keyring password**, not an API
   key. It invokes the system `/usr/bin/gnome-keyring-daemon --unlock
   --components=secrets`, supplying the password solely through a private stdin
   pipe. This may start GNOME Keyring and create its encrypted login keyring if
   absent. Review that side effect before consenting. Blank passwords, a
   noninteractive terminal, and getpass echo fallback are refused. No password
   is put in shell history, argv, environment, configuration, logs, or a file.
   The daemon receives only a small OS-session environment allowlist, not
   inherited API tokens or dynamic-loader overrides.
   Python cannot guarantee memory zeroization; this is not such a guarantee.
   The helper suppresses daemon output and checks collection availability again;
   a zero daemon exit code is **not** reported as proof of success.

   If another Secret Service owns this bus (for example KeePassXC), use its own
   secure unlock UI instead of the GNOME helper. If there is no default
   collection/alias after GNOME unlock, select a persistent default collection
   using the OS keyring manager (e.g. Seahorse); KaroX will not guess or change
   aliases. Forgotten passwords require OS-supported recovery; KaroX does not
   reset/delete a keyring or promise to recover encrypted secrets.
5. **Explicit disposable-test consent:** verify writing, reading in an entirely
   new interpreter, and deleting a synthetic random credential:

   ```sh
   karox credential setup --verify --consent --json
   karox credential doctor --json
   ```

   The test uses only `KaroX/credential-check` with a random `probe-*` account.
   It never reads/rotates an existing bridge/provider credential or prints a
   secret. Success requires cleanup too. On cleanup failure, the helper reports
   the disposable item identifier for manual removal in the OS keyring UI. An
   abruptly killed test can leave a harmless random probe item; remove it there.
6. Restart previously failed bridge processes under that same OS user/session,
   then rerun the normal bridge diagnostics. Setup does not silently rewrite
   profiles or rotate missing/revoked keys. Repair such references through the
   existing guarded credential/profile flows.

## Background bridges and reboot/logout

The unlock belongs to the **OS Secret Service daemon**, not the KaroX Python
process. Children inherit the bus address; independently launched processes can
rediscover the canonical same-user bus. A password entered into one Python
process and used only by an in-memory encrypted-file plugin would not solve this
problem; no such backend is added here.

The OS daemon and its bus must outlive **all** bridge supervisors, MCP children,
and reconnects. A systemd user session/approved service lifecycle is preferable.
A manually reviewed `dbus-run-session -- bash` can provide a temporary local
session on a host without a user bus, followed by the setup commands inside that
shell **only if its bus uses a supported private `unix:path` socket** (many
distributions instead use unsupported abstract sockets). **Keep the shell open
until all bridges are stopped**. Exiting it
kills the bus even if a bridge has detached. Do not run a one-shot
`dbus-run-session -- karox ...` and assume a detached bridge remains unlocked.
KaroX does not create unmanaged private D-Bus sessions or keep unlock passwords.

After reboot, logout, lock, or daemon restart, storage may need local unlock
again. No unattended reboot unlock is promised; use an administrator-approved
OS login/service design if that is required. Arbitrary same-user processes may
access an unlocked user keyring: it is not isolation against compromised code
running as the same user. Bridge service users, containers and SSH sessions
must share the intended user bus or deliberately use their own secure service.

## Scope and evidence

Linux uses only native Secret Service, never a chaining/plugin fallback. Windows
Credential Manager and macOS Keychain remain supported native backends. Linux
backend overrides other than `keyring.backends.SecretService.Keyring` are
refused. `KEYRING_PROPERTY_*` overrides (including scheme and preferred collection) are not
used: diagnostics and operations deliberately use the same default collection.
No provider environment variable is automatically created or selected.

`SecretStorage` and `jeepney` are declared explicitly as Linux-only dependencies
in both manifests because the integration now calls their APIs directly:
non-activating bounded D-Bus metadata probes and noninteractive existing-
collection access. They were already transitive dependencies of `keyring` on
Linux; no new OS package or encrypted backend is bundled.

The dedicated test suite emulates a Secret Service over a private Unix socket
and exercises the actual Python keyring stack and a fresh background interpreter
(including bridge credential reads). It never launches a real OS user daemon,
installs an OS package, or changes host authentication. This proves protocol
integration and process propagation, **not** live Debian/GNOME/PAM conformance,
reboot recovery, or encryption-at-rest behavior of a configured OS service.
Those checks require a consenting user on the target OS. Do not report them
passed based on emulation.
