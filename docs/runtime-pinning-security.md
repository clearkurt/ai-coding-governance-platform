# Codex runtime pinning

The daemon launches Codex only from its application-data `runtime/releases/<version>` directory. This daemon build requires runtime `0.145.0-alpha.27`, App Server schema `app-server-schema-1`, model catalog `deepseek-v4-flash-1`, and configuration template `company-responses-1`, plus an exact 64-hex-character SHA-256. Arbitrary non-empty version values are rejected.

`configure-codex` treats the supplied artifact as untrusted staging input. It validates the manifest shape, hashes the artifact, executes only `--version` for version verification, copies the artifact and manifest into a temporary managed release directory, verifies the copied bytes, and atomically publishes the directory. Replacement keeps the prior directory until publication succeeds.

At every daemon startup and every App Server restart, runtime validation reloads the installed manifest, requires it to equal the configured pin, canonicalizes the derived managed executable, verifies that it remains below the managed runtime directory, and recalculates SHA-256 before checking `--version` again. User `PATH`, WindowsApps aliases, arbitrary executable paths, missing hashes, and in-place staging execution are not runtime inputs.

This mechanism provides deterministic local pinning and tamper detection. Release provenance still depends on the authenticated company release workflow that supplies the manifest and artifact; this repository does not embed a release binary, private signing key, or machine-specific path.

Changing any of the four compatibility versions requires a new daemon build and its contract tests to pass through the company release process. Updating a release record alone cannot make an existing daemon accept a different runtime or protocol contract.
