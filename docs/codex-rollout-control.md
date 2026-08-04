# Codex rollout control

Status: the gate and its PostgreSQL recovery behavior have passed local automated integration. Production cohort selection and observation remain release activities.

The central FastAPI control plane has a production-only, fail-closed gate for creation of new Codex tasks. It does not switch the default application entry point or remove the legacy rollback path.

- `disabled` is the default and rejects every new authorized task with a fixed 503 response.
- `allowlist` accepts only device UUIDs in `COMPANY_AGENT_CODEX_ROLLOUT_DEVICE_IDS`, encoded as a JSON array. UUIDs are strictly parsed, deduplicated, limited to 1000, and the production list must be non-empty.
- `all` permits every otherwise-authorized device and is an explicit high-risk production choice.

Authorization is evaluated before the rollout gate. Invalid cross-team or cross-project targets receive the existing fixed 403 regardless of rollout mode, so response differences cannot enumerate allowlisted devices. The store revalidates authorization during creation.

The gate appears only in `POST /tasks`. Switching to `disabled` does not block pending/running task delivery or replay, ACKs, events, approval decisions, cancellation, rollback, audit, or model tokens for an existing active task. This prevents emergency rollback from stranding in-flight work.

The real PostgreSQL integration creates a pending task, changes production configuration to `disabled`, reconstructs a fresh session/store, and verifies that the identical task and delivery still replay. This proves the local persistence contract, not a real-device gray rollout.

Recommended rollout is `disabled`, then a small reviewed `allowlist`, then larger reviewed batches. Use `all` only through a separate approved production change. For emergency rollback, return to `disabled`, retain pending records and audit evidence, and let recovery/ACK paths finish; do not revoke device credentials merely to stop new work.
