# Pinned Codex App Server contract

Generated and inspected locally on 2026-08-04 from
`codex-cli 0.145.0-alpha.27` using `codex app-server generate-json-schema`.

The bridge uses JSON-RPC 2.0 over `app-server --stdio --strict-config` JSONL.

- Client requests: `initialize`, `thread/start`, `turn/start`, `turn/interrupt`.
- Server notifications: `thread/started`, `turn/started`, `turn/completed`,
  `item/started`, `item/completed`, `item/agentMessage/delta`,
  `item/commandExecution/outputDelta`, `item/fileChange/patchUpdated`, and
  `error`-style notifications.
- Server approval requests: `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`, `item/permissions/requestApproval`.

`initialize` requires `clientInfo.name` and `clientInfo.version`.
`thread/start` accepts `cwd`, approval policy, and `sandbox`; `turn/start`
requires `threadId` and `input`; `turn/interrupt` requires `threadId` and
`turnId`. This intentionally small contract is version-pinned; changing the
bundled runtime requires regenerating and reviewing the schema first.
