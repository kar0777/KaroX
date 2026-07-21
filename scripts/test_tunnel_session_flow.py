"""Regression checks for per-session tunnel selection and Tailscale login UX."""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
ps = (root / "start.core.ps1").read_text(encoding="utf-8-sig")
sh = (root / "start.core.sh").read_text(encoding="utf-8")

# Windows: T opens the desktop client, captures a login URL, and opens it.
assert "function Start-TailscaleDesktopApp" in ps
assert "tailscale-ipn.exe" in ps
assert "https://login\\.tailscale\\.com/" in ps
assert "Start-Process $loginUrl" in ps
assert 'UI-Choice "T" (L "OPEN TAILSCALE"' in ps

# Every new session chooses its own provider instead of blindly using the global default.
assert "function Select-SessionTunnelProvider" in ps
assert "$tunnelProvider = Select-SessionTunnelProvider (Get-SelectedTunnelProvider)" in ps
assert "Get-RunningTailscaleSessions" in ps
assert "Use Cloudflare Tunnel for this additional session?" in ps

# POSIX launcher keeps parity and sends menu rendering to stderr so command
# substitution captures only the selected provider value.
assert "select_session_tunnel_provider()" in sh
assert "} >&2" in sh
assert 'tunnel_provider="$(select_session_tunnel_provider' in sh
assert "Use Cloudflare Tunnel for this additional session?" in sh
assert "open_url \"$login_url\"" in sh

print("KaroX tunnel/session regression checks passed")
