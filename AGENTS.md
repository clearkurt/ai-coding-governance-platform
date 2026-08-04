# Repository Guidelines

## Project Structure & Module Organization

This is a mixed TypeScript/Rust monorepo for an internal AI coding assistant.

- `apps/web/`: React + Vite Chinese web UI.
- `apps/api/`: Fastify API, SQLite persistence, authentication, WebSocket device gateway, and smoke test.
- `apps/agent/`: Current Windows Rust local executor (`company-agent`) and updater binary. The confirmed target is to reduce it to a local device bridge/daemon that hosts Codex App Server rather than keep a second general-purpose coding-agent runtime.
- `knowledge/`: versioned company context, especially `code-style.md` injected into generation requests.
- `data/` and `uploads/` are runtime-only and must not be committed.

Keep browser-facing logic in `web`, server authorization and audit logic in `api`, and local filesystem enforcement in `agent`. Do not move local-path trust decisions to the server.

## Confirmed Target Architecture (Implemented in Parallel, Not Yet Production-Cut Over)

The project is moving from a custom server-side LLM tool loop plus Rust executor to a Codex-based local agent runtime:

- Run a pinned Codex App Server process on each Windows device and communicate with it locally over stdio JSON-RPC.
- Use DeepSeek's native Responses API as the Codex model provider. `deepseek-v4-flash` has been manually validated for the intended workflow.
- Keep the real DeepSeek API key only on the central server. Codex should call a company Responses API proxy using a short-lived, device-bound token supplied by the local daemon.
- Keep the Rust program as a small device bridge for pairing, Credential Manager storage, WSS reverse connection, heartbeats, reconnection, directory selection, Codex lifecycle/version management, event forwarding, backups, and audit upload.
- Run Codex tasks only in daemon-managed sanitized shadow workspaces; synchronize successful deltas back with original-file hash preconditions and redact paths/secrets before WSS upload. See `docs/shadow-workspace-security.md`.
- Retire the custom general-purpose Agent loop and local tools (`list_files`, `read_file`, `stage_patch`, `apply_patch`, and the `run_command` classifier) after the Codex path is proven and migrated.
- Preserve the current Web UI, central authentication, device/project ownership, audit, quotas, and task persistence.
- Do not expose Codex App Server directly over the network. The daemon starts it as a child process and uses stdio; the existing daemon-to-API channel remains validated WSS.
- Pin the bundled Codex runtime, model catalog, configuration template, and JSON-RPC schema. Do not resolve or launch an arbitrary user-installed `codex` from `PATH`.

The confirmed target application stack is:

- Web: Vue 3, TypeScript, Vite, Pinia, Vue Router, and Naive UI.
- Central API: Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, and HTTPX.
- Persistence: PostgreSQL. Do not design the target multi-user/device/task system around SQLite.
- Browser streaming: SSE for task and conversation events; ordinary HTTP for commands and approval responses.
- Device transport: validated WSS between the central API and the Windows daemon.
- Local control: stdio JSON-RPC between the Rust daemon and Codex App Server.
- Tests: Vitest and Playwright for the web app; pytest for the API; cargo test for the daemon.

Do not introduce React or Fastify into new target-architecture work unless this decision is explicitly revisited. Existing React/Fastify/SQLite code is the legacy implementation and may remain during migration.

The target slices are implemented in parallel, but production-environment acceptance is incomplete and the legacy path remains the default. Before changing code, read `docs/codex-deepseek-target.md` and `docs/rollout-acceptance.md`, and preserve a rollback path during migration. Do not switch the root package scripts or remove the legacy path until those gates pass.

## Build, Test, and Development Commands

From the repository root:

```powershell
npm install          # install web/API dependencies
npm run dev          # Vite UI + tsx API watcher
npm run build        # build web, then type-check/build API
npm test             # API smoke test: auth, pairing, conversation generation
```

For the Windows Agent:

```powershell
cd apps/agent
$env:PATH = "C:\msys64\ucrt64\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
cargo test
cargo run -- run
```

For the parallel target applications, use the commands documented in `apps/api-next/README.md` and `apps/web-next/README.md`. The target acceptance suite includes `pytest`, `ruff check .`, Vitest, the Vue production build, and Playwright. Root npm commands intentionally continue to validate the legacy path.

The Agent pins its GNU Rust toolchain in `rust-toolchain.toml`; use it rather than the MSVC default on this development machine.

## Coding Style & Naming Conventions

Use TypeScript strict mode and keep API validation in Zod at request boundaries. Use `camelCase` in TypeScript and `snake_case` in Rust. Preserve the existing compact UI style, but prefer readable multi-line code for new non-trivial flows. Rust code must return actionable `anyhow` errors and never bypass sandbox path validation.

## Testing Guidelines

Run the tests appropriate to every touched path before committing. For legacy changes run `npm run build` and `npm test`; for target API changes run `pytest` and `ruff check .`; for target web changes run Vitest, the production build, and Playwright; for daemon changes run `cargo test`. Add API coverage when changing authentication, device pairing, task status, or permissions. Add Rust unit tests for path traversal, file limits, task deduplication, and write preconditions. Do not test against real user projects; use temporary directories.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit-like format: `feat: add local agent control panel`, `fix: prevent duplicate agent task delivery`, or `chore: ...`. Keep each commit focused. PRs should describe behavior, security implications, tests run, and include screenshots for UI changes. Link the relevant issue or task when one exists.

## Security & Configuration

Never commit `.env`, SQLite databases, upload data, pairing codes, credentials, short-lived model tokens, or private signing keys. Development may use `ws://localhost`; any non-local deployment must use validated `wss://`. The current implementation still requires staged preview, explicit approval, and unchanged source hash. Do not remove those controls until the Codex migration provides tested workspace boundaries, task-level backup/rollback, and equivalent audit coverage.
