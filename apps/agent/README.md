# Windows local daemon

`company-agent` is the Windows device bridge for the target Codex architecture. It owns pairing, Credential Manager access, the validated WSS connection, task shadow workspaces, backups, audit forwarding, and the pinned Codex App Server lifecycle.

## Development

```powershell
cd apps/agent
$env:PATH = "C:\msys64\ucrt64\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
cargo test
cargo build --release
```

To smoke-test a company-approved real Codex artifact without making a model request, explicitly provide its staging path and run the ignored integration test:

```powershell
$env:COMPANY_AGENT_REAL_CODEX = "D:\staging\codex.exe"
cargo test real_pinned_codex_artifact_initializes_with_strict_config -- --ignored --nocapture
cargo test real_codex_completes_turn_against_local_responses_stub -- --ignored --nocapture
Remove-Item Env:COMPANY_AGENT_REAL_CODEX
```

The test computes the artifact hash, installs it into a temporary daemon-managed release directory using the four compile-time pin constants, starts App Server with strict managed configuration, completes `initialize`/`initialized`, shuts it down, and removes the temporary directory. It does not start a thread/turn, invoke the Responses endpoint, or print a token. An unset variable produces an explicit skip message; no user-specific path or hash is embedded in the repository.

The second ignored test uses a one-shot localhost Responses stub and temporary command-auth helper. A real pinned App Server fetches the authenticated fixed company model catalog, starts a thread/turn, consumes the documented text streaming event sequence, and must emit the fixed text plus `turn/completed` without approval or workspace changes. This proves local Codex/Responses protocol compatibility, not DeepSeek behavior or model quality.

That real test also verifies the minimum request-side `shell_command` function schema emitted by the pinned artifact without logging the request body or token. Its deterministic non-text phase drives one fixed temporary-workspace file write through the official Responses function-call stream, requires and explicitly accepts the real App Server approval, checks the matching tool-output continuation, syncs the shadow change through existing hash preconditions, then restores the pre-turn backup. It never calls an external model or approves network, hardware, or unrelated side effects. See [codex-tool-loop-contract-audit.md](../../docs/codex-tool-loop-contract-audit.md).

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
