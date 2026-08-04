# Local TLS/WSS integration contract

Status: passed on this checkout against a real localhost uvicorn TLS process and a random PostgreSQL 16 database. This result does not replace production reverse-proxy or PKI validation.

The environment-gated API integration `test_uvicorn_tls_wss_validates_ca_hostname_and_delivery_replay` validates the application's local TLS WebSocket contract without using a production certificate or private key.

The test creates a temporary one-use CA and RSA server certificate with the explicit `IP:127.0.0.1` subject alternative name. It starts a real uvicorn TLS child process on a random localhost port against a random migrated PostgreSQL database. A client that trusts only the temporary CA must authenticate a device, receive a persisted delivery, disconnect, receive the identical delivery again, and ACK it. A default client that does not trust the temporary CA must fail its TLS handshake. A client that trusts the CA but requests `localhost` instead of the certified `127.0.0.1` identity must also fail hostname verification.

Client certificate and hostname verification remain enabled in every path. The test does not use an unverified SSL context. The temporary CA certificate, server certificate, CSR and private keys live only in a temporary directory that is removed after the test. Certificate tool output and private key material are not printed or committed. The uvicorn process has bounded startup, I/O and shutdown waits and is terminated, then killed if necessary, during cleanup.

On this Windows development machine, certificate generation uses OpenSSL from the `Ubuntu-24.04` WSL distribution because the Python environment does not include a certificate-generation library. If WSL or OpenSSL is unavailable, the integration reports a clear skip. This is test tooling only and does not prescribe the production certificate issuer or termination topology.

Run the full PostgreSQL integration suite from `apps/api-next` after supplying the disposable administrative test URL:

```powershell
$env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL = "postgresql://test_admin:<password>@127.0.0.1:5432/postgres"
.\.venv\Scripts\python.exe -m pytest -m integration -q -rs
Remove-Item Env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL
```

Passing this test proves only that direct localhost uvicorn TLS, WebSocket upgrade, certificate trust, hostname verification and database-backed replay work together. Production acceptance still requires the deployed domain and certificate chain, reverse-proxy behavior, certificate rotation and expiry monitoring, TLS policy, WSS idle/upgrade timeouts, and reconnect behavior through the real network path.
