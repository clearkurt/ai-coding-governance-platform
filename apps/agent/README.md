# Windows local daemon

`company-agent` is the Windows device bridge for the target Codex architecture. It owns pairing, Credential Manager access, the validated WSS connection, task shadow workspaces, backups, audit forwarding, and the pinned Codex App Server lifecycle.

## Development

```powershell
cd apps/agent
$env:PATH = "C:\msys64\ucrt64\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
cargo test
cargo build --release
```

## Pairing

```powershell
company-agent.exe enroll --server ws://localhost:8000 --code <pairing-code>
company-agent.exe run
```

Use `--legacy` on `enroll` only when pairing against the legacy `/api/agent/ws` endpoint.

## Installing a managed Codex release

`configure-codex` no longer accepts an executable that will be launched in place, and SHA-256 is no longer optional. The operator must supply every field from the approved company release record:

```powershell
company-agent.exe configure-codex `
  --artifact D:\staging\codex.exe `
  --version 0.145.0-alpha.27 `
  --sha256 <64-hex-release-sha256> `
  --schema-version app-server-schema-1 `
  --model-catalog-version deepseek-v4-flash-1 `
  --config-template-version company-responses-1
```

The values above are the exact compatibility contract supported by this daemon build; arbitrary non-empty alternatives are rejected. The artifact path is installation input only. The daemon verifies its hash and exact `codex-cli <version>` output, atomically installs it below the daemon application-data directory, and stores only the release manifest in `agent.json`. Every startup reloads the installed manifest and rechecks the managed artifact hash and version before spawning App Server. It never searches `PATH`, launches the staging path, or accepts an omitted hash.

Existing development configurations using `--executable` must be reinstalled with the command above. `company-agent use-legacy` remains available and can clear even an old Codex configuration shape.

See [runtime-pinning-security.md](../../docs/runtime-pinning-security.md) and [shadow-workspace-security.md](../../docs/shadow-workspace-security.md) for security boundaries.
