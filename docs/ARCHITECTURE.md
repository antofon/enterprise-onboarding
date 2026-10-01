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

- **api service (implemented):** one FastAPI process serving the onboarding API under `/api/v1`, the mock target platform under `/target/v1`, and the mock legacy billing source under `/mock/billing/v1`. Mounting these in the same process keeps the reviewer experience to one `docker compose up`; the loaders and the migration layer still talk to them over HTTP so the seam is real.
- **mock billing source (implemented):** `/mock/billing/v1/subscriptions`, the customer's LegacyBill 4.2 api: bearer token from `BILLING_API_TOKEN`, `page` and `page_size`, `has_more`, records served from the same `subscriptions.json` the generator writes. The profiler has to authenticate and page like it would against the real thing. A wrong token is a 401 in the standard envelope; a feed failure surfaces as a 502 `source_unavailable` on the profile call and leaves the project's stage alone.
- **source profiler (implemented):** `app/services/profiling`. Loaders, value-level type inference, per-column stats, dataset quality issues, cross-dataset key checks. Details under "Source profiling".
- **schema comparison (implemented):** `app/services/comparison.py`. Deterministic classification of every source field against the target catalog. Details under "Schema comparison".
- **ai mapping service (implemented):** `app/ai` holds the provider interface (Anthropic default, OpenAI behind the same interface, `none` is manual mode), the answer schema, and the prompt builder; `app/services/mapping.py` asks per dataset, checks the answer against the catalog, sends problems back a bounded number of times, caches by input hash, and writes one `field_mappings` row per column. Details under "AI boundaries".
- **human review + customer clarification (implemented):** `app/services/mapping.py` (decisions) and `app/services/clarifications.py` (questions). Approve, reject, ignore, edit, ask the customer, reopen; bulk approval at or above the configured threshold only; the customer's answer can resolve the mapping on the spot. Details under "Human review".
- **transformation engine (implemented):** `app/services/transform`. Pure converters chosen from the target field's own type, the customer's value maps and rule settings from `sample_customer/transformation_config.yaml`, record rules and cross-dataset rules as named functions citing the customer's rule numbers, and a plan derived from the approved mappings before any row runs. Details under "Transformation".
- **validation engine (implemented):** `app/services/validation.py`. The target's pydantic contract, the platform's cross-record invariants, and the customer's own cross-checks, over every transformed record. Errors block a record; warnings travel with it. Details under "Validation".
- **migration runner (implemented, dry run):** `app/services/migration.py`. One http request per record against the target api, parents before children, into a staging namespace of the run's own; retries that are safe because writes are idempotent; every refusal stored with the status, the target's message and the request id. Details under "Dry run against the target".
- **mock target platform (implemented):** `app/api/target.py` and `app/services/target_store.py`, Meridian's write api: bearer token, the pydantic models as the field contract, the cross-record rules a real platform owns (organization before its children, ids unique per entity, identical re-send is a no-op and a changed one a 409, one primary contact and one address per organization, no live subscription under a dormant account), namespaces, and injectable faults. Details under "Dry run against the target".
- **database (implemented):** one PostgreSQL with two schemas. `onboarding` holds this tool's state. `target` holds the fictional platform's tables and is only ever written through the target API.
- **workbench ui (implemented: overview, source assessment, mapping review, dry run):** Streamlit, talks to the API over HTTP only. `ui/streamlit_app.py` holds the navigation and the project selector, `ui/views/` one module per screen.
- **cli (implemented: `check`, `generate-data`, `profile`, `suggest`, `eval-mapping`, `transformation-plan`, `validate`, `dry-run`):** Typer. `profile` runs the profiler and, with `--compare`, the schema comparison on one file or feed without the API or the database. `suggest` asks the configured model for one file the same way. `eval-mapping` scores the model against the golden set. `transformation-plan`, `validate` and `dry-run` run the later stages on a project from the terminal and print the same counts the workbench shows.
- **logging (implemented):** structlog, JSON in containers, request id on every line, context fields bound per stage.

## Data flow

Implemented through the dry run. Reconciliation and the readiness report are planned and marked.

1. **Attach.** `POST /api/v1/projects/{id}/sources` records where each source lives: a csv or json file under one of the configured source roots (`sample_customer/data`, `generated`) or the url of a feed. File paths are checked against the roots before the filesystem is touched; absolute paths and `..` are refused with a 422. Nothing is read yet. `GET /api/v1/sources/available` lists what can be attached so the workbench offers a pick list instead of a text box.
2. **Load.** `POST /api/v1/projects/{id}/sources/profile` loads every attached source into a dataframe of raw values. csv cells stay the strings that were in the file: pandas is told not to guess types, so zip codes never become floats and blanks stay blank. json and api records keep their json types, so a column that is an integer in 70% of records and a string in the rest is reported as exactly that. The billing feed is paged over http with the bearer token, page size and `has_more` the legacy system uses. The http client is a FastAPI dependency, which is how the tests hand the app its own test client and the loader still speaks http.
3. **Profile.** Every non-blank cell gets a tag: integer, decimal, numeric text with symbols, date with the format that parsed it, datetime, email or malformed email, url with or without a scheme, phone or short phone, identifier, code, text. Placeholders (`n/a`, `unknown`, `-`) count as missing and are counted separately. The column's type is the dominant tag, with a few rules on top (a digits-only column named like an id is an identifier, not a quantity; `0`/`1` is boolean only when the name says so). Whatever does not fit the type is counted as malformed, with examples. Per column: null and placeholder counts, distinct count and uniqueness, sample values, value shapes (`AA-999999`), the full value distribution when there are at most 50 distinct values, case variants, stray whitespace, and per-type extras (date formats and ISO share, numeric text and `x.0` counts, boolean spellings, E.164 share, normalized duplicates for emails and names). Per dataset: exact duplicate rows, the key column (a name that says id, ref or num with nearly unique values), missing and duplicated keys.
4. **Cross-check.** Once every source is loaded, any column that carries the same name as another dataset's key column is checked against it: how many values resolve, how many are orphans, with examples. On the sample this finds the injected dangling account numbers and, on top of them, every contact, subscription and activity of the organizations whose own account number is blank.
5. **Assess.** The profile becomes a list of issues with a severity. **error**: rows will be rejected or cannot be migrated until someone decides (missing or duplicated keys, orphan references, malformed emails or ids). **warning**: a rule or a decision is needed before the dry run (mixed date formats, numeric text, placeholders, high null rates, near-duplicates, inconsistent json types, future dates). **info**: normalization handles it (case variants, whitespace, boolean spellings, urls without a scheme, upper-case emails).
6. **Store.** One `source_fields` row per column and the dataset-level summary (counts, key facts, references, issues) in `source_datasets.quality`. Re-profiling replaces both. The project moves to `profiled`.
7. **Compare.** `GET /api/v1/projects/{id}/schema-comparison` rebuilds the profiles from the database and classifies every source field against the target catalog. Computed on request, never stored: it is a pure function of the profile and the catalog. The mapping stage is where decisions get persisted.
8. **Map.** `POST /api/v1/projects/{id}/mappings/suggest` rebuilds the profiles and the comparison, then asks the model once per dataset with the brief described under "AI boundaries", or, in manual mode, turns the comparison itself into the proposal (origin `heuristic`, no confidence invented). Every column gets a `field_mappings` row: proposed target, confidence, reason, whether a rule is needed and which, whether the customer has to be asked. Rows a person already decided are kept; `force` starts over. Fields the model flags open a `clarification_questions` row. The project moves to `mapped`.
9. **Review.** `PATCH /api/v1/projects/{id}/mappings/{mapping_id}` with an action: approve (optionally with a different target and a rule), reject, ignore, edit, clarify (opens a question), reopen. Every approved target is checked against the catalog. `POST .../mappings/bulk-approve` approves suggested rows with a target at or above the high-confidence threshold, never below it, and only when a person calls it. The first decision moves the project to `in_review`; when every field is decided and no question is open it moves to `ready_to_transform`; reopening pulls it back.
10. **Clarify.** `GET/POST /api/v1/projects/{id}/clarifications`, `PATCH .../{question_id}` to record the customer's answer. The answer can carry a resolution (approve with a target and a rule, ignore, reject) that is applied to the mapping through the same decision path a reviewer uses; without one the mapping returns to the reviewer's queue with the answer attached.
11. **Plan.** `GET /api/v1/projects/{id}/transformation-plan` resolves the approved mappings into what will happen to every column, and refuses with a 409 naming the columns while any mapping is undecided or any question is open. Derived, not stored: approved mappings plus the target catalog plus the customer's configuration, so two runs from the same approvals do the same thing and the plan can be read before anything runs.
12. **Transform.** Every source is loaded read-only the way profiling loads it. For every row, one draft record per entity the dataset emits: each approved column runs through its converter chain, the record rules see the whole record, and the cross-dataset rules see every record. Nothing is written anywhere. What comes out is every record the migration would send, every reason a record cannot go, every note on what normalization changed, and the name of every rule that touched a record. On the sample: 9,259 rows in about one second.
13. **Validate.** `POST /api/v1/projects/{id}/validate`. Every draft against the target's pydantic contract, then the batch against the platform's invariants and the customer's cross-checks. A `migration_runs` row of kind `validation` keeps the counts per entity, the issue counts by type, the rules applied and the normalizations, and every issue goes to `validation_issues` with the dataset, the row, the record id, the column and the value, so somebody can go and look. The project moves to `validated`. Nothing reaches the target.
14. **Dry run.** `POST /api/v1/projects/{id}/migrations/dry-run`. The same transform and validate, then every valid record to the target api over http, organizations first, into a namespace named after the run. The run row records attempted, accepted, created, unchanged, rejected, failed, blocked and retries per entity, what the target holds in the namespace afterwards, and every refusal in `migration_failures`. The namespace is kept for inspection and reconciliation unless the caller asks for it to be purged. The project moves to `dry_run_complete`.
15. **Reconcile, report.** Planned.

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

| `field_mappings` | one row per source column per project: proposed `target_path`, `status` (suggested, needs_clarification, approved, rejected, ignored), `origin` (model, heuristic, manual), `confidence`, `reason`, `transformation_required` and the rule in plain English, `clarification_required`, what the comparison said (`comparison_class`, top `candidates`), which model and `llm_calls` row produced it, and who decided what when (`decided_by`, `decided_at`, `decision_note`). Unique per project, dataset and column |
| `clarification_questions` | a question for the customer, usually tied to one mapping: the text, `context` (values seen, candidate targets, proposed target), `origin`, `status` (open, answered, withdrawn), the `answer`, who gave it, and the `resolution` applied to the mapping |
| `llm_calls` | every model call: purpose, dataset, provider, model, prompt version, `input_hash`, `cached`, status, attempts, tokens in and out, latency, the provider's request id, and the structured `response` (field names and reasons, never rows). The cache is a lookup on `input_hash` |

`stage` is a plain varchar validated by a Python enum rather than a native PostgreSQL enum, because the stage list moves during the build and native enums cannot be altered by `create_all`.

| `migration_runs` | one validation pass or one dry run: kind, status, the staging `namespace` (null for a validation pass), the configuration version and summary, the options, the whole transformation plan, per-entity `stats` (source rows, built, skipped by rule, valid, invalid, with warnings, attempted, accepted, created, unchanged, rejected, failed, blocked, retries), totals, `issue_counts` by type, `applied_rules`, the top `normalizations`, `target_counts`, timings, truncation flags, the error |
| `validation_issues` | one row per thing wrong with one record in a run: entity, dataset, source row, record id, column, error type, severity, message, the offending value, the rule that found it |
| `migration_failures` | one row per record the target refused or the run never attempted: entity, record id, dataset, source row, stage (`target` or `blocked`), attempts, http status, error type, the target's message, request id, an excerpt of the body |

The transformation plan is not a table: it is derived from the approved mappings and stored on the run that used it, the same way the schema comparison is derived from the profiles.

Schema `target` (the fictional platform, written only through its api): `organizations`, `contacts`, `subscriptions`, `activities`, each keyed by `(namespace, <entity>_id)` with the accepted fields as typed columns, a `content_hash` of the accepted payload for the id contract, and the request id that wrote the row. Children carry a composite foreign key to the organization in the same namespace, so purging a namespace cascades.

Coming with later milestones: `reconciliation_results`, `readiness_reports`.

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
| GET | `/api/v1/sources/documents` | implemented: context documents under the document roots |
| GET | `/api/v1/target-fields` | implemented: the target catalog |
| POST | `/api/v1/projects/{id}/mappings/suggest` | implemented; 502 `llm_error` when the model cannot give a usable answer |
| GET | `/api/v1/projects/{id}/mappings[?dataset=&status=]` | implemented |
| GET | `/api/v1/projects/{id}/mappings/summary` | implemented: counts, open questions, required-field coverage by approved mapping |
| POST | `/api/v1/projects/{id}/mappings/bulk-approve` | implemented, floor at the configured high-confidence threshold |
| GET / PATCH | `/api/v1/projects/{id}/mappings/{mapping_id}` | implemented; PATCH takes an action |
| GET / POST | `/api/v1/projects/{id}/clarifications` | implemented |
| GET / PATCH / DELETE | `/api/v1/projects/{id}/clarifications/{question_id}` | implemented; PATCH records the answer, DELETE withdraws |
| GET | `/api/v1/projects/{id}/llm-calls` | implemented: model, tokens, latency, cached, errors |
| GET | `/api/v1/projects/{id}/transformation-plan` | implemented: what the approved mappings will do to every column; 409 while any column is undecided |
| POST | `/api/v1/projects/{id}/validate` | implemented: transform and check every record, write nothing |
| POST | `/api/v1/projects/{id}/migrations/dry-run` | implemented: every valid record through the target api into a staging namespace |
| GET | `/api/v1/projects/{id}/migrations[?kind=]` | implemented: every run, newest first |
| GET | `/api/v1/projects/{id}/migrations/{run_id}` | implemented: one run with its counts |
| GET | `/api/v1/projects/{id}/migrations/{run_id}/issues[?entity=&severity=&error_type=]` | implemented |
| GET | `/api/v1/projects/{id}/migrations/{run_id}/issue-breakdown` | implemented: counts by entity, severity and type |
| GET | `/api/v1/projects/{id}/migrations/{run_id}/failures[?entity=&error_type=]` | implemented: refused and blocked records |
| GET | `/api/v1/projects/{id}/reports/readiness` | Day 5 |
| POST | `/target/v1/organizations`, `/contacts`, `/subscriptions`, `/activities` | implemented: bearer token, `X-Meridian-Namespace`, 201 created / 200 unchanged / 409 / 422 / 500 |
| GET | `/target/v1/counts`, `/target/v1/{entity}`, `/target/v1/{entity}/{id}` | implemented, per namespace |
| DELETE | `/target/v1/namespaces/{id}` | implemented: purge a staging namespace; the live one is refused |

Every non-2xx response is `{"error": {"type", "message", "details"}}`, including the framework's own 404/405/422.

## AI boundaries

See "The one rule" above. This is how the mapping step keeps to it.

**One interface, three providers.** `app/ai/provider.py`. `complete(system, user, output=<pydantic model>)` returns an instance of that model plus tokens, latency and the provider's request id. Anthropic uses `messages.parse` with the model as `output_format`; OpenAI uses `responses.parse` with it as `text_format`; both are constrained to the schema at decoding time. `LLM_PROVIDER=none`, or a provider whose key is missing, is `NullProvider`: manual mode, never a faked answer. No provider or model name appears outside configuration. Both SDKs retry 429s and 5xx twice on their own; the application does not add transport retries on top.

**What the model sees.** `app/ai/prompts.py`. One dataset per call. Per column: the inferred type, null and unique rates, distinct count, placeholder and whitespace counts, and then only what the column's kind justifies. Small vocabularies (statuses, tiers, plans, roles, countries, booleans) are sent with their counts, because those values are the business meaning. Dates get their formats and range, numbers their range and how many carry symbols, identifiers and codes their shapes and up to three examples. Emails, phones, urls, people's and companies' names, free text (notes, descriptions, owners) get shapes and counts and no values at all. Alongside: the deterministic comparison for the column (classification, top candidates with name similarity and compatibility notes), the whole target catalog with types, enums, required flags and descriptions, the target's cross-record rules, the dataset's key and orphan facts, the customer's business-rules document (read from a path-checked document root, capped at 24,000 characters), and the project notes. Rows never leave the database; the largest thing in a prompt is the rules document.

**What comes back, and what happens if it is wrong.** `app/ai/schemas.py`. One entry per source field: `target_field` (a catalog path or null), `confidence`, `reason`, `transformation_required` and the rule in plain English, `clarification_required` and the exact question for the customer, plus dataset-level observations. The schema is closed and every field is required so both providers' structured output modes can enforce it. What the schema cannot enforce is checked in `check_proposal`: every source field answered exactly once, no invented fields, every target in the catalog, confidence in 0..1, a question whenever one is required, a rule whenever one is required. Problems are sent back to the model verbatim (`Your previous answer had these problems...`) up to `LLM_MAX_ATTEMPTS` times in total, then the call is recorded as failed and the API answers 502 `llm_error` with the problems listed. Nothing is written to `field_mappings` from a rejected answer.

**Cache.** The input hash is a sha256 over the prompt version, the system prompt, the provider, the model and the full brief. A successful `llm_calls` row for the same project and hash is served instead of a new call and recorded as a cached call with zero tokens. Re-profiling, a changed rules document, a changed catalog or a prompt edit changes the hash. `force` bypasses it.

**Usage logging.** Every call is an `llm_calls` row and a structured log line: provider, model, attempts, input and output tokens, latency, request id, and the outcome. Keys are read from the environment and never logged; the prompt is not stored; the response is stored because it is the proposal and its reasons, which the review screen and the audit trail need.

**Manual mode.** With no provider the suggest endpoint still works: the comparison's `MATCHED` and `TRANSFORMATION_REQUIRED` fields become proposals with origin `heuristic`, `AMBIGUOUS` fields open a templated question naming the candidates, `UNMAPPED` and `INCOMPATIBLE` fields wait for a person. No confidence is invented; the column shows `no confidence`. Everything downstream is the same.

**Measured.** `evals/expected_mappings.json` is the golden set for the committed sample: 41 fields, including one that is genuinely ambiguous by the customer's own rules (`customer_tier`) and several where "not needed" is the right answer. `enterprise-onboarding eval-mapping` runs the same prompt path with no database and writes `evals/results/*.json`: accuracy over all fields, accuracy over fields with an expected target, deferred and unresolved counts, wrong and overconfident counts, clarification recall and false positives, transformation-flag recall, the number of wrong answers at or above the high-confidence threshold, and mean confidence for right versus wrong answers. The README quotes only from those files.

## Human review

Implemented. Every `field_mappings` row is in one of five states, and only a person moves it to the three terminal ones.

- **suggested**: proposed by the model, the comparison, or edited by a person; nobody has decided.
- **needs_clarification**: at least one open question for the customer.
- **approved**, **rejected**, **ignored**: a person decided. `decided_by`, `decided_at` and `decision_note` say who, when and why.

Actions (`PATCH .../mappings/{id}`): **approve** (with the proposed target, or a different one from the catalog, and optionally a rule), **reject**, **ignore** (the field is not migrated), **edit** (change target or rule, stays undecided, origin becomes `manual`), **clarify** (opens a question, the row waits), **reopen** (back to suggested, open questions withdrawn). Approving or ignoring withdraws open questions on that row so nothing stays half-asked.

**Bulk approval** is a person's action over suggested rows with a target and a confidence at or above `MAPPING_HIGH_CONFIDENCE` (0.85 by default). A caller may raise the bar, never lower it. Rows that ask a question, have no target, or come from the heuristic (no confidence) are never bulk-approved.

**Thresholds only steer attention.** The workbench colors confidence green at or above the high threshold, red below the low one (0.6), amber between. Nothing changes state because of a color.

**Re-suggesting keeps decisions.** A second suggest run replaces suggested rows and leaves decided rows and rows with an open question alone; the run reports how many it kept. `force` replaces everything and withdraws the open questions, for the case where the inputs changed enough that the old decisions should not survive.

**The customer's answer is a decision path, not a side channel.** Answering a question with a resolution calls the same `decide()` the reviewer uses, with the customer's answer as the note and the answerer as the reviewer. Answering without one puts the row back in the reviewer's queue with the answer attached.

## Transformation

Implemented. `app/services/transform`. The stage that turns the customer's values into Meridian's, and the one where "deterministic" has to mean something specific.

**Converters are code and pure.** `converters.py`. One value and a context in, one value or a reason out, no database, no model, no knowledge of which customer it is. `trim`, `blank_to_null`, `string`, `identifier`, `date`, `datetime`, `money`, `integer`, `boolean`, `email`, `phone_e164`, `url`, `person_name_split`, `enum_map`, `country_code`, `region_code`, `string_list`, `title_case`. A chain runs left to right and stops at the first empty value or the first error. Every conversion that changed the meaning of a value leaves a note on the record (`'Q' mapped to 'monthly' (customer rule 9)`, `date read as %m/%d/%y`, `name split in two`), and the notes are aggregated on the run so a reviewer can see that 1,819 phone numbers were assumed to be +1.

**Nothing is repaired by guesswork.** `lindsey.herrera at wong.com` is reported as a malformed address, not turned into `lindsey.herrera@wong.com`, because an address the tool invents reaches a real person. A plan value the rules do not name (`Trial`), a status that is a typo (`Actve`), an activity kind Meridian has no slot for (`Call`) are reported with the value and the count, which is the customer's to-do list, not the tool's.

**The customer-specific half is configuration.** `sample_customer/transformation_config.yaml`, loaded by `config.py` into a closed pydantic model. Which of the customer's words mean which of Meridian's values, each map citing the numbered rule in `business_rules.md` it comes from; the policy for a word that is not on the list (`error`: reject and report; `default`: leave the field out so Meridian's own default applies, with a note; `null`: leave it empty); the date and timestamp formats to try, in order; the placeholders that mean "nothing here"; converter overrides for the fields whose type cannot choose (one name column into two, a phone format, a country vocabulary); and which record rules are on, with their thresholds. The file is reviewed, read-only at runtime and never written by a model. A second customer is a second file. A converter name that does not exist, or a yaml boolean where a province code was meant (`ON`), is a load-time error with a sentence, not a failure on row 4,000.

**The target's type picks the normalizer.** `plan.py`. An enum field gets `enum_map`, a date gets `date`, a decimal gets `money`, a string with the identifier pattern gets `identifier`, and every chain starts with `trim` and `blank_to_null`. Configuration overrides the chain only where listed. The plan is derived from approved mappings and refuses to exist while any column is undecided.

**A dataset emits an entity only when it maps that entity's identifier.** `organizations.csv` maps `acct_num` to `organization.organization_id`, so it emits organizations. It also maps `primary_contact_email` to `contact.email`, but it maps no `contact.contact_id`, so it does not emit contacts: that value travels as context on the organization draft for the cross-dataset rules. That is the general rule behind rule 1 below, and it is what stops a contact being invented out of an email address.

**Record rules are named, cited and counted.** `record_rules.py`. Each is a function of the whole draft, enabled and parameterised from configuration, and leaves its name on every record it touches: `ca_country_from_region` (rule 5: CA is Canada unless the state column says a US state, and flagged when there is no state to tell), `dormant_account_inactive` (rule 2: no activity in eighteen calendar months, measured from the configured `as_of` date, becomes inactive; churned stays churned), `legacy_gold_seat_split` (rule 7: Legacy Gold under ten seats is Professional; with no seat count the plan is undecidable and the record is reported), `drop_dead_trials` (rule 11: a trial older than ninety days does not migrate; the record is skipped, counted and named, never silently dropped). The cross-dataset rule `primary_from_account_email` (rule 1) flags the contact whose address the account row names, where no contact on the account carries the flag; where the address is not in the contact export the account is flagged for the customer instead. On the sample it flags nobody and reports 127 accounts, which is the finding: the CRM's account-level address does not match the contact export for any account that lacks a primary.

**Dates are measured from a pinned date.** `as_of` in the configuration, not `today()`, so a run in six months reproduces today's numbers and the build log's counts stay true.

## Validation

Implemented. `app/services/validation.py`. Three layers over every transformed record, all deterministic.

- **The contract.** Each draft is validated against the pydantic model in `app/target/schema.py`, which is the same model the target api applies on write. Types, enums, patterns (ISO country codes, E.164, the identifier shape), required fields, renewal after start. Pydantic's error codes are translated into the vocabulary the rest of the run uses (`missing_required`, `invalid_enum_value`, `invalid_format`, `out_of_range`, ...) and the offending value travels with the issue.
- **The invariants.** Rules that need the whole batch and belong to the platform whatever the customer: identifiers unique per entity (the second occurrence is reported, naming the row of the first); every child's organization among the organizations that will migrate; at most one primary contact per organization; one address per organization; no trial or active subscription under an inactive or churned organization; no customer_since, start_date or occurred_at after the migration date (a warning).
- **The customer's cross-checks.** From `validation_rules` in the configuration. `strategic_requires_enterprise` (rule 4) flags a subscription whose organization is Strategic in the CRM and whose plan is not Enterprise. It reads the tier column from the source row because that column has no approved target yet, and it changes nothing: rewriting a plan from a column nobody has agreed to map would be exactly the quiet decision this tool exists to prevent.

**Two messages that matter.** A child whose organization is in the export but cannot migrate is reported as `parent_record_rejected`, with the organization's own error types, because fixing the account releases the children; a child whose organization is not in the export at all is `missing_relationship`, because the customer owes the data. On the sample those are 839 and 143 records, and conflating them would hide where the work is. A subscription refused because its account is dormant says whether the CRM said so or whether `dormant_account_inactive` did: in the second case the conflict is between two of the customer's own rules, and 311 of the sample's subscriptions sit in it.

**Severity.** `error`: the record is not sent. `warning`: the record is sent and the warning stays on the run. `info` is reserved. A record skipped by a customer rule is not validated at all, and a record that already failed conversion is not re-reported by the batch checks.

**Measured on the committed sample** (configuration `2026-10-01.1`, as of 2026-10-01): 9,259 rows built, 75 skipped by rule 11, 7,163 valid (308 with warnings), 2,021 blocked by errors. Organizations 889 of 1,030; contacts 1,702 of 2,279; subscriptions 414 of 1,003; activities 4,158 of 4,947. The largest blocks are `parent_record_rejected` (839), the dormant-account conflict (311), values with no mapping (258, of which 215 are `Call` activities) and required fields that are empty (181). Transform and validate together take about 1.3 seconds.

## Dry run against the target

Implemented. `app/services/migration.py` on this side of the seam, `app/api/target.py` and `app/services/target_store.py` on the platform's side. Both live in the same process for the reviewer's convenience; the runner reaches the platform only over http, with the platform's bearer token, one record per request, exactly as it would reach a real one.

**Namespaces.** Every row the platform stores belongs to a namespace. The all-zero uuid is the live data; a dry run writes into a namespace named after its run id. Namespaces are isolated: a child cannot find a parent in another namespace, the same identifier in two namespaces is two records, and the live data is never touched by a rehearsal. A namespace can be read back and counted (`GET /target/v1/counts`, `GET /target/v1/{entity}`) and purged in one call (`DELETE /target/v1/namespaces/{id}`); the live namespace refuses to be purged. By default a run keeps its namespace so it can be inspected and reconciled.

**The id contract.** Writes carry the customer's stable identifier. The platform hashes the accepted payload: the same identifier with identical content is a 200 and a no-op, with different content a 409 `duplicate_identifier` listing the fields that differ. That is what makes a retry safe and what makes a re-run of the same records in the same namespace an audit rather than a double-write.

**What the platform enforces, and what the runner does about it.**

| the target says | status | runner |
|---|---|---|
| created | 201 | accepted, created |
| already there, identical | 200 | accepted, unchanged |
| duplicate identifier, duplicate primary contact, duplicate email | 409 | rejected, never retried |
| missing relationship, business rule violation, contract violation | 422 | rejected, never retried |
| unavailable | 500, 502, 503, 504, 408, 429 | retried with backoff, then failed |
| answered 200 with a body that is not a write result | 200 | treated as failed and retried |
| no answer within the timeout, connection failure | none | retried, then failed as `timeout` or `transport_error` |

A 409 or a 422 is the target's considered answer and is never retried. Everything else is retried up to `TARGET_MAX_ATTEMPTS` with a linear backoff, because a repeat of the same record is a no-op.

**No wasted calls.** Organizations are written first. A contact, subscription or activity whose organization is not in the namespace, because the target refused it or because the run never wrote it (a limited run, or a run restricted to one entity), is recorded as blocked with the organization that blocked it and is not sent. On a full run of the sample every one of the 7,163 valid records was accepted and nothing was blocked: validation caught everything the target would have refused, which is checked, not assumed, by the target api's own tests.

**Faults, on purpose.** The mock platform can be told to fail: `TARGET_FAULT_RATE` and `TARGET_FAULT_MODES` draw per record, seeded with the record id so a demo fails on the same records every time, and the `X-Meridian-Fault` header forces one on a single request. `server_error` answers 500 and writes nothing. `malformed` performs the write and answers 200 with the wrong body, so the runner has to notice and the retry has to be a no-op. `timeout` performs the write and then sleeps past the client's timeout, so the retry gets `unchanged`, which is the id contract doing its job. Off by default: a plain run only fails where the customer's data is bad.

**What a run keeps.** `migration_runs`: kind, status, namespace, the configuration version and summary, the options, the whole plan, per-entity stats from source rows through accepted and blocked, totals, issue counts by type, rule counts, the top normalizations, what the target holds, timings, and the error if the run failed. `validation_issues`: every issue, errors first, up to `VALIDATION_ISSUE_LIMIT` with a flag when there were more. `migration_failures`: every refusal and every blocked record with the stage, the attempts, the http status, the error type, the target's message, the request id and an excerpt of the body. The customer's rows are not stored; an issue keeps the identifier, the column and the offending value.

**Measured on the committed sample.** Full dry run, no limit: 7,163 records sent, 7,163 accepted (7,163 created), 0 rejected, 0 failed, 0 blocked, 0 retries; the namespace holds 889 organizations, 1,702 contacts, 414 subscriptions and 4,158 activities, which is exactly what the run says it accepted. 55 seconds end to end in process, about 7 milliseconds per write, sequential. With every write forced to fail (`TARGET_FAULT_RATE=1.0`, `server_error`, two attempts), the run still completes, every organization is failed with status 500, the target's message and the request id, and every child is blocked.

## Failure recovery

Implemented for the migration layer; what the rest of the system does with a failure is listed for completeness.

- **A source is unreachable.** Profiling answers 502 `source_unavailable` and leaves the project's stage alone (Day 2). Transformation loads sources the same way and fails the run the same way: the run row is marked `failed` with the exception, nothing is half-written, and the stage is untouched.
- **The configuration is wrong.** A missing file, invalid yaml, a converter that does not exist, a yaml boolean in a value map: 500 `transformation_config_invalid` with the reason, before any row is read.
- **The mappings are not ready.** 409 `invalid_state` naming the undecided columns. Nothing runs.
- **The target is down, slow, or answers nonsense.** Retried per record, then recorded per record; the run completes with the counts it has and the stage still advances, because a rehearsal that found the target flaky is a rehearsal that did its job. The failures table says which records, with what, so the next run can be compared.
- **A write landed but the answer was lost.** The retry is a no-op by the id contract; the record is counted as accepted and unchanged, and the run's `retries` count shows it happened.
- **The run itself crashes.** The run row is marked `failed` with the error, the staging namespace keeps whatever landed for inspection, and the stage is untouched. Re-running starts a new run in a new namespace; nothing has to be cleaned up first.
- **Partial runs.** `limit_per_entity`, `entities` and `stop_after_failures` exist so a problem can be reproduced on a slice. Children whose organization the slice never wrote are blocked, not attempted, and say so.

## Local development vs AWS demo

Two deployments of the same code. Local is the build and review environment and stays fully functional with no AWS account. The AWS demo (planned, after the local workflow is verified end to end) extends it with object storage and a hosted instance. Nothing in the local path depends on AWS.

**Local development (implemented):**

```
laptop or vps
  docker compose
    db   postgres 16, published on 127.0.0.1:5433 only
    api  fastapi, mock target platform, mock billing source, 127.0.0.1:8000
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
