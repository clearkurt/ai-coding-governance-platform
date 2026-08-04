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

The localhost test remains deterministic, authenticated, catalog-pinned, external-network-free and bounded by timeouts. Run it with:

```powershell
$env:COMPANY_AGENT_REAL_CODEX = "<approved-pinned-codex.exe>"
cargo test real_codex_completes_turn_against_local_responses_stub -- --ignored --nocapture
Remove-Item Env:COMPANY_AGENT_REAL_CODEX
```
