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

- **api service (implemented):** one FastAPI process serving the onboarding API under `/api/v1`, the mock target platform under `/target/v1` (Day 4), and the mock legacy billing source under `/mock/billing/v1`. Mounting these in the same process keeps the reviewer experience to one `docker compose up`; the loaders and the migration layer still talk to them over HTTP so the seam is real.
- **mock billing source (implemented):** `/mock/billing/v1/subscriptions`, the customer's LegacyBill 4.2 api: bearer token from `BILLING_API_TOKEN`, `page` and `page_size`, `has_more`, records served from the same `subscriptions.json` the generator writes. The profiler has to authenticate and page like it would against the real thing. A wrong token is a 401 in the standard envelope; a feed failure surfaces as a 502 `source_unavailable` on the profile call and leaves the project's stage alone.
- **source profiler (implemented):** `app/services/profiling`. Loaders, value-level type inference, per-column stats, dataset quality issues, cross-dataset key checks. Details under "Source profiling".
- **schema comparison (implemented):** `app/services/comparison.py`. Deterministic classification of every source field against the target catalog. Details under "Schema comparison".
- **database (implemented, empty):** one PostgreSQL with two schemas. `onboarding` holds this tool's state. `target` holds the fictional platform's tables and is only ever written through the target API.
- **workbench ui (implemented: overview, source assessment):** Streamlit, talks to the API over HTTP only. `ui/streamlit_app.py` holds the navigation and the project selector, `ui/views/` one module per screen.
- **cli (implemented: `check`, `generate-data`, `profile`):** Typer. `profile` runs the profiler and, with `--compare`, the schema comparison on one file or feed without the API or the database.
- **logging (implemented):** structlog, JSON in containers, request id on every line, context fields bound per stage.

## Data flow

Implemented through schema comparison. The rest is planned and marked.

1. **Attach.** `POST /api/v1/projects/{id}/sources` records where each source lives: a csv or json file under one of the configured source roots (`sample_customer/data`, `generated`) or the url of a feed. File paths are checked against the roots before the filesystem is touched; absolute paths and `..` are refused with a 422. Nothing is read yet. `GET /api/v1/sources/available` lists what can be attached so the workbench offers a pick list instead of a text box.
2. **Load.** `POST /api/v1/projects/{id}/sources/profile` loads every attached source into a dataframe of raw values. csv cells stay the strings that were in the file: pandas is told not to guess types, so zip codes never become floats and blanks stay blank. json and api records keep their json types, so a column that is an integer in 70% of records and a string in the rest is reported as exactly that. The billing feed is paged over http with the bearer token, page size and `has_more` the legacy system uses. The http client is a FastAPI dependency, which is how the tests hand the app its own test client and the loader still speaks http.
3. **Profile.** Every non-blank cell gets a tag: integer, decimal, numeric text with symbols, date with the format that parsed it, datetime, email or malformed email, url with or without a scheme, phone or short phone, identifier, code, text. Placeholders (`n/a`, `unknown`, `-`) count as missing and are counted separately. The column's type is the dominant tag, with a few rules on top (a digits-only column named like an id is an identifier, not a quantity; `0`/`1` is boolean only when the name says so). Whatever does not fit the type is counted as malformed, with examples. Per column: null and placeholder counts, distinct count and uniqueness, sample values, value shapes (`AA-999999`), the full value distribution when there are at most 50 distinct values, case variants, stray whitespace, and per-type extras (date formats and ISO share, numeric text and `x.0` counts, boolean spellings, E.164 share, normalized duplicates for emails and names). Per dataset: exact duplicate rows, the key column (a name that says id, ref or num with nearly unique values), missing and duplicated keys.
4. **Cross-check.** Once every source is loaded, any column that carries the same name as another dataset's key column is checked against it: how many values resolve, how many are orphans, with examples. On the sample this finds the injected dangling account numbers and, on top of them, every contact, subscription and activity of the organizations whose own account number is blank.
5. **Assess.** The profile becomes a list of issues with a severity. **error**: rows will be rejected or cannot be migrated until someone decides (missing or duplicated keys, orphan references, malformed emails or ids). **warning**: a rule or a decision is needed before the dry run (mixed date formats, numeric text, placeholders, high null rates, near-duplicates, inconsistent json types, future dates). **info**: normalization handles it (case variants, whitespace, boolean spellings, urls without a scheme, upper-case emails).
6. **Store.** One `source_fields` row per column and the dataset-level summary (counts, key facts, references, issues) in `source_datasets.quality`. Re-profiling replaces both. The project moves to `profiled`.
7. **Compare.** `GET /api/v1/projects/{id}/schema-comparison` rebuilds the profiles from the database and classifies every source field against the target catalog. Computed on request, never stored: it is a pure function of the profile and the catalog. The mapping stage is where decisions get persisted.
8. **Map, review, transform, validate, dry-run, reconcile, report.** Planned, Days 3 to 5.

## Source profiling

The profiler never trusts a dtype. It reads raw values and counts. That choice is what makes the numbers in the workbench and in this documentation defensible: every "4.7% of emails are malformed" is a count of cells that failed a stated rule, with examples, not a statistic pandas produced on the way in. The unit tests pin the profiler to the synthetic generator's own defect manifest: where a defect maps one to one onto a measurement (missing ids, duplicated ids, missing seat counts) the counts must agree exactly; where duplication copies defective rows the profiler must find at least as many.

Samples stored per column are capped at five values of at most 60 characters. What the mapping prompt gets to see on Day 3 is a further sanitized subset; raw rows never leave the database.

## Schema comparison

Deterministic, and honest about what it cannot know. It sees three things: the column name, the profiler's stats, and the target catalog.

- **Names.** Lower-cased, split on `_` and camelCase, singularized, passed through a small synonym table (`acct` to `account`, `mail` to `email`, `dt` to `date`, `freq` to `cycle`, `type` to `kind`, `st` to `region`, ...). The entity a file is about is read from its name (`organizations.csv`, `subscriptions (billing api)`) and can be overridden per dataset. Inside that entity the entity's own prefix is set aside on both sides, so `sub_status` and `subscription.status` compare as `status` against `status`. The score is the average of weighted jaccard and containment over the token sets; generic words (`id`, `name`, `date`, `count`, `last`, ...) weigh half and never earn the containment bonus on their own. It is a name similarity, not a confidence, and the workbench labels it that way.
- **Classes.** `MATCHED`: one candidate in the entity scores at least 0.7 with no close rival, and the values fit as they are. `TRANSFORMATION_REQUIRED`: the same match, but the profile says a rule is needed (date formats, currency text, enum values outside the target list, boolean spellings, E.164, urls without a scheme, json type casts, placeholders). `AMBIGUOUS`: a candidate scores at least 0.4 in the entity or 0.6 in another entity, or two candidates tie, or the entity is unknown and the name fits two of them; a person confirms. `UNMAPPED`: no name token helps. `INCOMPATIBLE`: the match is clear but no rule we have gets there (free text into an integer, 80 distinct values into a four-value enum, an empty column).
- **Coverage.** Per entity: which required target fields are covered by a `MATCHED` or `TRANSFORMATION_REQUIRED` source field, which only have an `AMBIGUOUS` candidate, and which have nothing. On the sample this is exactly the mapping stage's to-do list: `organization_id` in every entity (someone has to say that `acct_num` is the account), `contact.first_name` and `last_name` (a split of `full_name`), `subscription.plan` (`subscription_level`) and `subscription.mrr_usd` (`monthly_amount`).
- **What it refuses to do.** Guess that `acct_num` is `organization_id`, that `customer_tier` is `account_priority`, or that `created_on` is `customer_since`. Those are semantic calls. By the one rule above they belong to the model's proposal and a person's approval, and the deterministic result is the context that proposal is made against.

## Database

Schema `onboarding` (implemented so far):

| table | what it holds |
|---|---|
| `projects` | one row per customer onboarding: customer, project, source systems, target environment, notes, `stage` |
| `source_datasets` | one row per source file or feed attached to a project (csv, json, api): where it lives, row and column counts, `profiled_at`, and `quality` (jsonb: duplicate rows, key column with missing and duplicated counts, references into other datasets with orphan counts, the issue list with severities) |
| `source_fields` | one row per column per profiled dataset: inferred type, null and unique percentages, distinct count, up to five sample values, and `stats` (jsonb: type tag counts, value distribution or a 50-value sample, shapes, case variants, per-type extras such as date formats or malformed examples). Replaced on every re-profile |

`stage` is a plain varchar validated by a Python enum rather than a native PostgreSQL enum, because the stage list moves during the build and native enums cannot be altered by `create_all`.

Coming with later milestones: `field_mappings`, `clarification_questions`, `migration_runs`, `validation_issues`, `reconciliation_results`, `readiness_reports`, `llm_calls`. Schema `target` (the fictional platform) gets `organizations`, `contacts`, `subscriptions` on Day 4.

## API

| method | path | status |
|---|---|---|
| GET | `/health` | implemented |
| POST | `/api/v1/projects` | implemented |
| GET | `/api/v1/projects` | implemented |
| GET | `/api/v1/projects/{id}` | implemented |
| GET | `/api/v1/sources/available` | implemented |
| GET / POST | `/api/v1/projects/{id}/sources` | implemented |
| GET / DELETE | `/api/v1/projects/{id}/sources/{dataset_id}` | implemented |
| POST | `/api/v1/projects/{id}/sources/profile` | implemented |
| GET | `/api/v1/projects/{id}/schema-comparison` | implemented |
| GET | `/mock/billing/v1/subscriptions` | implemented, bearer token, paginated |
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
    api  fastapi, mock target platform (Day 4), mock billing source, 127.0.0.1:8000
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
