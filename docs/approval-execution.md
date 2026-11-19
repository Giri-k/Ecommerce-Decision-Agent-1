# Phase 2: Local approval and durable execution

Implemented and verified November 18, 2026; status reviewed against the proactive approach on November 19. Real local PostgreSQL, authenticated HTTP requests, restricted service credentials, and committed receipts provide the action boundary for the upcoming controllers. No LLM API calls or hosted account are required for this local backend.

## Run locally

After `make bootstrap` and `make data`, from the project root:

```sh
make local-setup
make approval-demo
make api
```

Setup starts native PostgreSQL on `127.0.0.1:54329`, applies checksummed migrations, seeds two independent stores, and provisions restricted roles. Existing stores and receipts are preserved. Each setup issues fresh 24-hour development sessions; existing sessions remain valid until expiry or administrator revocation.

Visit `http://127.0.0.1:8000/docs`. The API loads only `var/runtime/api.env`. `/health` checks the process; `/ready` checks connectivity, the restricted API role, and operational schema. It does not certify worker availability.

Ignored, owner-only files under `var/runtime/`:

| File | Purpose |
| --- | --- |
| `api.env`, `api.json` | Restricted API connection |
| `agent_0.json`, `executor_0.json`, `simulator_0.json` | Separate credentials bound to store-a |
| Corresponding `_1.json` files | Credentials bound to store-b |
| `sessions.json` | User bearer tokens; never commit or include in reports |
| `admin.json`, `services.json` | Provisioning secrets; never give these to a runtime service |

Seeded identities: merchant-a (store-a admin), approver-a (store-a approver), viewer-a (store-a viewer), merchant-b (store-b admin). This authenticates local development requests; hosted signup/login remains Phase 4.

PostgreSQL data and bootstrap credentials remain in ignored `var/local-db/`. `make local-db-stop` stops it without deleting data. The development dependency `embedded-postgres` supplies native binaries (tested server: 18.6). Docker was not needed or tested; the optional Compose definition alone does not provision application roles.

## Complete deterministic demo

`make approval-demo` creates a separate `ec_demo_*` database per invocation, preserving the regular stores. Credentials stay under `var/approval-demo/runs/<run>/`. The declared fixture uses M5-derived rates with one day of starting inventory to trigger replenishment; it is separate from normal policy-comparison scenarios.

The existing rules control reads the snapshot, forecasts demand, and validates its bundle. Three numerical traces are persisted with the investigation. A real agent-role database connection submits the immutable proposal. An automated script acts as the seeded authorized merchant and approves its exact hash over the real HTTP route; no human merchant UI or LLM is involved. Execution remains paused until the scripted admin enables it.

The separate executor commits the bundle, receipt, purchase orders, and audit event together. Duplicate approval returns the existing job; duplicate execution returns the existing receipt. Three simulator advances use fresh worker instances and the durable private checkpoint. Execution is paused again at the end.

Inspect `var/approval-demo/latest-report.json` for actions, hash, approval, receipt, and day/version results. Reports contain no passwords or bearer tokens. IDs and timestamps vary across invocations; this checks workflow and recovery, not byte-identical evaluation or economic performance.

For separately queued work in the regular store:

```sh
make executor
make simulator-worker
```

Each processes at most one eligible job and exits, returning `idle` when none can be claimed. These are execution and simulator workers, not an investigation worker. The worker exposes heartbeat/lease renewal; shared monitoring, automatic investigation scheduling, and supervised continuous investigation processes remain Phase 3 work.

## API contract

Protected routes require `Authorization: Bearer <session>`. Investigation/proposal/job routes also require `X-Tenant-ID`. Create/decision/advance requests require `Idempotency-Key`; changed input under a reused key conflicts.

| Route | Behavior |
| --- | --- |
| `GET /me` | Verified user and memberships |
| `GET /stores/{tenant}` | Visible snapshot, date, execution switch |
| `GET /stores/{tenant}/sales?lookback_days=56` | Completed simulated sales; 1–365-day bounded lookback |
| `POST /investigations` | Question and new investigation; approver/admin |
| `GET /investigations/{run}` | State, traces/checkpoints, proposal references |
| `GET /proposals/{id}` | Immutable payload/hash, status, receipt |
| `POST /proposals/{id}/approve` or `/reject` | Body `{"payload_hash":"…"}`; approver/admin |
| `GET /execution-jobs/{id}` | Durable state/retry information |
| `GET /executions/{id}` | Committed receipt |
| `POST /admin/execution` | Body `{"enabled":true}`; admin only |
| `POST /sandbox/advance` | Body `{"snapshot_version":1}`; enqueue one day; admin only |
| `GET /sandbox/jobs/{id}` | Durable simulator state/result |

Extra approval fields such as `approver`, `approved`, or `session_id` are rejected. Identity comes from server-side sessions. Proposal submission is a backend service operation. There is no merchant browser interface yet.

## Transaction and security boundary

Migrations are in `src/ecommerce_agent/sql/`; `meta.migrations` records their checksums. Add a migration instead of editing an applied one. Runtime roles have no schema ownership, superuser, BYPASSRLS, role-creation, or database-creation rights.

One aggregate row holds the small ten-product store snapshot. Public mutations lock store first, then proposal, then job. This serializes cash/inventory changes consistently. Proposals, approvals, orders, receipts, jobs, traces, and released sales have separate tenant-scoped records with forced row-level security.

User scope comes from active membership and an unexpired/unrevoked server session. Service scope comes from the login's protected tenant binding, not caller-selected user/tenant settings. Agents cannot inspect private evaluator state, approve, write store state, or assume executor privileges.

Approvals bind the canonical hash and verified user/session. Payloads, approvals, receipts, audit records, policy versions, and thread records are immutable to runtime services. Edits require a new proposal. Audit records are append-only, not a cryptographic log against the database administrator.

The executor rechecks approval authority, payload/hash, 15-minute expiry, simulated day, snapshot/policy versions, pricing/cadence rules, packs/caps, and shared cash/holding reserve inside its transaction. It commits the entire bundle or none, with one receipt per tenant/proposal. The simulator follows the same store-lock/version protocol.

Leases use row locks and `SKIP LOCKED`, unique owners, heartbeats, expiry, and five attempts by default. Reclaimed work fences the old worker. Terminal validation errors are recorded; interrupted work can be retried. Exhausted jobs become `FAILED`; the historical proposal can remain `APPROVED`, which does not imply execution.

A crash before commit rolls back all effects; retry after commit returns the receipt. These guarantees cover database effects; this project places no external supplier orders or payments.

Before advancing, the simulator reconciles unapplied execution receipts in version order, registers private arrival timing, and accounts for procurement once. It commits public state, private checkpoint, completed sales, order status, and job result together. Retries cannot duplicate sales or deliveries. Only fulfilled sales, dates/prices, and visible stock indicators enter `app.observed_sales`; latent demand, hidden elasticity, future delays, private seeds, and evaluator ledgers stay private.

The same local OS user can read generated secrets. Deployment must distribute only each service's credential and add hosted identity, TLS, supervision, and secret management.

## Verification and next work

The last recorded full `make check` verification on November 18 passed 137 tests, lint, formatting, and smoke. Twenty-five real PostgreSQL integration cases cover tampering, tenant isolation, runtime privileges, expiry/revocation, cash/decision races, execution/advance races, idempotency, fencing, and executor/simulator crashes before and after commit. Tests create/drop disposable databases; no Docker, M5 download, API key, or paid service is needed. One upstream Starlette/httpx warning remains. The November 19 status review does not represent an additional test run.

The deterministic demo, regular-store setup, and actual HTTP readiness/authentication also passed. The API was stopped after verification; PostgreSQL data remains locally.

The next work is Phase 3A public observations and shared monitoring, Phase 3B durable automatic scheduling with its own restricted service identity, and Phase 3C a complete persisted 28-day fixed-workflow episode. Current investigation creation requires a human approver/admin session; automation must not reuse a merchant token or acquire approval rights. Phase 3D then adds the course-gateway model adapter, bounded agent loop, budgets, evidence checks, and investigation resume. See [the current architecture and next steps](current-architecture-and-next-steps.md).

The checkpoint table is storage infrastructure, not a completed adaptive workflow or LangGraph integration. Phase 4 adds the merchant UI/deployment. The required comparison now includes the fixed deterministic workflow, one adaptive agent, and a multi-agent system. Both agent architectures remain unimplemented; the rules controller remains a development reference. Every evaluated controller must use the same monitoring logic, reviews on days 1/4/7/10, and approval/executor boundary. Multi-agent specialists provide analyses to one supervisor, which submits one final proposal; they do not independently approve or spend shared cash. `make controls` remains a privileged numerical development harness.

References: [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html), [function security](https://www.postgresql.org/docs/current/sql-createfunction.html), [locking and SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html), [embedded-postgres](https://github.com/kainpl/embedded-postgres).
