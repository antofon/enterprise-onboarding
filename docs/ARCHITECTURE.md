# Architecture

Growing document. Sections are marked **implemented** or **planned** and get rewritten as the build moves.

## The one rule

Use the model where semantic reasoning adds something. Use deterministic code everywhere deterministic code is more reliable.

The model is allowed to: propose which source field means which target field and say why; interpret the customer's business-rules prose; suggest transformations (enum maps, normalizations, composites); write the clarification questions we send the customer; explain migration blockers in plain language; draft the executive summary of the readiness report from numbers computed elsewhere.

The model is never allowed to: count rows, do arithmetic, detect duplicates, validate against the schema, execute generated code, compute reconciliation numbers, decide that an invalid record is valid, write to the target platform, or approve a low-confidence mapping on its own.

## Components (planned unless marked)

```
customer sources (csv, json, legacy billing api)
        |
        v
  source profiler  ---------------------------> pandas, deterministic
        |
        v
  schema comparison ---------------------------> deterministic classification
        |
        v
  ai mapping service ------------------------> provider interface (anthropic | openai | none)
        |                                       structured output validated by pydantic
        v
  human review + customer clarification -------> stored decisions, nothing auto-approved below threshold
        |
        v
  transformation engine ------------------------> rule dsl, no generated code executed
        |
        v
  validation engine ---------------------------> target pydantic models + enums + fk + business rules
        |
        v
  migration runner (dry run) ------------------> httpx against the target api, retries, failure capture
        |
        v
  target platform api + postgres "target" schema
        |
        v
  reconciliation ------------------------------> source vs transformed vs accepted, deterministic
        |
        v
  readiness report ----------------------------> READY / READY WITH CONDITIONS / BLOCKED
```

- **api service (implemented, skeleton):** one FastAPI process serving the onboarding API under `/api/v1`, the mock target platform under `/target/v1`, and the mock legacy billing source under `/mock/billing/v1`. Mounting the target in the same process keeps the reviewer experience to one `docker compose up`; the migration layer still talks to it over HTTP so the seam is real.
- **database (implemented, empty):** one PostgreSQL with two schemas. `onboarding` holds this tool's state. `target` holds the fictional platform's tables and is only ever written through the target API.
- **workbench ui (implemented, placeholder):** Streamlit, talks to the API over HTTP only.
- **cli (implemented, `check` only):** Typer. Useful operations without the UI.
- **logging (implemented):** structlog, JSON in containers, request id on every line, context fields bound per stage.

## Data flow

Planned: written on Day 2 once profiling exists.

## Database

Schema `onboarding` (implemented so far):

| table | what it holds |
|---|---|
| `projects` | one row per customer onboarding: customer, project, source systems, target environment, notes, `stage` |
| `source_datasets` | one row per source file or feed attached to a project (csv, json, api), row/column counts, quality summary once profiled |
| `source_fields` | one row per column per dataset: inferred type, null and unique percentages, sample values, stats |

`stage` is a plain varchar validated by a Python enum rather than a native PostgreSQL enum, because the stage list moves during the build and native enums cannot be altered by `create_all`.

Coming with later milestones: `field_mappings`, `clarification_questions`, `migration_runs`, `validation_issues`, `reconciliation_results`, `readiness_reports`, `llm_calls`. Schema `target` (the fictional platform) gets `organizations`, `contacts`, `subscriptions` on Day 4.

## API

| method | path | status |
|---|---|---|
| GET | `/health` | implemented |
| POST | `/api/v1/projects` | implemented |
| GET | `/api/v1/projects` | implemented |
| GET | `/api/v1/projects/{id}` | implemented |
| POST | `/api/v1/projects/{id}/sources/profile` | Day 2 |
| POST | `/api/v1/projects/{id}/mappings/suggest` | Day 3 |
| GET / PATCH | `/api/v1/projects/{id}/mappings[/{mapping_id}]` | Day 3 |
| POST | `/api/v1/projects/{id}/validate` | Day 4 |
| POST | `/api/v1/projects/{id}/migrations/dry-run` | Day 4 |
| GET | `/api/v1/projects/{id}/migrations/{run_id}` | Day 4 |
| GET | `/api/v1/projects/{id}/reports/readiness` | Day 5 |
| POST / GET | `/target/v1/organizations`, `/contacts`, `/subscriptions` | Day 4 |

Every non-2xx response is `{"error": {"type", "message", "details"}}`, including the framework's own 404/405/422.

## AI boundaries

See "The one rule" above. Details (prompt inputs, schema hashing, caching, retries, usage logging) land on Day 3.

## Human review

Planned for Day 3.

## Failure recovery

Planned for Day 4.

## Local development vs AWS demo

Two deployments of the same code. Local is the build and review environment and stays fully functional with no AWS account. The AWS demo (planned, after the local workflow is verified end to end) extends it with object storage and a hosted instance. Nothing in the local path depends on AWS.

**Local development (implemented):**

```
laptop or vps
  docker compose
    db   postgres 16, published on 127.0.0.1:5433 only
    api  fastapi, mock target platform, mock billing source (Day 2), 127.0.0.1:8000
    ui   streamlit workbench, 127.0.0.1:8501
  files
    sample_customer/   the committed 1,000-org customer, mounted read-only
    generated/         named volume for bigger generated customers
  STORAGE_BACKEND=local   planned: artifacts (profiles, reports, run output) on the local filesystem
```

**AWS demo (planned):**

```
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

Rules the AWS phase follows:

- **Scope is S3 + EC2 + IAM.** No RDS, no load balancer, no container service. Postgres in Docker is enough for this project; the point is the onboarding workflow, not the infrastructure.
- **No long-lived keys anywhere.** The instance gets its permissions from an IAM role via the instance profile; boto3 picks them up on its own. Nothing AWS-related is ever committed, and the operator's own credentials stay on the operator's machine.
- **Least privilege, explained.** The IAM policy lists only the S3 actions the application calls, scoped to the one bucket, and the deployment doc says why each statement exists.
- **Configuration, not code.** `STORAGE_BACKEND=local|s3`, `AWS_REGION`, `S3_BUCKET`, `S3_PREFIX` via environment. The S3 adapter implements the same storage interface as the local filesystem store.
- **Documented and disposable.** `docs/AWS_DEPLOYMENT.md` (planned) covers instance configuration, security group, IAM policy, deployment steps, expected monthly cost, and how to stop or delete everything after the demo.

## Security considerations

Planned for Day 6: what is implemented versus what production would need.

## Scaling considerations

Planned for Day 6.
