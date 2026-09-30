# Build log

Chronological engineering decisions. Each entry: problem, decision, why, alternatives, result.

## 2026-09-28, Day 1

### Repo layout: one python package

- **Problem:** the suggested layout had code in `app/`, `target_platform/`, `scripts/`, `ui/`. Three importable top-level packages complicates packaging and the Docker image.
- **Decision:** all importable code lives in `app/`. `target_platform/` holds the fictional platform's schema exports and documentation, not code. `scripts/` and `ui/` are thin entry points.
- **Why:** one package, one `pyproject`, one `uv sync`, one import root in tests.
- **Alternatives:** a `src/` layout with multiple packages. More ceremony than a one-week build needs.
- **Result:** `uv sync` and the Docker build are a single step.

### Postgres from compose on port 5433, not 5432

- **Problem:** development machines often already run a PostgreSQL on 5432 (this one does).
- **Decision:** compose publishes the database on 127.0.0.1:5433 for local tools; inside the compose network the API uses `db:5432`.
- **Why:** `docker compose up` must work on a reviewer's laptop without them stopping their own database.
- **Result:** no port collision, and the database is not exposed beyond localhost.

### Ports bound to localhost only

- **Problem:** compose files usually publish `8000:8000`, which on a server with a public address exposes the API and UI to the internet.
- **Decision:** every published port is `127.0.0.1:<port>`. `http://localhost:8000` still works on a laptop; nothing listens on the public interface.
- **Why:** the tool handles customer data in real life; the default should not need a firewall to be safe.

### Two schemas in one database

- **Problem:** the fictional target platform needs its own tables, and the migration must not be able to bypass its API.
- **Decision:** `onboarding` and `target` schemas in the same PostgreSQL. The target schema is only written through the `/target/v1` API.
- **Why:** one database keeps the reviewer setup simple while the separation keeps the migration honest.
- **Alternatives:** a second database container. More moving parts for no additional signal.

### `create_all` now, alembic later

- **Problem:** the schema will change daily for a week.
- **Decision:** tables are created from the SQLAlchemy metadata at startup. Alembic migrations come once the model stops moving (Day 6 cleanup at the earliest).
- **Why:** migration files for a schema that changes every day would be noise; schema versioning is documented as a production consideration.

### Mock target platform lives in the same FastAPI process

- **Problem:** the migration layer must go through a realistic REST interface, and reviewers want one command.
- **Decision:** the target platform is a separate router (`/target/v1`) with its own tables, and the migration runner calls it over HTTP through `TARGET_API_BASE_URL`.
- **Why:** the network seam is real (serialization, status codes, timeouts, retries all apply) without a second service to run.
- **Alternatives:** a dedicated container. Easy to split later since the base URL is configuration.

### Plain python orchestration first

- **Problem:** the workflow has state, branching, and a human review step; LangGraph is a candidate.
- **Decision:** stage functions in plain Python with the project's stage stored in PostgreSQL. LangGraph is evaluated on Day 6 against the working workflow, and the outcome is recorded here either way.
- **Why:** the prompt's own rule: do not overengineer around the framework before the domain workflow exists.

### uv for dependencies

- **Decision:** `uv` with `pyproject.toml` + `uv.lock`, also inside the Docker build.
- **Why:** fast, reproducible, and the `pyproject.toml` is standard so pip or poetry users can still install.

### structlog for logging

- **Decision:** structlog with JSON output in containers, console output locally, request id bound per request, stage context bound per pipeline step.
- **Why:** the logs must let someone diagnose an onboarding failure by project id and run id without grepping free text.

### Synthetic data: commit a 1,000-org sample, generate 10,000 on demand

- **Problem:** the prompt is clear that a tiny perfect CSV is not acceptable, but a 10,000-organization customer is ~8 MB of CSV and does not belong in git.
- **Decision:** `sample_customer/data/` holds a 1,000-organization sample (seed 42) so the repo works out of the box; `make seed` or the CLI generates 10,000 (or any size) into an ignored `generated/` directory. Same generator, same seed handling, byte-identical output for a given seed.
- **Why:** reviewers get a real, messy dataset immediately and the repo stays small.

### Defects are counted, not assumed

- **Problem:** the docs will say things like "4% of emails are malformed"; that number must be true.
- **Decision:** every injected defect increments a counter and the counts are written to `manifest.json` next to the data. A test asserts every planned defect kind actually occurs at a 400-row size.
- **Why:** measured numbers only, including for the synthetic input. The same discipline applies to the mapping eval later.

### Where the ambiguity is planted

- `customer_tier` (Gold/Silver/Bronze/Strategic/Platinum) has two plausible targets, `account_priority` and `subscription.plan`; the rules doc says it is neither a plan nor exactly a priority.
- `CA` in the country column is Canada in billing and sometimes California in old CRM rows; the rules doc says to use the state column.
- `primary_contact_email` sits on the organization row but belongs to a contact.
- `company_status` has `On Hold` and `Closed`, which the rules doc resolves, and `Actve`, which nothing resolves.
- Strategic accounts on Starter plans, and Active accounts with no activity for 18+ months, are conflicts between the data and rules 2 and 4.

## 2026-09-30

### No .env file is ever committed, templates included

- **Problem:** `.env.example` was committed on Day 1 as the usual template. It held no secrets, but a `.gitignore` rule of `.env` with a committed `.env.example` next to it is one typo away from committing the real file, and the rule for this repo is that nothing matching `.env*` ever reaches the remote.
- **Decision:** `.env.example` was removed from every commit with `git filter-repo` and the history force-pushed (the remote was minutes old, no other clones). The template now lives at `deploy/env.template`, `.gitignore` carries `.env*` with no exceptions, and a committed pre-commit hook (`.githooks/pre-commit`, activated once per clone with `make hooks`) refuses any env file and runs gitleaks on the staged diff.
- **Why:** an ignore rule that needs an exception is a rule that gets bypassed. Renaming the template makes the rule absolute and the hook makes it mechanical.
- **Alternatives:** keep `.env.example` and rely on review. Cheaper today, and exactly how real keys end up in public repos.
- **Result:** no object in the repository history references `.env.example`. GitHub keeps unreferenced commits until its garbage collection runs; irrelevant here since the file never held a value.

### AWS demo becomes required, scoped to S3 + EC2 + an IAM role

- **Problem:** hosted deployment was a stretch item. The project now needs to show real cloud deployment experience without turning into an infrastructure project, and it must not lose the "one `docker compose up`" reviewer experience.
- **Decision:** AWS deployment is required and comes after the local workflow is verified end to end, not in parallel with it. Scope: a private S3 bucket for intake datasets and generated outputs (boto3, behind a `STORAGE_BACKEND=local|s3` switch), the Dockerized app on one small Linux EC2 instance, and an IAM role on the instance for S3 access. Postgres stays in Docker on the instance and is never publicly reachable. Ten checkboxes join the Definition of Done in the README.
- **Why:** EC2 + S3 + IAM covers the pieces an implementation engineer actually touches on a customer deployment (where the data lands, where the app runs, who is allowed to read what) and each one is explainable in an interview. Local first, because a workflow that only works in the cloud cannot be reviewed or debugged cheaply.
- **Alternatives:** RDS for Postgres (rejected: more surface, no onboarding signal, and the local and cloud databases would diverge). Long-lived access keys in the instance's `.env` (rejected: the whole point of the IAM role is that there is nothing to leak). Building in AWS from the start (rejected: every bug would cost a deploy cycle).
- **Result:** README Status carries the AWS block, `docs/ARCHITECTURE.md` splits local development from the AWS demo, and the storage interface is designed with two backends in mind from Day 2 on. Still open, to settle before the AWS phase: region and whether resources are created by script or by hand, whether the Streamlit UI also runs on the instance, and public demo port versus tunnel-only access.
