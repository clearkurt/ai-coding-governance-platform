# PostgreSQL backup and restore drill

Status: automated and environment-gated. PostgreSQL 16.14 `pg_dump` and `pg_restore` are available through WSL on this machine, but the drill has not yet passed on this checkout because this task environment does not provide `COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL`.

The integration test `test_postgres_custom_backup_restore_preserves_pending_control_plane_state` is the local control-plane recovery drill. It deliberately creates random source and target databases and login roles; it never dumps, restores, drops, or changes a named application database. Cleanup terminates connections to the random target database and removes both databases and roles in `finally` paths.

The drill requires:

- a disposable PostgreSQL 16 instance;
- an administrative test URL whose role has `CREATEDB` and `CREATEROLE`;
- `pg_dump` and `pg_restore` from the same PostgreSQL major version as the server;
- no production credentials or production database URL.

Run it from `apps/api-next`:

```powershell
$env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL = "postgresql://test_admin:<password>@127.0.0.1:5432/postgres"
$env:COMPANY_AGENT_TEST_PG_DUMP = "<PostgreSQL-16-bin>\pg_dump.exe"       # optional when on PATH
$env:COMPANY_AGENT_TEST_PG_RESTORE = "<PostgreSQL-16-bin>\pg_restore.exe" # optional when on PATH
# Or use PostgreSQL 16 tools inside WSL; do not set the two executable variables above:
$env:COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX = '["wsl","-d","Ubuntu-24.04","--"]'
.\.venv\Scripts\python.exe -m pytest -m integration -q -rs
Remove-Item Env:COMPANY_AGENT_TEST_POSTGRES_ADMIN_URL
Remove-Item Env:COMPANY_AGENT_TEST_PG_DUMP -ErrorAction SilentlyContinue
Remove-Item Env:COMPANY_AGENT_TEST_PG_RESTORE -ErrorAction SilentlyContinue
Remove-Item Env:COMPANY_AGENT_TEST_POSTGRES_TOOL_PREFIX -ErrorAction SilentlyContinue
```

The optional tool prefix must be a JSON array containing one or more non-empty strings. It is prepended to `pg_dump` and `pg_restore` as an argument vector; the test never enables a shell. The custom dump travels over subprocess stdout/stdin so no Windows temporary path is passed to a WSL process. With the WSL example, PostgreSQL must be reachable from WSL at the `127.0.0.1` address in the administrative URL before running the drill.

The backup uses custom format with `--no-owner`; the restore uses `--no-owner --exit-on-error`. Passwords are passed only through the child process environment and are removed from command-line URLs. Both commands have a 120-second timeout and the temporary dump directory is automatically deleted.

Acceptance requires all of the following after connecting a new SQLAlchemy engine and creating a fresh `PostgresStore` against the restored database:

- 18 application tables, 21 foreign keys, 26 unique constraints, and the complete `task_status` enum;
- device authentication and the same pending task/delivery identity;
- the same unacknowledged approval decision, followed by successful ACK removal;
- authoritative event sequence and source-event deduplication identities;
- preserved model-token usage totals;
- task, delivery, approval, and model-token audit records.

An absent admin URL or missing/mismatched PostgreSQL client tool causes a clear test skip. A skip is not evidence that backup and restore work. Before production rollout, run this drill against an isolated PostgreSQL 16 environment matching production and retain the test output in the release evidence.
