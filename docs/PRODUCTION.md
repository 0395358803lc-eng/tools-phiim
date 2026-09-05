# Production Operations — TH Media

## Release gates

A production release must pass `scripts/release.ps1`. The release chain is:

1. Dependency integrity and vendored Flow CLI SHA-256 verification.
2. Ruff and JavaScript syntax checks.
3. Full pytest suite with branch coverage >= 70%.
4. Headless Chromium browser E2E for the offline workflow.
5. Bandit static security scan.
6. `pip-audit` vulnerability scan and CycloneDX SBOM generation.
7. Third-party license report generation.
8. Locked PyInstaller build.
9. Executable smoke test against `/api/health`.
10. Authenticode signing of the executable.
11. Inno Setup installer build.
12. Authenticode signing of the installer.

The release script intentionally refuses to continue without `FLOW_STUDIO_CERT_THUMBPRINT`.

## Data and backups

Each workspace stores project JSON in `projects/` and automatic bounded backups in `backups/<project-id>/`.
Project JSON includes `schema_version`. Older payloads are migrated through `migrations.py`; payloads newer than the running application are rejected rather than silently downgraded.

Backups are created before replacing an existing project, rate-limited during high-frequency progress updates, retained to a bounded history, and created immediately before deletion or restore. Backup metadata and restore operations are exposed through the local project API.

## Logs

Desktop production sessions write rotating UTF-8 logs under the Windows user data directory:

`%LOCALAPPDATA%\TH Media\logs\studio.log`

Unhandled desktop/thread/API exceptions and background analysis/render/final-video failures are logged. Logs rotate at 5 MB with five retained files. Credentials and session tokens must never be written to logs.

## Credentials and local API

xKiro API credentials and the Google Flow CLI session remain protected by Windows DPAPI. Flow CLI runtime cookies must never be committed or packaged as shared credentials. Desktop sessions generate a random per-process session token. Mutating API requests (`POST`, `PUT`, `PATCH`, `DELETE`) require that token in `X-Flow-Studio-Session`. The token is passed to the frontend only through the initial URL fragment and removed from browser history immediately after startup.

The API continues to bind only to `127.0.0.1` on a random port.

## Workspace concurrency

A workspace is protected by `.flow-story-studio.lock`. A second TH Media process cannot open the same workspace for mutation until the first session releases the lock.

## Dependency reproducibility

`requirements.lock.txt` contains the validated Windows/Python 3.12 constraint set. The vendored `flow_cli-0.6.0-py3-none-any.whl` is additionally pinned by SHA-256 in `vendor/SHA256SUMS.txt` because it is not available for PyPI vulnerability lookup. The Google Flow transport is the vendored Python Flow CLI wheel; `setup.ps1` installs that wheel and Playwright Chromium without a Node/npm transport.

## External provider risk

Google Flow remains an external browser/UI dependency outside this application's control. Production support should treat Flow UI changes as a provider outage risk. The only transport is the integrated Python Flow CLI with its encrypted authenticated session. Recovery is fail-closed: an existing upstream job identity must be recovered or explicitly force-rerendered, never silently resubmitted. No local release can guarantee an upstream Google interface remains stable.


## Release signing

Never ship the production installer unsigned. Configure a real Windows code-signing certificate with a private key in the current user's certificate store and set:

`FLOW_STUDIO_CERT_THUMBPRINT=<certificate thumbprint>`

`sign-artifact.ps1` uses the Windows SDK `signtool.exe`, SHA-256 file digest, and RFC3161 timestamping, then verifies the resulting Authenticode status.

Do not substitute a self-signed certificate for a public production release.

## WebView2 prerequisite bootstrap

`THMedia-Setup-<version>.exe` embeds the official Microsoft Edge WebView2 Evergreen Bootstrapper. During setup, the installer checks the documented WebView2 Runtime registry registration (`pv` under EdgeUpdate client `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` in HKLM/HKCU). If a valid Runtime version is already present, setup skips installation. If it is missing, setup extracts the Microsoft bootstrapper and runs it silently with `/silent /install`, waits for completion, and verifies that the Runtime registration is now present before installing TH Media.

`scripts/build-installer.ps1` downloads the bootstrapper from Microsoft's official fwlink each time an installer is built, verifies its Authenticode signature is valid and signed by Microsoft, and only then invokes Inno Setup. The bootstrapper itself is ignored by Git because it is a third-party binary that is refreshed at build time.
