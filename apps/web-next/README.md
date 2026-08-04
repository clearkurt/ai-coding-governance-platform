# Vue target web application

This is the Vue 3 replacement UI for the Codex migration. It runs alongside the legacy React app until cutover.

```powershell
npm install
npm run dev
npm test -- --run
npm run build
npm run test:e2e
```

The Vite development server proxies browser API requests to `http://127.0.0.1:8081`. Set `VITE_API_BASE_URL` only when the API is intentionally hosted on another origin and that deployment has matching credential/CORS policy.

The current workspace supports session login, device pairing codes, device/project discovery, conversation creation, task creation, SSE event replay, approvals, cancellation and task rollback.
