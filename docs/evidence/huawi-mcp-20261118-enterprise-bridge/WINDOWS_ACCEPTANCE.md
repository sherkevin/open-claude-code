# Windows acceptance status and runbook

## Current status

Windows remote testing is intentionally paused by the user as of 2026-11-18.
No Windows Server 2019 machine was logged in to, modified or used for this
evidence pass. No DPAPI, ACL, service, reboot, upgrade or uninstall result is
claimed as a real-machine result.

The repository does contain the Windows implementation and automated coverage:

- machine-scoped DPAPI for `identity.json`, `device_key.json`, `secrets.json`
  and `auth_profiles.json`;
- strict data-directory ACL setup with hard failure when `icacls` or ACL
  verification fails;
- upgrade backup into the protected data directory;
- atomic migration of legacy plaintext JSON on first protected read;
- Windows service restart and frpc supervision;
- Windows `frpc.toml` that uses official `.Envs` placeholders instead of
  persisted relay/STCP secrets;
- `connector/scripts/windows-smoke.ps1` and the installer script in the ZIP.

The Go module’s Windows-specific sources compile in the normal build, but
cross-compilation is not a substitute for running on Windows Server 2019.

## Required target

- Windows Server 2019 Datacenter 64-bit with a graphical desktop, as confirmed
  for the customer environment.
- Administrator access for installation.
- A synthetic or authorized local MCP endpoint; do not use customer secrets in
  development evidence.
- The fixed 1.3.0 package and its `SHA256SUMS` from `RELEASE_REPORT.md`.

When remote testing resumes, the first device actions must be read-only: verify
the host name, operating system and user, and record the device ID, process IDs
and session IDs before changing anything. Do not sign in repeatedly or reuse a
finished authorization process.

## Acceptance sequence

1. Verify the ZIP hash against `RELEASE_REPORT.md` and run `install.ps1` as
   Administrator.
2. Confirm the service is `Running` and `http://127.0.0.1:17872/` is reachable.
3. Inspect the four sensitive files: they start with `KAIWU-DPAPI-V1` and do not
   contain the synthetic bearer in plaintext.
4. Inspect the data-directory ACL: only `S-1-5-18` (SYSTEM) and
   `S-1-5-32-544` (Administrators) may have access; inherited rules must be
   disabled.
5. Import the synthetic MCP configuration, approve the intended tool, and run
   initialize, list and call through the local proxy.
6. Start the connector, verify tunnel and Edge status, then stop the relay and
   confirm a bounded 503/failure rather than a silent success.
7. Restart the Windows service and confirm the persisted configuration is
   loaded without exposing a plaintext secret.
8. Restart the whole machine and repeat the service/data-plane checks.
9. Reinstall the package over the existing data directory and verify the
   pre-upgrade backup and retained configuration.
10. Uninstall or revoke the test device and verify local access is denied and
    the cloud session cannot continue.

## Expected evidence to capture

- operating-system name/version, host name and time zone;
- package version and SHA-256;
- service state before/after restart and reboot;
- sanitized ACL output;
- DPAPI prefix and plaintext-secret scan result (never paste real secrets);
- local MCP lifecycle output with fixed synthetic data;
- relay-stop and recovery timings;
- upgrade backup path and retained configuration result;
- uninstall/revocation result.

Do not record a pass for this document until the steps were executed on the
target Windows host.
