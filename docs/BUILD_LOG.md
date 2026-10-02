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

## 2026-09-30, Day 2

### Profile raw values, not pandas dtypes

- **Problem:** pandas guesses types on the way in. Zip codes become floats, `unknown` becomes NaN, `"60"` and `60` become the same column, and every one of those guesses erases a fact the implementation engineer needs to know about the customer's data.
- **Decision:** csv is read with every cell as the string that was in the file and no NA parsing; json and api records keep their json types. Each cell is tagged by our own rules and the column's type is decided from the tag counts. Everything that does not fit the type is counted as malformed, with examples.
- **Why:** the workbench and the docs quote numbers like "106 malformed emails" and "seat_count is an int in 709 records and a string in 253". Those have to be counts of cells that failed a stated rule, not artifacts of a parser.
- **Alternatives:** pandas inference plus `infer_objects` (the counts are gone by then). A profiling library such as ydata-profiling (heavy, html-oriented, and it would own the numbers instead of us).
- **Result:** the 1,000-organization sample (9,259 rows over four sources) profiles in about 2 seconds inside the container, including the paged billing feed. The 10,000-organization customer (92,751 rows) takes about 18 seconds, all of it in the per-cell tagging loop. Fine for the demo; vectorized tagging or sampling above a row threshold is noted for the Day 6 scaling pass.

### Profiler numbers are tested against the generator's manifest

- **Problem:** a profiler that reports plausible-looking numbers is easy to write and hard to trust.
- **Decision:** the unit tests load the committed sample and compare the profile with `manifest.json`, the generator's own count of every defect it injected. Where a defect maps one to one onto a measurement (missing ids, duplicated ids, missing seat counts) the test demands exact equality. Where duplication copies defective rows (a duplicated contact keeps its malformed email) the profiler must find at least as many.
- **Result:** one real finding fell out of the exact checks. The orphan check reports 75 dangling contacts against 50 injected. The other 25 are the contacts of the 13 organizations whose own account number is blank, which is precisely what an implementation engineer would need to know and what a looser test would have hidden.

### Schema comparison is deterministic and says AMBIGUOUS out loud

- **Problem:** the temptation to add a clever matcher that "gets" `acct_num` to `organization_id` so the comparison looks smarter.
- **Decision:** names, a small synonym table, the profiler's stats, and the target catalog. Nothing else. Anything semantic comes out as `UNMAPPED` or `AMBIGUOUS` with the candidates listed, and the model plus a person handle it on Day 3. The score is a name similarity and is labelled as such.
- **Why:** the one rule in ARCHITECTURE.md. Also, a deterministic pass that admits what it does not know is the ground truth the AI mapping step gets measured against later; if the heuristic already guessed, the eval would be measuring the heuristic.
- **Alternatives:** string distance (Levenshtein happily pairs `last_touch` with `last_name`), embeddings (a model call in disguise, without the rationale or the review step).
- **Result:** on the sample, 5 `MATCHED`, 18 `TRANSFORMATION_REQUIRED`, 2 `AMBIGUOUS`, 16 `UNMAPPED`, 0 `INCOMPATIBLE`, and the required-field coverage names the exact semantic gaps: `organization_id` everywhere, `contact.first_name` and `last_name`, `subscription.plan` and `mrr_usd`. Three judgment errors in the first cut were fixed before commit: the entity prefix scoring as a match (`subscription_level` against `subscription_id`), generic words earning a containment bonus (`full_name` against `organization.name`), and whitespace alone turning a `MATCHED` into a `TRANSFORMATION_REQUIRED`.

### Comparison is computed on request, not stored

- **Decision:** `GET /schema-comparison` rebuilds the profiles from the database and recomputes. Nothing is persisted.
- **Why:** it is a pure function of the profile and the catalog, so storing it only creates a stale copy. Decisions (approve, reject, edit, ask the customer) are what deserve rows, and those arrive with the mapping stage.

### The billing feed is a real http seam inside the same process

- **Problem:** "at least one REST API or simulated external service" is easy to fake with a file read behind a function called `api`.
- **Decision:** `/mock/billing/v1/subscriptions` is a router in the same FastAPI process with a bearer token, `page`, `page_size` and `has_more`. The loader pages through it with httpx like it would against the customer's system. The http client is a FastAPI dependency, so the tests hand the app its own test client and the loader still speaks http.
- **Result:** the 401, the pagination arithmetic and the "feed is down" path (502 `source_unavailable`, stage untouched) are all exercised by tests, not assumed.

### Relationships by exact key name only

- **Decision:** a column references another dataset's key when it has the same name. `acct_num` in `contacts.csv` against `acct_num` in `organizations.csv`, nothing fuzzier.
- **Why:** dumb rules are explainable rules. Deciding that `account_id` and `acct_num` are the same key is a mapping decision and gets made where mapping decisions are reviewed.

### Workbench verified in a real browser before commit

- **Problem:** the first cut of the source assessment screen looked fine in the code and was wrong in the browser: a "page not found" modal on the overview (the default page had a url path), a columns table cut off in a half-width column, and an emoji status dot that renders as a box on machines without an emoji font.
- **Decision:** every screen gets a headless Chromium pass (Playwright) that checks page errors, console errors and the presence of the content it is supposed to show, with screenshots reviewed by eye. Layout is full width and stacked; the status dot is a css character.
- **Why:** the workbench is what a hiring manager sees in the demo. jsdom-style assertions would have passed all three defects.

## 2026-09-30, Day 3

### The model answers in a schema, and the schema is not enough

- **Problem:** structured output guarantees the shape of the answer, not its sense. A target path that is not in the catalog, a source field answered twice or not at all, `clarification_required` with no question text, `transformation_required` with no rule: all valid JSON, all unusable.
- **Decision:** two layers. The providers' structured-output modes (`messages.parse` on Anthropic, `responses.parse` on OpenAI) enforce a closed Pydantic schema at decoding time. `check_proposal` then enforces the contract the schema cannot carry, and every problem is phrased so it can go back to the model verbatim ("target_field 'organization.nope' is not in the catalog; use an exact entity.field path or null"). Up to `LLM_MAX_ATTEMPTS` (3) in total, then the call is recorded as failed and the API answers 502 with the problems. Nothing is written from a rejected answer.
- **Alternatives:** JSON inside free text with regex repair (fragile, and it hides which part was wrong). Trusting the schema alone (an invented target would have landed in `field_mappings` and only failed at transformation time, far from its cause).
- **Result:** ten real calls across both providers passed the contract on the first attempt. The retry and give-up paths are exercised by a fake provider that misbehaves on purpose, in unit and integration tests.

### The brief is statistics, and a model still misread one

- **Problem:** in the first real run GPT-5 read `null_pct: 1.0` on `contacts.acct_num` as "100% null" and built a customer question around a column that is 1% blank.
- **Decision:** `null_percent`, `unique_percent`, `iso_percent`, a `null_count` next to the rate, and one sentence in the system prompt: percentages run 0 to 100. Prompt version bumped to `2026-09-30.2`, which invalidates the cache on purpose.
- **Why:** the model cannot see the data, so every number in the brief has to be unambiguous on its own. A name that a person reads correctly is not enough.

### What the model is allowed to see

- **Decision:** per column the brief carries type, null and unique rates, distinct count, and then only what the column's kind justifies. Small vocabularies (statuses, tiers, plans, roles, countries, booleans) go with their counts, because those values are the meaning. Dates go as formats and a range, numbers as a range and a count of values with symbols, identifiers as shapes plus three examples. Emails, phones, urls, people's and companies' names and free text (notes, descriptions, owners) go as shapes and counts, no values. A unit test asserts that no email address from the sample can appear in a prompt.
- **Why:** the prompt is the only place customer data would ever leave the box. Account numbers and plan names are not personal data; a contact's email is.
- **Alternatives:** send the first five rows (what most "AI maps CSV columns" demos do). Rejected: the model does not need the rows to map a column, and the customer would be right to ask why they were sent.

### Questions are for mapping decisions, not data cleanup

- **Problem:** the first Opus 5 run on `organizations.csv` asked nine questions for fifteen columns. Most were about blank keys, duplicate account numbers and defaults for blanks, which the validation stage settles later with the engineer, not the customer.
- **Decision:** a rule in the system prompt: `clarification_required` is for which target, what a value means, and how to translate a vocabulary the rules do not cover; blank keys, duplicates, malformed values and defaults belong in `observations`. The eval counts questions the golden set did not expect as `clarification_false_positives`.
- **Result:** with the rule in place, claude-opus-5 still asks on 7 fields the golden set did not mark and gpt-5 on 8 (`company_status`, `industry_code`, `contact_role`, `subscription_level`, `activity_type` for both: enum translations the rules cover only partly). Recorded, not hidden; the eval is the gate for the next prompt change.

### Manual mode proposes from the comparison and invents nothing

- **Problem:** the tool has to work with no model key at all, and "manual mode" cannot mean an empty screen.
- **Decision:** without a provider, `MATCHED` and `TRANSFORMATION_REQUIRED` fields from the deterministic comparison become suggestions with origin `heuristic` and no confidence; `AMBIGUOUS` fields open a templated question naming the candidates; `UNMAPPED` and `INCOMPATIBLE` wait for a person. Everything downstream is identical.
- **Alternatives:** fabricate a confidence for heuristic matches (0.7 for a name match, say). Rejected: the review screen would then color a name similarity as if it were a probability, which is exactly the confusion the comparison's own docstring warns against.

### Cache by input hash, kept on the call row

- **Decision:** the cache key is a sha256 over prompt version, system prompt, provider, model and the full brief. A repeat is served from the last successful `llm_calls` row with a response, and the repeat is logged as its own zero-token row with `cached = true`.
- **Why:** an unchanged project should cost nothing to re-suggest, and the audit trail should still show that someone asked. Re-profiling, a changed rules document, a changed catalog or a prompt edit all change the hash on their own.

### Datasets are asked about in parallel

- **Problem:** one Opus 5 call on `organizations.csv` took 122 seconds (11,150 tokens in, 10,208 out, adaptive thinking). Four datasets in sequence is eight minutes, past the workbench's request timeout and past anyone's patience in a demo.
- **Decision:** a thread pool over datasets (`LLM_PARALLEL_CALLS`, 4), each thread with a copy of the request's log context so its lines still carry the request id. Database work stays on the request thread. Successful proposals are recorded even when a sibling dataset fails, so a retry only re-asks the failure.
- **Result:** the full four-dataset eval runs in the time of its slowest call: 106 s for claude-opus-5, 90 s for gpt-5, against 277 s and 263 s of summed call time.

### A question the model raised is not a review

- **Problem:** the first cut moved the project to `in_review` as soon as the model opened a clarification question. The integration test expected `mapped` and was right: nobody had reviewed anything.
- **Decision:** `mapped` until a person decides something; `in_review` after the first decision; `ready_to_transform` when every field is decided and no question is open; later stages are pulled back only by a reopen.

### The golden set, and the first measured results

- **Decision:** `evals/expected_mappings.json` covers the 41 fields of the committed sample. Each field has the right target or null, an `accept` list of other answers that count, `clarify: true` for the one field the customer must be asked about (`customer_tier`, by the customer's own rule 3), and `clarify_ok` where asking instead of answering is fine. The scorer gives each field one outcome: correct, deferred, unresolved, wrong, overconfident. A unit test checks the golden set against the sample's columns and the catalog so it cannot drift.
- **Result:** both providers, prompt `2026-09-30.2`, one run each, four parallel calls: 41/41 fields correct, 33/33 fields with an expected target, clarification recall 1/1, 0 wrong, 0 overconfident, 0 wrong at confidence 0.85 or above; mean confidence on correct answers 0.85 (claude-opus-5) and 0.887 (gpt-5). Result files under `evals/results/`.
- **Caveat, recorded on purpose:** one customer, 41 fields, lenient alternatives. A perfect score says the prompt and both providers handle this sample; it is a regression check for prompt changes, not a claim about other customers. A second dataset with a different shape is the way to make the number mean more.

## 2026-10-01, Day 4

### The customer-specific half is a file, and no model writes it

- **Problem:** the transformation stage has to know that `Legacy Gold` is Enterprise, that `Q` billing is monthly now, that `On Hold` is active. That knowledge is one customer's, it changes when the customer's rules change, and the spec rightly forbids executing anything a model generated.
- **Alternatives:** a rule DSL interpreted at runtime (a second language to document and test, and a model would be tempted to write it); value maps hardcoded in Python per customer (a second customer means a code change and a deploy); letting the model's `transformation` text drive the engine (prose is not a program).
- **Decision:** converters are code and pure. What is specific to one customer lives in `sample_customer/transformation_config.yaml`: value maps citing the numbered rule each entry comes from, the policy for an unmapped word, date formats in order, placeholders, converter overrides for the four fields whose type cannot choose, record rules with thresholds. Loaded into a closed pydantic model, read-only at runtime. The model's `transformation` text from Day 3 is kept on the mapping as the reviewer's note and never executed.
- **Result:** a second customer is a second file. A converter name that does not exist fails at load time with the list of known ones. `ontario: ON` was the first thing the loader caught: yaml reads `ON` as a boolean, and the error now says so in a sentence instead of failing two hundred lines in.

### The target's type picks the normalizer

- **Problem:** forty-odd approved mappings, each needing the right chain of normalizers, and a reviewer should not have to pick them.
- **Decision:** the catalog's own type and constraints choose: enum gets `enum_map`, date gets `date`, decimal gets `money`, a string with the identifier pattern gets `identifier`, everything starts with `trim` and `blank_to_null`. Configuration overrides only where a type cannot know: one name column into two, a phone format, a country vocabulary, a region vocabulary. Four overrides for the whole customer.
- **Result:** `GET .../transformation-plan` shows the chain for every column before any row runs, and the plan refuses to exist while any column is undecided.

### Nothing is repaired by guesswork

- **Problem:** the export has 139 addresses like `lindsey.herrera at wong.com`. Turning ` at ` into `@` is a one-line fix and almost always right.
- **Decision:** reported, not repaired. An address the tool invents reaches a real person; the customer gets a list of 106 contacts (after deduplication) with the exact value. The same stance for `Trial` as a plan (27 subscriptions, no rule says which plan a trial becomes), `Actve` as a status (16 accounts, a typo nothing resolves), and `Call` as an activity kind (215 rows, Meridian's list has no slot for it). Each is a line in the readiness report with a count, which is the customer's to-do list, not the tool's.

### A dataset emits an entity only when it maps that entity's identifier

- **Problem:** `organizations.csv` maps `primary_contact_email` to `contact.email`. Taken literally that is a contact record per account row, with no name, which Meridian refuses.
- **Decision:** a dataset emits records for an entity only when that entity's id field has an approved mapping. Everything else mapped across entities travels as context on the draft, for cross-dataset rules. Rule 1 reads it to flag the primary contact; no contact is invented.
- **Result:** on the sample the rule flags zero contacts and reports 127 accounts whose named address is not in the contact export. That is the finding, and it is a better finding than 127 invented contacts.

### Dates are measured from a pinned date

- **Decision:** `as_of` in the configuration, not `today()`. Dormancy (rule 2), dead trials (rule 11) and future-date warnings all measure from it, and a run in six months reproduces the numbers in this log. Eighteen months is calendar months, clamped to the end of the month, because thirty-day arithmetic drifts.

### Validation tells you where the work is

- **Problem:** the first full validation pass reported 982 `missing_relationship` errors. Only 143 of those child records point at accounts that are not in the export at all; the other 839 point at accounts that are in the export but failed validation themselves, and the two need different people to fix them.
- **Decision:** two error types. `parent_record_rejected` names the organization and its own error types, because fixing the account releases the children. `missing_relationship` means the account is not in the export at all, because the customer owes the data. On the sample: 839 and 143.
- **Result:** the dormant-account conflict is reported the same way. 311 subscriptions are refused because the account is dormant, and the message says whether the CRM said so or whether `dormant_account_inactive` did. In the second case the conflict is between two of the customer's own rules (rule 2 against the billing export), and no amount of engineering settles that; a person does.

### A dry run is a rehearsal, not a simulation

- **Problem:** "dry run" could mean validate and stop. The spec asks for a migration into a staging environment with real failure handling, and a simulation would prove nothing about the target's rules or the runner's retries.
- **Alternatives:** write into the live target tables and roll back (one transaction across seven thousand http requests is not how the real thing works); a separate database for staging (a second compose service for a demo); a flag on each row (works, but the live data and the staging data share uniqueness constraints and parent lookups).
- **Decision:** namespaces. Every row in the target schema belongs to one; the all-zero uuid is live, a dry run writes into a namespace named after its run id. Keys are `(namespace, id)`, parents are looked up in the same namespace, the composite foreign key cascades on purge, and the live namespace refuses to be purged. A run keeps its namespace for inspection and reconciliation unless asked to throw it away.
- **Result:** two runs of the same five records are two sets of creates, not a conflict. Zero live rows after every test. The reconciliation stage on Day 5 has a namespace to count.

### Retries are safe because writes carry their identifier

- **Decision:** the platform hashes the accepted payload. Same id, identical content: 200 and a no-op. Same id, different content: 409 listing the fields that differ. The runner retries 5xx, 408, 429, timeouts, transport failures and a 200 whose body is not a write result; it never retries a 409 or a 422, because those are the target's considered answer. The `timeout` fault lets the write land and then sleeps, so the retry gets `unchanged`, which is the id contract doing its job.
- **Result:** with every write forced to fail (`TARGET_FAULT_RATE=1.0`, `server_error`, two attempts), the run completes, every organization is recorded with status 500, the target's message and a request id, and every child is blocked rather than attempted. With the `malformed` fault the write lands, the runner reports it as failed, and the target's count shows it is there, which is exactly why a retry has to be a no-op.

### Children are not attempted when their organization did not land

- **Problem:** a limited run (first 120 organizations) sent 321 children at the target and got 321 honest 422s for organizations the run never wrote.
- **Decision:** organizations first, and a child whose organization is not in the namespace is recorded as blocked with the reason (refused, or never written by this run) and not sent. The same rule covers organizations the target refused in a full run.
- **Result:** the limited run reports 159 accepted and 321 blocked, every one with the organization that blocked it, and makes 321 fewer calls.

### Measured

- Transformation: 9,259 rows across four sources in 1.0 seconds. A validation pass, including storing every issue, 1.4 seconds.
- Validation on the committed sample (configuration `2026-10-01.1`, as of 2026-10-01): 7,163 valid, 2,021 blocked by errors, 75 skipped by rule 11, 308 valid records carrying a warning. By entity: organizations 889 of 1,030, contacts 1,702 of 2,279, subscriptions 414 of 1,003, activities 4,158 of 4,947.
- Rules applied: dormancy 469 accounts, dead trials 75, CA-from-region 35, Legacy Gold seat split 6. Rule 1 flagged no contact and reported 127 accounts.
- Full dry run, no limit: 7,163 sent, 7,163 accepted, 0 rejected, 0 failed, 0 blocked, 0 retries; the namespace holds exactly what the run says it accepted. 55 seconds in process, sequential, about 7 ms per write; 69 seconds in the compose stack with the runner reaching the target over the docker network, and validation 1.6 seconds there. Batching and parallel writes are on the list for the scaling pass, not before: a sequential run is reproducible, and reproducible is what a rehearsal is for.
- The entity table came back from the api in jsonb's order, not write order (postgres sorts jsonb keys by length). Fixed in the workbench and the cli; the run row is still jsonb because nothing else reads it in order.
- Workbench: the Dry run page was driven in headless Chromium against the live database (select the project, open the plan, validate, open the records, full dry run) with screenshots reviewed by eye. Two defects in the checker itself (the page's uppercase labels) and none in the page.
- 235 unit tests, 24 integration tests, 1 end-to-end test, all green without a model key. The end-to-end test creates a project, attaches three files and the billing feed, profiles, compares, proposes in manual mode, reviews every column (the reviewer supplies the identifiers the heuristic cannot see, through the same edit action the workbench uses), validates all 9,259 rows and rehearses a slice against the target.

## 2026-10-02, Day 5

### Reconciliation runs inside the dry run, not after it

- **Problem:** reconciliation as a separate stage, run later, would have to rebuild what the run knew: which records were valid, which were sent, which were accepted. The run row keeps counts, not identifiers, and re-transforming later describes today's mappings, not the run's.
- **Decision:** the runner reconciles as it finishes, before any purge, with everything it holds in memory: counts per stage, the first error of every excluded record, the accepted identifiers. The result goes to `reconciliation_results` with the ledger of accepted identifiers, so a later re-check can compare the target against what the run saw.
- **Alternatives:** a reconciliation endpoint that re-derives the expected set from the stored plan (re-runs the transform, and silently answers a different question when the mappings changed); counts only (cheaper, and blind to the case where one record is missing and another is unexpected, which leaves the counts equal).
- **Result:** reading back 7,163 identifiers takes nine requests and no measurable share of the 65-second run. The ledger is 98 KB of json, 25 KB as postgres stores it.

### Identifiers, not counts

- **Problem:** "the namespace holds 889 organizations and the run accepted 889" is the Day 4 check. It passes when one accepted record is missing and one refused record landed anyway.
- **Decision:** the accepted identifiers are compared with the identifiers the target holds, read back through the target's own api. Missing and unexpected records are separate discrepancies with sample identifiers; an unexpected record the run recorded as failed is called out as a write that landed and lost its answer.
- **Result:** the `malformed` fault with one attempt now fails reconciliation by name instead of passing a count check by accident. On Day 4 that test asserted `target_counts == 2` and `accepted == 0` side by side and nothing connected them.

### Not reached is counted, not derived

- **Problem:** "valid = sent + blocked + not reached" is a tautology if not-reached is computed as the difference.
- **Decision:** the runner counts records it never reached where it stops reaching them: past a `limit_per_entity`, after `stop_after_failures`, or an entity left out with `entities`. The check then compares three independent counts with a fourth.
- **Result:** a limited slice balances on its own terms. A unit test drops one not-reached record and the check names `valid_accounted`.

### Every exclusion has exactly one reason

- **Problem:** a record can carry several errors. Counting exclusions by issue type adds up to more than the records excluded (88 contacts with a short phone number, 83 of them as their first error; 45 repeated emails, 25 as the first), and a reconciliation that does not add up is worthless.
- **Decision:** the reason a record is excluded is its first error in pipeline order (conversion, then the contract, then the batch checks). Reconciliation checks that reasons sum to exclusions. The readiness report's work table counts records per problem, says plainly that a record with two problems appears twice, and points at the reconciliation for the once-each count.

### The status is decided by gates, and the gates are stated

- **Problem:** `READY WITH CONDITIONS` invites judgment calls. A status that depends on how somebody felt about the numbers cannot be argued with.
- **Decision:** `assess` is a pure function over stored facts. Blockers: undecided mappings or open questions, no full rehearsal, a rehearsal whose plan no longer equals today's, a reconciliation with discrepancies, any record the target refused or failed, an entity below 95% of its in-scope records landing. Conditions: anything left behind above the floor, any record with a warning. Records the customer's own rules set aside do not count against coverage. Every gate prints what it checked and the number it found.
- **Why:** the readiness report is the thing a customer pushes back on. Each line has to survive "why does this say blocked."
- **Alternatives:** a weighted score (a number with no meaning to a customer); asking the model for the status (the one rule forbids it, and it would be right most of the time, which is worse).
- **Result:** Apex is `BLOCKED`, with all four entities under the floor and subscriptions at 44.6%. That is the honest answer for this export, and the report says what changes it: 311 subscriptions in the dormancy conflict, 215 `Call` activities, 106 malformed emails, 97 organizations missing required values, and the rest in the table.

### A stale rehearsal is a blocker

- **Decision:** the report compares the dry run's stored plan with the plan the approved mappings and the configuration produce now. A reopened mapping or a new configuration version makes the rehearsal stale, and the report says which.
- **Why:** a readiness report about a migration that would no longer run that way is a report about something else.

### Issues become work with an owner

- **Decision:** every issue type maps to a kind (customer decision, customer data correction, released by other fixes, review and sign-off), a sentence, and for decisions the question to send. Values are quoted only where they are vocabulary (`'Call'`, `'Trial'`, `'Actve'`, `'CA'`); emails, phones, names and records are counted, never quoted. An issue type the catalog does not know still becomes work with a plain sentence rather than disappearing.
- **Result:** 27 work items, 7 customer questions, and the 839 records waiting on their accounts shown as one expectation instead of three blockers.

### The model drafts the summary, the code checks it

- **Problem:** the spec allows the model to draft the executive summary "grounded only in deterministic results." A sponsor reads the summary instead of the numbers, so an invented figure or a softened status there is the most expensive mistake the tool can make.
- **Decision:** the facts go to the model already formatted the way they may be quoted. `check_summary` refuses a draft whose headline does not state the status, that uses another status word, that contains any number not in the facts, or whose explanations do not cover exactly the codes asked for. Problems go back verbatim, then the summary falls back to one written by code, and the report says which and why.
- **Alternatives:** trust the model (no); let the model write only prose with placeholders the code fills (safe, and reads like a mail merge); skip the model (the template is serviceable, the model's explanations are better at saying why a record cannot go).
- **Result:** a property test holds the code-written summary to the same check, and it failed on the first run: the template quoted the number of decisions and corrections, which the model's facts did not carry. Fixed by adding the counts to the facts. The first real report needed three attempts: claude-opus-5 returned no blocker explanations twice because the codes were only inside the json. Naming them in the instruction fixed it; the next report passed on the first attempt in 16 seconds (4,823 tokens in, 1,255 out). The check covers numbers and status words, not every sentence of reasoning, and the README says so.

### GET answers even when nothing is stored

- **Decision:** `GET /reports/readiness` returns the latest stored report, or, with none, a preview computed on the spot with the template summary, marked `stored: false`, written nowhere.
- **Why:** the spec's endpoint is a GET, and a reviewer trying it should get the answer, not a 404 that says to POST first. Generating with the model and storing stays a POST because it has side effects and costs money.

### The first deep link after a restart shows a Streamlit dialog

- **Problem:** the browser pass found a "Page not found" dialog over the readiness page, then could not reproduce it.
- **Finding:** it appears on the first request to any page path after the workbench process starts, on every page, not only the new one. On a server's first script run Streamlit's page registry only knows the main script, so the runner sends `page_not_found` before `st.navigation` registers the pages; the right page renders behind the dialog and every later session is clean.
- **Decision:** recorded as a limitation, not patched around the framework. Opening the overview first avoids it.

### Measured

- Full dry run on the committed sample in the compose stack: 7,163 accepted, 65 seconds, reconciliation balanced, 28 of 28 checks.
- Readiness: `BLOCKED`, 4 blockers, 1 condition, 27 work items, 7 customer questions. claude-opus-5 summary: one attempt, 16 seconds.
- Workbench: the readiness page driven in headless Chromium against the live database (read the report, re-check the reconciliation, open the checks and the appendix, generate with the model off, then the overview card and the dry run page's reconciliation line), 18 of 18 checks, no page errors, screenshots reviewed by eye. Two fixes from the review: "1 conditions", and chips whose word spaces rendered too narrow to read.
- 288 unit tests, 56 integration tests, 1 end-to-end test, all green without a model key.
