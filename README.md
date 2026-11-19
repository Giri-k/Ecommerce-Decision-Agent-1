# Ecommerce Decision Agent

A course project for a proactive assistant that helps a merchant decide when to reorder products or change prices. It operates on a simulated store with ten products, one warehouse and two suppliers. The merchant must approve a recommendation before the system applies any simulated change.

The project will compare a fixed numerical workflow, one adaptive AI agent, and a coordinator with inventory and pricing agents. All three will use the same store information, calculation tools, business rules and approval process.

## Current state

Implemented:

- M5 data preparation with reproducible product selection, chronological splits and checksums.
- Daily store simulation covering sales, stock, deliveries, cash and costs.
- Local forecasting methods and calculations that compare order and price choices.
- A fixed decision algorithm and a separate rules control for development checks.
- A FastAPI backend with authenticated local users and store-scoped permissions.
- Exact-plan approvals, transactional execution, duplicate protection and restart recovery.
- Tests, linting and a GitHub Actions workflow.

Still to build:

- Automatic monitoring, durable alerts and scheduled investigations.
- The fixed controller's complete persisted application workflow.
- The single AI agent and the required coordinated agent team.
- The merchant interface, hosted authentication and deployment.
- The final matched comparison and analysis.

No live language-model calls have been made. The forecasts run locally. The approval demo uses a script acting as the merchant, and the current numerical comparison is a development harness rather than the final experiment. The project does not place real supplier orders or make payments.

## 1. Clone and install

Use a Unix shell, GNU Make and `uv` (tested version: `0.8.22`). Python is pinned to `3.11.13` in `.python-version`; `uv` can install it. Windows users should use WSL for these commands.

Clone the repository, then run the setup commands from its root:

```sh
git clone https://github.com/harenshylak/Ecommerce-Decision-Agent.git
cd Ecommerce-Decision-Agent
uv python install
make bootstrap
make check
make seed
make budget
```

`make bootstrap` installs the locked dependencies. `make check` runs linting, formatting checks, tests and the fixture smoke check. Tests start native PostgreSQL and use disposable test databases. They need permission to start local processes and bind localhost ports, but do not require Docker, M5 files or model credentials.

`make seed` writes a small synthetic operational fixture under `var/fixture/`. `make budget` prints estimates without contacting a model provider. `make smoke` is available when you want a quick check without starting PostgreSQL.

Initial installation needs internet access. Run these commands as your normal user, rather than root, because PostgreSQL refuses to run as root.

## 2. Prepare the historical data

The official M5 files stay outside Git. Obtain them through the [M5 Forecasting Accuracy data page](https://www.kaggle.com/competitions/m5-forecasting-accuracy/data), following its access process, and place these files in `data/raw/`:

```text
calendar.csv
sales_train_evaluation.csv
sell_prices.csv
```

Then run:

```sh
make data
make simulate
make forecast-validation
make controls
```

You can use an existing download directory instead:

```sh
make data M5_SOURCE_DIR='/path/to/m5'
```

The importer writes the ten-product panel, split information, data-quality report and checksums to `var/data/m5/`. The expected panel has 19,410 daily records. Training uses days 1–1801, validation uses days 1802–1885, and the final historical test uses days 1886–1941. Keep the final test untouched while developing the methods.

`make simulate` runs a no-change simulation. `make forecast-validation` compares the local forecasts on validation data. `make controls` runs the rules and fixed numerical workflows on development scenarios. Outputs appear under `var/` and are regenerated locally.

See [data/raw/README.md](data/raw/README.md) for input checksums and download details. Raw or prepared M5 sales data is not needed to run the tests or synthetic fixture commands above.

## 3. Run the local backend

After preparing the historical data:

```sh
make local-setup
make approval-demo
make api
```

Local setup starts PostgreSQL at `127.0.0.1:54329`, applies migrations and creates separate API, agent, executor and simulator credentials. It also creates local merchant, approver and viewer sessions. Existing store state is preserved. Generated credentials and sessions are private files in ignored `var/runtime/`; never commit or share them. Each teammate should generate their own.

The API runs at `http://127.0.0.1:8000`. Open `/docs` for its routes, `/health` for process health and `/ready` for database readiness. Protected routes require a local bearer session and store membership. There is no merchant web interface yet.

`make approval-demo` creates a separate development database. It proposes a rules-based order, uses the real approval route, executes the approved bundle once and advances the simulator through saved checkpoints. The report is `var/approval-demo/latest-report.json`. It pauses execution at the end.

Additional commands:

```sh
make executor          # process at most one queued execution job
make simulator-worker # process at most one queued day-advance job
make local-db-stop     # stop PostgreSQL without deleting its data
```

Execution starts disabled for the regular stores. An authenticated store admin can enable it through `/admin/execution`. Workers above handle execution and simulation, not automatic investigation.

The optional `infra/compose.yaml` starts PostgreSQL with Docker using settings from `.env.example`. It does not replace application role provisioning. The native `make local-setup` path is the current development setup.

## Project layout

```text
src/ecommerce_agent/    Application, simulation, forecasts, planning and persistence
src/ecommerce_agent/sql/ Database migrations
config/development.yaml Shared development assumptions and limits
tests/                 Unit and real-PostgreSQL integration tests
infra/compose.yaml     Optional PostgreSQL container
.github/workflows/     CI checks
uv.lock                Locked dependencies
var/                   Generated local files, databases, secrets and reports (ignored)
data/raw/              Locally obtained M5 inputs (CSV files ignored)
```

## Where to continue

Start with [the architecture and next implementation steps](docs/current-architecture-and-next-steps.md). It describes the agreed scope and acceptance checks for each remaining phase:

1. Build a common view of public observations and a monitor that records alerts.
2. Add a restricted scheduler identity and durable investigation jobs.
3. Run the fixed controller through the complete approval, execution and saved-state path.
4. Connect the course gateway and implement the bounded single-agent tool loop.
5. Add the coordinator, inventory agent and pricing agent under one total team budget.
6. Run a small matched development comparison before the final evaluation.

The [approval and execution runbook](docs/approval-execution.md) explains identities, API contracts, migrations and recovery. Preserve that authority boundary when adding controllers. Add a migration rather than editing an already applied migration.

All approaches review on days 1, 4, 7 and 10 within a 14-day decision window. A further 14 days lets deliveries and sales continue without new order or price decisions. The proposed final experiment is 30 held-out scenarios, three matched repetitions and three approaches, for 270 store runs. This experiment has not run yet. Historical forecast accuracy and simulated store outcomes are separate results.

Document retrieval/RAG and embeddings are outside the agreed scope. The course-provided model gateway still needs its base URL, authentication, tool support and usage reporting verified. Add live credentials only to ignored local environment files or a deployment secret manager.

## Development routine

```sh
make bootstrap  # after dependency changes
make format
make check
```

Keep generated files, local sessions, raw data and course presentation material out of commits. The repository contains the source, tests, setup files and developer handoff instructions needed to continue the implementation.
