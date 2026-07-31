# Repository Guidelines

## Project Structure & Module Organization

This is a mixed TypeScript/Rust monorepo for an internal AI coding assistant.

- `apps/web/`: React + Vite Chinese web UI.
- `apps/api/`: Fastify API, SQLite persistence, authentication, WebSocket device gateway, and smoke test.
- `apps/agent/`: Windows Rust local executor (`company-agent`) and updater binary.
- `knowledge/`: versioned company context, especially `code-style.md` injected into generation requests.
- `data/` and `uploads/` are runtime-only and must not be committed.

Keep browser-facing logic in `web`, server authorization and audit logic in `api`, and local filesystem enforcement in `agent`. Do not move local-path trust decisions to the server.

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

The Agent pins its GNU Rust toolchain in `rust-toolchain.toml`; use it rather than the MSVC default on this development machine.

## Coding Style & Naming Conventions

Use TypeScript strict mode and keep API validation in Zod at request boundaries. Use `camelCase` in TypeScript and `snake_case` in Rust. Preserve the existing compact UI style, but prefer readable multi-line code for new non-trivial flows. Rust code must return actionable `anyhow` errors and never bypass sandbox path validation.

## Testing Guidelines

Run `npm run build`, `npm test`, and `cargo test` before committing. Add API smoke coverage when changing authentication, device pairing, task status, or permissions. Add Rust unit tests for path traversal, file limits, task deduplication, and write preconditions. Do not test against real user projects; use temporary directories.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit-like format: `feat: add local agent control panel`, `fix: prevent duplicate agent task delivery`, or `chore: ...`. Keep each commit focused. PRs should describe behavior, security implications, tests run, and include screenshots for UI changes. Link the relevant issue or task when one exists.

## Security & Configuration

Never commit `.env`, SQLite databases, upload data, pairing codes, credentials, or private signing keys. Development may use `ws://localhost`; any non-local deployment must use validated `wss://`. File writes require a staged preview, explicit approval, and unchanged source hash.
