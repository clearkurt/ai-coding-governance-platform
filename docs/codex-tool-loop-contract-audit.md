# Pinned Codex tool-loop contract audit

Status: the request-side tool contract and deterministic non-text execution loop are verified against the real pinned artifact.

On 2026-08-04 the ignored, environment-gated Rust test `real_codex_completes_turn_against_local_responses_stub` was run against the real `codex-cli 0.145.0-alpha.27` artifact. Its authenticated localhost Responses request declares a function tool named `shell_command`. The test now asserts only the minimum security-relevant request contract without printing or retaining the request body or token:

- tool type is `function` and name is `shell_command`;
- parameters are an object and `command` is required with string type;
- `sandbox_permissions` is limited to `use_default` and `require_escalated`.

The real request also advertised planning, user-input, image, goal, multi-agent and web-search capabilities. Those names are not accepted as evidence that this project should allow them in the deterministic mutation test. The intended fixture must request only a bounded file change in a temporary shadow workspace, and the test must reject network, hardware and unrelated external side effects.

The full ignored test now uses the official Responses function-call event contract and has been run successfully against the exact pinned artifact. The localhost stub emits `response.output_item.added`, function-call argument delta/done, `response.output_item.done`, and `response.completed` for one `shell_command` call. The real App Server then emits exactly one `item/commandExecution/requestApproval`. The test checks that the approval describes only the fixed temporary-workspace filename and contains no network or model-provider target before explicitly returning `accept`.

The next Responses request must contain a string `function_call_output` with the identical call ID. After the final deterministic text response, the test proves:

1. the approved file exists only in the shadow workspace before synchronization;
2. existing hash-preconditioned `ShadowWorkspace::sync_back` publishes it to the canonical temporary project;
3. the pre-turn `BackupStore` snapshot removes the new file and restores the original project content;
4. the real artifact reaches its normal terminal notification and shuts down cleanly.

Cancellation remains a separate future ignored test: it has different timing and terminal-event assertions, and combining it with mutation/rollback would make failures ambiguous and the fixture nondeterministic.

## Cancellation release gate

The separate ignored test `real_codex_cancels_observed_local_responses_stream` now encodes the complete cancellation contract with a synchronized localhost long-running stream. It waits until the real pinned artifact has received `response.created` and `response.in_progress`, calls the existing `turn/interrupt` API, requires `turn/completed` with `turn.status` exactly `interrupted`, rejects approvals, checks both real and shadow workspaces remain unchanged, and requires the upstream TCP stream to close within ten seconds.

The initial real `0.145.0-alpha.27` runs emitted the correct `interrupted` terminal status and left the workspaces unchanged, but the upstream Responses connection remained open for the full ten-second observation window when only `turn/interrupt` was called. This established that an interrupted notification alone does not release the upstream request.

The daemon production path now keeps its strict single-active-task lease after `task.cancel`, waits for the interrupted terminal, persists that terminal without syncing the shadow workspace, and then shuts down the task-bound App Server process tree. The outer pinned-runtime loop starts and initializes a fresh App Server for the next task. Clearing the DispatchBook before returning prevents process-exit recovery from adding a synthetic failure over the cancelled terminal.

The real cancellation test mirrors that lifecycle by shutting down immediately after the strict interrupted terminal. It then requires the upstream connection to close. Two consecutive real runs passed, followed by a successful real deterministic text turn using a fresh App Server. The ordinary test suite separately proves that the active-task lease rejects a second task and that normal terminal removal prevents synthetic recovery failure.

The localhost test remains deterministic, authenticated, catalog-pinned, external-network-free and bounded by timeouts. Run it with:

```powershell
$env:COMPANY_AGENT_REAL_CODEX = "<approved-pinned-codex.exe>"
cargo test real_codex_completes_turn_against_local_responses_stub -- --ignored --nocapture
Remove-Item Env:COMPANY_AGENT_REAL_CODEX
```
