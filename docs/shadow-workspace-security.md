# Task shadow workspace security boundary

Codex App Server never receives the real authorized project root as its task working directory. For each Codex task, the Windows daemon creates a sanitized shadow workspace under the daemon's local application-data directory and passes only that path to `thread/start` and `turn/start`.

The mirror excludes `.env` variants, private keys and certificates, SSH/GPG directories, credential and auth directories, `.git`, dependency directories, virtual environments, build outputs, coverage data, and caches. Any symbolic link, junction, or Windows reparse point aborts mirror creation. The initial limits are 10,000 files, 20 MiB per file, and 100 MiB total. Original relative paths, sizes, and SHA-256 hashes are stored in daemon-controlled metadata beside the Codex-visible mirror, never inside it.

On a successful Codex turn, the daemon computes the allowed file delta. Existing files are written or deleted only when the real file still matches its original SHA-256. A concurrent user change or same-path creation fails the entire synchronization without overwriting the user. Changed files are staged outside the real project; replaced or deleted files are moved to transaction recovery storage first. If application fails, already-applied changes are restored and the task is reported as failed. Empty-directory changes are not synchronized in this first version.

Excluded and sensitive paths are never synchronized in either direction. The pre-task backup remains the explicit task rollback source and is retained until a future explicit cleanup policy removes it. The legacy executor remains available through `UseLegacy` during migration.

Before WSS upload, the daemon maps real and shadow absolute paths to `$WORKSPACE` and recursively redacts sensitive keys such as tokens, passwords, secrets, API keys, authorization values, cookies, and credentials. Strings shaped like bearer credentials, common API keys, or private-key blocks are replaced with `[REDACTED]`. This is defense in depth and does not make arbitrary binary output safe for audit ingestion.
