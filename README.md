# AI-Assisted Enterprise Implementation & Data Onboarding System

A customer implementation system for mapping, validating, and migrating enterprise data into a SaaS platform: source profiling, deterministic schema comparison, AI-assisted semantic mapping with human review, deterministic transformation and validation, dry-run migration through the target API, reconciliation, and an implementation readiness report.

The scenario: a B2B SaaS company signed a new enterprise customer. That customer's data lives in a legacy CRM export, a billing system behind a REST API, and a business-rules document nobody has read in a year. Before go-live, all of it has to land in the platform's schema, and somebody has to answer *can this customer safely migrate, what has to change first, and what still needs the customer's input?*

This tool is what an implementation or forward-deployed engineer would use to answer that.

> The customer (Apex Equipment Services), the target platform (Meridian), and every record are fictional and synthetic. This is a portfolio build, not a client engagement. Any business impact figure in the docs is labeled as a scenario estimate.

## Contents

- [Why this problem matters](#why-this-problem-matters)
- [Who this is for](#who-this-is-for)
- [What the system does](#what-the-system-does)
- [The one rule: where AI is and is not allowed](#the-one-rule-where-ai-is-and-is-not-allowed)
- [End-to-end workflow](#end-to-end-workflow)
- [Architecture](#architecture)
- [Engineering considerations](#engineering-considerations)
- [Evaluation and testing](#evaluation-and-testing)
- [Tech stack](#tech-stack)
- [Quick start](#quick-start)
- [Project layout](#project-layout)
- [Deployment](#deployment)
- [Current limitations](#current-limitations)
- [Future work](#future-work)
- [Documentation](#documentation)

## Why this problem matters

Enterprise onboarding looks simple from the outside: receive the customer's data, map it into the new platform, import it, go live. The risky part is everything between "receive the data" and "go live."

Customer data arrives in different formats, uses different names for the same concept, contains duplicate and malformed records, omits required fields, and conflicts with the target system's rules. A migration can report success and still load wrong data that surfaces weeks later as billing errors, broken account relationships, and support tickets.

Common failure modes this system is built to catch before go-live:

- duplicate organizations and contacts under different identifiers
- required fields that are blank or hold placeholders (`n/a`, `unknown`)
- dates in six formats, numbers stored as text with currency symbols, booleans spelled five ways
- source fields that do not match any target field by name
- fields that match a target by name but mean something else (`status` on which record?)
- child records that point at parent records that do not exist
- imports that "succeed" while source and destination counts do not reconcile

A schema mismatch found during profiling takes minutes to fix. The same mismatch found after production migration pulls in Support, Engineering, Implementation, and the customer to work out what changed, which records were affected, and whether the migrated data can still be trusted.

> Find uncertainty and bad data before go-live, make the migration explainable, and keep an audit trail of what happened and why.

## Who this is for

The workflow is modeled around the people involved in a real enterprise implementation:

- **Implementation Engineers / Forward Deployed Engineers** configure the migration, investigate edge cases, and own technical delivery.
- **Customer Engineers / Solutions Engineers** turn customer requirements into a working implementation.
- **Customer technical stakeholders** supply the source data, explain customer-specific fields, and resolve ambiguous mappings.
- **Support Engineers** deal with the fallout when incorrect or incomplete data reaches production.
- **End users** depend on migrated records being right so their accounts and subscriptions behave.

Onboarding is treated as a cross-functional system, not a one-time import script.

## What the system does

The system turns raw customer data into a migration decision:

```text
customer sources (csv, json, legacy billing api)
        |
        v
  source profiler ------------------------------> pandas, deterministic
        |
        v
  schema comparison ----------------------------> deterministic classification
        |
        v
  ai mapping service ---------------------------> provider interface (anthropic | openai | none)
        |                                         structured output validated by pydantic
        v
  human review + customer clarification --------> stored decisions, nothing auto-approved below threshold
        |
        v
  transformation engine ------------------------> explicit rules, no generated code executed
        |
        v
  validation engine ----------------------------> target pydantic models + enums + fk + business rules
        |
        v
  migration runner (dry run) -------------------> httpx against the target api, retries, failure capture
        |
        v
  target platform api + postgres "target" schema
        |
        v
  reconciliation -------------------------------> source vs transformed vs accepted, deterministic
        |
        v
  readiness report -----------------------------> READY / READY WITH CONDITIONS / BLOCKED
```

The result is not "import succeeded." It is evidence that lets an implementation engineer decide whether the customer is `READY`, `READY WITH CONDITIONS`, or `BLOCKED`, and a list of what has to change or be answered before that changes.

Three ways in:

- **Workbench UI** (Streamlit): create a project, attach sources, profile them, read the quality issues and the schema comparison. Talks to the API over HTTP only.
- **REST API** (FastAPI): every operation the UI performs, plus the simulated billing source and, in the same process, the simulated target platform.
- **CLI** (Typer): `check`, `generate-data`, `profile [--compare]`. Profiles a file or feed without the API or the database.

## The one rule: where AI is and is not allowed

Use the model where semantic reasoning adds something. Use deterministic code everywhere deterministic code is more reliable.

**The model is allowed to:** propose which source field means which target field and say why; interpret the customer's business-rules prose; suggest transformations (enum maps, normalizations, composites); write the clarification questions sent to the customer; explain migration blockers in plain language; draft the executive summary of the readiness report from numbers computed elsewhere.

**The model is never allowed to:** count rows, do arithmetic, detect duplicates, validate against the schema, execute generated code, compute reconciliation numbers, decide that an invalid record is valid, write to the target platform, or approve a low-confidence mapping on its own.

Concretely: does `customer_tier` mean `account_priority` or `subscription.plan`? That is a semantic question and the model may propose an answer with a confidence and a reason. Are 10,000 records still 10,000 records after transformation? That is arithmetic and the model is not consulted.

The transformation engine never executes model-generated Python. A model suggestion is a proposal a person reviews, not migration truth.

## End-to-end workflow

Profiling and schema comparison are in the codebase today. The stages after them are specified here as designed and listed under [Current limitations](#current-limitations).

### 1. Data ingestion and profiling

**Sources.** A project attaches the customer's sources by location: a csv or json file under one of the configured source roots (`sample_customer/data`, `generated`), or the url of a feed. File paths are checked against the roots before the filesystem is touched; absolute paths and `..` are refused with a 422. The customer's billing system is a paginated REST API with a bearer token (`page`, `page_size`, `has_more`), and the loader pages through it with httpx exactly as it would against the real system. A wrong token is a 401; a feed failure surfaces as a 502 `source_unavailable` and leaves the project's stage alone.

**Raw values, not pandas dtypes.** csv cells stay the strings that were in the file. pandas is told not to guess types, so zip codes never become floats, `unknown` never becomes NaN, and `"60"` and `60` stay distinguishable. json and api records keep their json types, so a column that is an integer in 70% of records and a string in the rest is reported as exactly that.

**Per cell, then per column.** Every non-blank cell gets a tag: integer, decimal, numeric text with symbols, date with the format that parsed it, datetime, email or malformed email, url with or without a scheme, phone or short phone, identifier, code, text. Placeholders (`n/a`, `unknown`, `-`) count as missing and are counted separately. The column's type is the dominant tag with a few rules on top (a digits-only column named like an id is an identifier, not a quantity; `0`/`1` is boolean only when the name says so). Whatever does not fit the type is counted as malformed, with examples.

Per column: null and placeholder counts, distinct count and uniqueness, sample values, value shapes (`AA-999999`), the full value distribution when there are at most 50 distinct values, case variants, stray whitespace, and per-type extras (date formats and ISO share, numeric text and `x.0` counts, boolean spellings, E.164 share, normalized duplicates for emails and names). Per dataset: exact duplicate rows, the key column (a name that says id, ref or num with nearly unique values), missing and duplicated keys.

**Across files.** Any column carrying the same name as another dataset's key column is checked against it: how many values resolve, how many are orphans, with examples. On the sample this finds the 50 injected dangling account numbers in `contacts.csv` and, on top of them, the 25 contacts of the 13 organizations whose own account number is blank. That second group is exactly what an implementation engineer needs to know and what a looser check would hide.

**Issues with severities.** The profile becomes a list a person can act on. **error**: rows will be rejected or cannot be migrated until someone decides (missing or duplicated keys, orphan references, malformed emails or ids). **warning**: a rule or a decision is needed before the dry run (mixed date formats, numeric text, placeholders, high null rates, near-duplicates, inconsistent json types, future dates). **info**: normalization handles it (case variants, whitespace, boolean spellings, urls without a scheme, upper-case emails).

**Measured on the committed sample** (1,030 organizations, 9,259 rows across four sources): profiling runs in about 2 seconds inside the container, billing feed included. The 10,000-organization customer (92,751 rows) takes about 18 seconds.

**Synthetic customer.** A reproducible generator writes the customer's exports with realistic defects and records how many of each it injected in a `manifest.json` next to the data: 45 defect kinds on the committed sample, including duplicate organizations under new and reused ids, missing and dangling account numbers, malformed and upper-case emails, dates in six formats, `CA` meaning both Canada and California, `Legacy Gold` and `Pro` where the target wants `enterprise` and `professional`, seat counts as text, quarterly billing the target does not support, and rule conflicts such as Strategic accounts on Starter plans. The repo ships a 1,000-organization sample (seed 42) so it works out of the box; `make seed` or the CLI generates 10,000 or any size, byte-identical for a given seed.

### 2. Schema comparison

Deterministic, and honest about what it cannot know. It sees three things: the column name, the profiler's stats, and the target catalog.

**Names.** Lower-cased, split on `_` and camelCase, singularized, passed through a small synonym table (`acct` to `account`, `mail` to `email`, `dt` to `date`, `freq` to `cycle`, `st` to `region`). The entity a file is about is read from its name (`organizations.csv`, `subscriptions (billing api)`) and can be overridden per dataset. Inside that entity the entity's own prefix is set aside on both sides, so `sub_status` and `subscription.status` compare as `status` against `status`. The score is a weighted token overlap; generic words (`id`, `name`, `date`, `count`, `last`) weigh half. It is a name similarity, not a confidence, and the workbench labels it that way.

**Five classes.**

| class | meaning |
|---|---|
| `MATCHED` | one candidate in the entity scores at least 0.7 with no close rival, and the values fit as they are |
| `TRANSFORMATION_REQUIRED` | the same match, but the profile says a rule is needed: date formats, currency text, enum values outside the target list, boolean spellings, E.164, urls without a scheme, json type casts, placeholders |
| `AMBIGUOUS` | a candidate scores at least 0.4 in the entity or 0.6 in another entity, or two candidates tie, or the entity is unknown and the name fits two of them; a person confirms |
| `UNMAPPED` | no name token helps; needs semantic mapping |
| `INCOMPATIBLE` | the match is clear but no rule gets there: free text into an integer, 80 distinct values into a four-value enum, an empty column |

**Required-field coverage.** Per target entity: which required fields are covered by a `MATCHED` or `TRANSFORMATION_REQUIRED` source field, which only have an `AMBIGUOUS` candidate, and which have nothing. On the sample this is exactly the mapping stage's to-do list: `organization_id` in every entity (someone has to say that `acct_num` is the account), `contact.first_name` and `last_name` (a split of `full_name`), `subscription.plan` (`subscription_level`) and `subscription.mrr_usd` (`monthly_amount`).

**What it refuses to do.** Guess that `acct_num` is `organization_id`, that `customer_tier` is `account_priority`, or that `created_on` is `customer_since`. Those are semantic calls. By the one rule they belong to the model's proposal and a person's approval, and this deterministic result is the context that proposal is made against. A comparison that already guessed would also poison the mapping eval: it would measure the heuristic, not the model.

**Measured on the sample:** 5 `MATCHED`, 18 `TRANSFORMATION_REQUIRED`, 2 `AMBIGUOUS`, 16 `UNMAPPED`, 0 `INCOMPATIBLE`. The comparison is computed on request from the stored profile and never persisted: it is a pure function of the profile and the catalog, so storing it would only create a stale copy.

### 3. Schema mapping: AI-assisted, human-approved

This is where the model earns its place. For each source field it sees the field name, the profiler's stats, a small sanitized set of sample values, the target catalog with types, enums, required flags and descriptions, the deterministic comparison result, and the customer's business-rules document. It never sees the dataset.

It proposes, per field, a structured mapping validated by Pydantic before anything is stored:

```json
{
  "source_field": "customer_tier",
  "target_field": null,
  "confidence": 0.41,
  "reason": "Gold/Silver/Bronze read as a spend band, but Strategic is an executive designation per rule 3. The target has both account_priority and subscription.plan; the rules doc says it is neither exactly.",
  "transformation_required": true,
  "clarification_required": true
}
```

The provider sits behind one interface. Anthropic is the default, OpenAI runs behind the same interface, and `LLM_PROVIDER=none` is **manual mode**: every other stage works, only the suggestions are off and the engineer maps by hand from the comparison. A provider without its key falls back to manual mode; the application never fakes a model response.

**Human review.** Every mapping carries a status and a decision trail. The engineer can approve, reject, edit the target field, define the transformation, mark the field ignored, or send a question to the customer. High-confidence mappings can be bulk-approved; nothing below the threshold is approved without a person. Low-confidence rows are visually obvious in the workbench.

**Customer clarification.** Unresolved mappings become targeted questions, stored with the project, answered in the tool, and fed back into the mapping: "We found `customer_tier` with values Gold, Silver, Bronze, Strategic and Platinum. Meridian has `account_priority` (operational treatment) and `subscription.plan` (what they pay for). Which does this field represent, and how should Strategic and the retired Platinum be handled?"

**Cost and safety design.** The prompt carries schema, stats and a handful of sanitized samples, never rows. A hash of the prompt inputs skips a second call for the same schema. Malformed structured output is retried a bounded number of times, then surfaces as an error instead of a guess. Model, latency and token usage are logged per call; keys and raw customer records are not.

### 4. Deterministic transformation and validation

**Transformation.** Approved mappings drive explicit rules: rename, parse dates in the formats the profiler saw, normalize booleans and phone numbers to E.164, map enums through a table (`Legacy Gold` to `enterprise` unless seats < 10, per rule 7), trim whitespace, split `full_name`, combine fields, apply defaults, and build the target API payload. Rules are application logic and configuration. No generated code is executed. That makes the layer testable, repeatable, reviewable, and safe to run more than once.

**Validation.** Before any dry run, every transformed record is checked against the target's Pydantic models (types, required fields, enums, patterns such as ISO country codes and E.164 phones), then against the cross-record rules: the parent organization exists, ids are unique per entity, at most one primary contact per organization, email unique within an organization, no active subscription on a churned organization, plus the customer's own rules (dormant accounts become inactive, trials older than 90 days are not migrated).

Validation returns reasons, not a boolean:

```json
{
  "record_id": "SUB-00415",
  "status": "rejected",
  "reasons": [
    "seats is required and is null",
    "status 'trialing' is not one of trial, active, past_due, cancelled",
    "renewal_date 2018-11-17 is not after start_date 2017-11-17"
  ]
}
```

### 5. Migration workflow: dry run and reconciliation

**Dry run.** Validated records are sent to the target platform's REST API (`POST /target/v1/organizations`, `/contacts`, `/subscriptions`), parents before children, with the customer's stable legacy ids as target ids so a re-run is idempotent: same id with the same content is a no-op, same id with different content is a 409. The runner records per record whether the target accepted or rejected it and why, and separates transport failures (timeouts, 500s, malformed bodies) from data failures. The mock target can inject failures on request so the error handling is exercised, not assumed. The source data is never modified.

**Reconciliation.** Counts across every stage, computed by code:

```text
source records
      |
      v
transformed records
      |
      v
attempted records
      |
      +----> accepted by target
      |
      +----> rejected by target
      |
      v
target records (GET /target/v1/stats)
```

An unexplained difference between any two stages is a finding, not a footnote. A successful HTTP status is not evidence that a migration is correct.

### 6. Reporting

The readiness report answers one question: do we have enough evidence to proceed with production onboarding?

It contains the mapping summary (approved, edited, rejected, unresolved, awaiting the customer), the data quality summary (duplicates, missing required fields, malformed values by column), validation results (passed, failed, warnings, top failure reasons), the dry-run and reconciliation numbers, the blockers, the open customer questions, next steps, and technical risks. The state is one of `READY`, `READY WITH CONDITIONS`, or `BLOCKED`, decided by rules over those numbers. The model may draft the plain-English executive summary from the computed numbers; it does not compute or change them.

Exported as Markdown for people and JSON for automation.

## Architecture

### Components

- **API service:** one FastAPI process serving the onboarding API under `/api/v1`, the simulated legacy billing source under `/mock/billing/v1`, and the simulated target platform under `/target/v1`. Mounting these in one process keeps the reviewer experience to one `docker compose up`; the loaders and the migration layer still talk to them over HTTP, so the network seam (serialization, status codes, timeouts, retries) is real. The base URLs are configuration, so either mock can be split into its own service without code changes.
- **Source profiler:** `app/services/profiling`. Loaders, value-level type inference, per-column stats, dataset quality issues, cross-dataset key checks.
- **Schema comparison:** `app/services/comparison.py`. Deterministic classification of every source field against the target catalog.
- **Target catalog:** `app/target`. Meridian's data model as Pydantic models; the catalog flattens them into the field list the comparison classifies against and the mapping prompt sees. The JSON schema files under `target_platform/schema` are generated from these models, and a test fails if they drift.
- **Database:** one PostgreSQL with two schemas. `onboarding` holds this tool's state. `target` holds the fictional platform's tables and is only ever written through the target API, so the migration cannot bypass the interface.
- **Workbench UI:** Streamlit, talks to the API over HTTP only. `ui/streamlit_app.py` holds navigation and the project selector, `ui/views/` one module per screen.
- **CLI:** Typer. `profile` runs the profiler and, with `--compare`, the schema comparison on one file or feed without the API or the database.
- **Logging:** structlog, JSON in containers and console locally, request id on every line, context fields (project, dataset, stage, counts, durations) bound per pipeline step.

### Database

Schema `onboarding`:

| table | what it holds |
|---|---|
| `projects` | one row per customer onboarding: customer, project, source systems, target environment, notes, `stage` |
| `source_datasets` | one row per source file or feed attached to a project (csv, json, api): where it lives, row and column counts, `profiled_at`, and `quality` (jsonb: duplicate rows, key column with missing and duplicated counts, references into other datasets with orphan counts, the issue list with severities) |
| `source_fields` | one row per column per profiled dataset: inferred type, null and unique percentages, distinct count, up to five sample values, and `stats` (jsonb: type tag counts, value distribution or a 50-value sample, shapes, case variants, per-type extras such as date formats or malformed examples). Replaced on every re-profile |

`stage` is a plain varchar validated by a Python enum rather than a native PostgreSQL enum, so the stage list can change without a migration. Tables are created from the SQLAlchemy metadata at startup; Alembic takes over once the schema stops moving.

### API

| method | path | what it does |
|---|---|---|
| GET | `/health` | liveness, database reachability, active LLM provider |
| POST / GET | `/api/v1/projects` | open an onboarding project; list projects newest first |
| GET | `/api/v1/projects/{id}` | project with its attached sources |
| GET | `/api/v1/sources/available` | files under the source roots and known feeds, ready to attach |
| GET / POST | `/api/v1/projects/{id}/sources` | list or attach a source (404 unknown file, 409 duplicate name, 422 outside the roots) |
| GET / DELETE | `/api/v1/projects/{id}/sources/{dataset_id}` | one source with its per-column profile; detach |
| POST | `/api/v1/projects/{id}/sources/profile` | load and profile every attached source, check keys across them (502 if a feed is down) |
| GET | `/api/v1/projects/{id}/schema-comparison` | classify every source field against the target catalog |
| GET | `/mock/billing/v1/subscriptions` | the customer's LegacyBill 4.2 api: bearer token, `page`, `page_size`, `has_more` |

Every non-2xx response has one shape, including the framework's own 404/405/422:

```json
{"error": {"type": "source_unavailable", "message": "billing feed returned 503", "details": {"url": "..."}}}
```

Interactive docs at `/docs`. The target platform's own API (`/target/v1`) is specified in [target_platform/documentation](target_platform/documentation/README.md).

### Where the data lives

Two deployments of the same code. Local development is the build and review environment and never depends on AWS. The AWS demo extends it with object storage and a hosted instance behind a storage switch. See [Deployment](#deployment) and [Local development vs AWS demo](docs/ARCHITECTURE.md#local-development-vs-aws-demo).

## Engineering considerations

Each risk below, the response, and the mechanism in this codebase. Where the mechanism belongs to a stage that is not in the codebase yet, it is described as the design the stage is built against.

**Incorrect semantic mappings.** Two fields look alike and mean different things. Every mapping carries a confidence, a reason, and a clarification flag; below the threshold nothing is approved without a person. The deterministic comparison runs first and says `AMBIGUOUS` or `UNMAPPED` out loud instead of guessing, so the model's proposal is judged against an honest baseline.

**Model overconfidence.** A confident wrong mapping. Model output is a proposal validated by Pydantic, stored with its reasoning, and reviewed. Downstream, the transformed data is validated deterministically regardless of what the model said. The mapping eval counts incorrect high-confidence suggestions as its own metric.

**Bad source data.** The profiler measures it before anything is transformed, from raw values, with counts of cells that failed a stated rule and examples. Source problems are reported separately from transformation or target failures, so the customer's data quality and the tool's behavior are never conflated.

**Partial migration.** Records silently lost between stages. Reconciliation counts source, transformed, attempted, accepted, rejected and target, and any unexplained difference is a finding.

**Retry safety.** Target ids are the customer's stable legacy identifiers, and the target API treats same id plus same content as a no-op and same id plus different content as a conflict. Re-running a dry run cannot create duplicates.

**Target API failure and rate limits.** Transport failures are classified apart from data failures. The migration runner retries with backoff on transient errors, captures every failure with its response, and the mock target can inject 500s, slow responses and malformed bodies so that path is tested.

**Sensitive customer data.** The application handles the customer's data in real life, so the defaults are conservative. Every published port is bound to `127.0.0.1`. Stored samples are capped at five values of at most 60 characters per column; the mapping prompt gets a further sanitized subset; raw rows never leave the database. LLM keys are read from the environment only. No `.env*` file of any kind is ever committed, templates included: `.gitignore` carries `.env*` with no exceptions, the template lives at `deploy/env.template`, and a committed pre-commit hook (`make hooks`) refuses env files and runs gitleaks on the staged diff.

**Auditability.** The question is not only "did this record migrate" but "how did we decide what this record should become." Mapping decisions, their reasoning, approval state, clarification answers, validation results and reconciliation output are stored with the project. Profiles are replaced on re-profile; decisions are kept.

**Schema drift.** The target catalog is generated from the Pydantic models and a test fails if the committed schema files diverge. The schema comparison is recomputed on request, never cached, so a changed export or a changed target shows up the next time someone looks.

**Observability.** structlog with JSON output in containers, a request id on every line and in the `x-request-id` response header, and per-stage context (project id, dataset, stage, record counts, duration in ms, error type). Someone diagnosing a failed onboarding filters by project id and stage instead of grepping free text.

**Failure philosophy.** The system fails loudly when correctness cannot be established. An unresolved required field does not silently become null. A record that violates the target schema is not counted as migrated. An ambiguous business concept is not mapped because the model produced an answer. A count mismatch is not hidden by a 200. Uncertainty is information, and the system exposes it.

## Evaluation and testing

**Test suites.** pytest, 103 tests across unit and integration. Unit tests need nothing; integration tests run against the compose PostgreSQL in their own database (`onboarding_test`, created by `deploy/postgres-init`) and skip cleanly when it is not up.

- The profiler is pinned to the synthetic generator's own defect manifest. Where a defect maps one to one onto a measurement (missing ids, duplicated ids, missing seat counts) the counts must agree exactly; where duplication copies defective rows the profiler must find at least as many.
- The schema comparison is judged on the committed sample: it must catch the renames the synonym table covers, refuse to guess at the semantic ones, and read the profiler's stats into the right compatibility notes.
- The billing feed's 401, its pagination arithmetic, and the "feed is down" path (502, stage untouched) are exercised through the API, not assumed.
- Path traversal and out-of-root attachments are rejected with 422 in tests.
- The target schema files are regenerated from the models in a test; drift fails the build.

**Workbench verification.** Every screen is checked in a real headless browser (Chromium via Playwright) before it is committed: page errors, console errors, the content it is supposed to show, and screenshots reviewed by eye. This is a review step, not part of the pytest suite. It exists because jsdom-style assertions passed three layout defects that a real browser caught on the first screen.

**Mapping evaluation.** A golden set of source fields with known correct targets (`evals/`), including fields whose correct answer is "requires clarification." The eval reports top-1 mapping accuracy, unresolved rate, confidence distribution, and the count of incorrect high-confidence mappings. Metrics shown anywhere in this repo come from actual eval runs; none are invented.

**System metrics vs business claims.** Profiling and comparison timings, counts, and eval scores are measured and reported as such. Business impact (implementation hours saved, avoidable errors) is not measured against real production usage and is labeled as an estimate wherever it appears.

## Tech stack

| layer | technology | purpose |
|---|---|---|
| application | Python 3.12, uv | core onboarding and migration logic; reproducible dependencies |
| api | FastAPI | the onboarding API, the simulated billing source, the simulated target platform |
| data processing | pandas | load and profile tabular customer data from raw values |
| contracts and validation | Pydantic v2 | request and response models, the target platform's data model, structured model output |
| persistence | PostgreSQL 16, SQLAlchemy 2 | implementation state, profiles, mappings, results, audit data |
| http | httpx | the billing feed loader and the migration runner |
| ai | provider interface: Anthropic default, OpenAI, or none | semantic mapping suggestions; configured by environment, no provider or model name hardcoded |
| ui | Streamlit | the implementation workbench |
| cli | Typer | backend operations from the terminal |
| logging | structlog | JSON logs with request ids and stage context |
| testing | pytest | unit, integration, end to end; the workbench is also checked in a real browser before commit |
| packaging | Docker, Docker Compose | one command brings up PostgreSQL, the API and the workbench |
| cloud | AWS S3, EC2, IAM | object storage for intake and outputs, a hosted demo instance, role-based access |

The cloud layer is secondary to the system design. The project is understandable and runnable as an implementation workflow without AWS.

## Quick start

```bash
cp deploy/env.template .env
docker compose up --build
```

- API: http://localhost:8000
- API docs: http://localhost:8000/docs
- Workbench UI: http://localhost:8501
- The customer's billing system, simulated: http://localhost:8000/mock/billing/v1/subscriptions (bearer token from `.env`)
- PostgreSQL: internal to compose, also on 127.0.0.1:5433 for local tools

First run: open the workbench, create a project, attach the four Apex sources on the Source assessment page and profile them. Or from the terminal, without the API or the database:

```bash
uv run enterprise-onboarding profile sample_customer/data/organizations.csv --compare
```

Generate a bigger customer than the committed sample (10,000 organizations, about 25,000 contacts):

```bash
make seed        # inside the api container, lands in the `generated` volume
# or locally
uv run enterprise-onboarding generate-data --rows 10000 --out generated/apex
```

Local development without containers (needs [uv](https://docs.astral.sh/uv/) and the compose postgres running):

```bash
uv sync
make hooks       # one-time: pre-commit hook that blocks .env files and runs gitleaks
uv run uvicorn app.main:app --reload
uv run streamlit run ui/streamlit_app.py
uv run pytest
```

Configuration is environment variables with working defaults for everything except the LLM keys; see `deploy/env.template`. `LLM_PROVIDER` is `anthropic`, `openai` or `none`. With `none` the tool runs in manual mode.

## Project layout

```
app/            the service: api, core (config, db, logging, errors, http), models, schemas, services, target
ui/             streamlit workbench, talks to the api over http
scripts/        synthetic customer data generator, target schema export
sample_customer/  the fictional customer's exports, business rules, kickoff notes
target_platform/  the fictional saas platform's schema and documentation
tests/          unit, integration, e2e
evals/          golden mapping set for measuring the ai mapping step
deploy/         env template, postgres init
docs/           architecture, build log
```

## Deployment

**Local (Docker Compose).** Three services: PostgreSQL 16, the FastAPI service, the Streamlit workbench. Every published port is bound to `127.0.0.1`. PostgreSQL is published on 5433 so a database already running on 5432 is left alone. The committed sample is mounted read-only; generated customers land in a named volume. This is the build and review environment and stays fully functional with no AWS account.

**AWS demo (S3 + EC2 + IAM).** The same three services on one small Linux EC2 instance, with a private S3 bucket for intake datasets and generated outputs:

```text
customer dataset
   |
   v
S3 bucket (private)                 intake/{customer}/{project}/...
   |
   v
EC2, small linux instance           docker compose, the same three services
   instance profile = IAM role       s3 get/put/list on this bucket and prefix only
   security group                    8000 and 8501 from the operator's ip, for the demo only
   postgres                          inside compose, bound to 127.0.0.1 on the instance, never public
   |
   v
S3 bucket                           output/{customer}/{project}/readiness.md, readiness.json, run logs
```

Rules the AWS deployment follows:

- **Scope is S3 + EC2 + IAM.** No RDS, no load balancer, no container service. PostgreSQL in Docker on the instance is enough for this project; the point is the onboarding workflow, not the infrastructure.
- **No long-lived keys anywhere.** The instance gets its permissions from an IAM role via the instance profile and boto3 picks them up on its own. Nothing AWS-related is committed, and the operator's own credentials stay on the operator's machine.
- **Least privilege, explained.** The IAM policy lists only the S3 actions the application calls, scoped to the one bucket, with the reason for each statement in the deployment doc.
- **Configuration, not code.** `STORAGE_BACKEND=local|s3`, `AWS_REGION`, `S3_BUCKET`, `S3_PREFIX` via environment. The S3 adapter implements the same storage interface as the local filesystem store, so local development never touches AWS.
- **Documented and disposable.** Instance configuration, security group, IAM policy, deployment steps, expected monthly cost, and how to stop or delete everything after the demo live in `docs/AWS_DEPLOYMENT.md`.

## Current limitations

- **Stages not yet in the codebase:** the AI mapping service and human review, customer clarification questions, the transformation engine, the validation engine, the target platform API, the dry-run migration runner, reconciliation, and the readiness report. Their design is specified above and in `docs/ARCHITECTURE.md`; the `evals/` golden set is empty until the mapping service exists.
- **AWS deployment is designed, not deployed.** The storage switch, S3 adapter and `docs/AWS_DEPLOYMENT.md` do not exist yet.
- **Schema management** is `create_all` at startup, not Alembic migrations.
- **Profiling is a per-cell Python loop.** About 18 seconds for 92,751 rows. Fine for the demo; vectorized tagging or sampling above a row threshold is the fix for larger customers.
- **Orchestration is plain Python** with the project's stage in PostgreSQL. LangGraph is evaluated against the working workflow, not before it exists.
- **Single-tenant, no authentication.** The tool is an internal implementation tool bound to localhost; production would need API authentication, role-based review, and customer isolation.

## Future work

Only after the end-to-end workflow above is solid:

- LangGraph, if the workflow's state and branching earn it, with the decision recorded either way
- RAG over the target platform's implementation docs to ground mapping suggestions
- a second customer dataset with a different shape
- batching and resumable runs for large datasets; vectorized profiling
- mapping-template reuse across similar customers
- historical comparison between dry runs
- cost and latency tracking per model call, model-provider fallback
- additional source connectors and target adapters
- automated rollback planning
- PDF export of the readiness report
- Alembic migrations once the schema settles

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, database, API, AI boundaries, local vs AWS
- [docs/BUILD_LOG.md](docs/BUILD_LOG.md): dated engineering decisions with the alternatives that lost
- [target_platform/documentation](target_platform/documentation/README.md): Meridian's entity model, required fields, enums, relationships, API and validation rules
- [sample_customer/business_rules.md](sample_customer/business_rules.md) and [implementation_notes.md](sample_customer/implementation_notes.md): the customer's rules and the kickoff notes the mapping step interprets

## Disclaimer

This is a portfolio and demonstration project built with synthetic data. The customer, the target platform, and every record are fictional. Migration results and performance figures are measured from the application on synthetic data; business-impact estimates are labeled as estimates and are not claims about a real customer migration.

## License

MIT
