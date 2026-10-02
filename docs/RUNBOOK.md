# Runbook: diagnosing a failed onboarding

What to do when a profile, a validation pass, a dry run or a report goes wrong. Everything below works on the compose stack as it ships; the log lines are real ones from this system.

## 1. Get an id

Every error the api returns carries the id of the request that failed, in the body and in the `x-request-id` header:

```json
{"error": {"type": "internal_error", "message": "the server hit an unexpected error; quote the request id when reporting it", "details": {}, "request_id": "crash-1"}}
```

The workbench prints it next to the error. A validation pass or a dry run also has a run id: it is on the Dry run page, in the cli's output, and in `GET /api/v1/projects/{id}/migrations`.

## 2. Pull the lines

The api logs one json object per line. Every line logged while a request or a run was in progress carries its `request_id`, its `project_id`, and once a run exists, its `migration_run_id`.

```bash
# everything one request did
docker compose logs api --no-log-prefix | grep '"request_id": "crash-1"'

# everything one run did, as a table
docker compose logs api --no-log-prefix \
  | grep '"migration_run_id": "1ee9d428-5632-4ab8-9bc1-82acc3d17cc0"' \
  | jq -c '{timestamp, level, event, stage, entity, record_count, duration_ms, error_type}'
```

The cli writes the same lines to stderr, so its stdout stays the command's answer: `enterprise-onboarding dry-run latest 2> run.jsonl`.

## 3. Read the stages

Every pipeline step logs `stage_started`, then either `stage_finished` with `duration_ms` and `record_count`, or `stage_failed` with `error_type`. Steps nest: `dry_run` contains `transform`, `validate`, one `write` per entity and `reconcile`. The first `stage_failed` is where it broke. A `stage_failed` at `warning` level is an answer the system gives on purpose (a feed that is down, a project in the wrong stage); one at `error` level is a bug, and its traceback is in the line's `exception` field.

## 4. A worked example: the target went away

A dry run against a target that refuses connections, from the cli with `LOG_FORMAT=json`, trimmed to the fields that matter:

```json
{"level": "info",    "event": "stage_finished", "stage": "transform", "record_count": 3309, "duration_ms": 472.0}
{"level": "info",    "event": "stage_finished", "stage": "validate", "record_count": 3309, "duration_ms": 198.7}
{"level": "info",    "event": "stage_started", "stage": "write", "entity": "organization"}
{"level": "error",   "event": "dry_run_breaker_open", "stage": "write", "entity": "organization", "error_type": "transport_error", "consecutive_failed": 10, "not_attempted": 879}
{"level": "info",    "event": "stage_finished", "stage": "write", "entity": "organization", "record_count": 10, "duration_ms": 7523.7, "not_attempted": 879}
{"level": "info",    "event": "stage_finished", "stage": "write", "entity": "contact", "record_count": 0, "not_attempted": 1702}
{"level": "warning", "event": "target_ids_unavailable", "stage": "dry_run", "entity": "organization"}
{"level": "info",    "event": "stage_finished", "stage": "reconcile", "status": "discrepancies", "failed_checks": 2}
{"level": "info",    "event": "stage_finished", "stage": "dry_run", "record_count": 10, "duration_ms": 8530.2, "not_attempted": 2581, "status": "failed"}
```

Read top to bottom: the data was fine (transform and validate finished), the first ten organizations failed every retry with `transport_error`, the breaker stopped the run, nothing else was sent, and reconciliation could not read the target back. The target is down, not the data. Fix the target, rehearse again. Nothing has to be cleaned up: the next run gets a namespace of its own.

## 5. What the events mean

| event | where | what it means | what to do |
|---|---|---|---|
| `stage_failed` with `source_unavailable` | profile, transform | a file is missing or a feed kept failing after its retries | check the location or the feed's url and token; the error names the page and the attempts |
| `source_retry` | profile, transform | the feed rate-limited or blipped and the loader waited it out | nothing, unless the profile then fails |
| `dry_run_breaker_open` | write | the target failed `TARGET_BREAKER_THRESHOLD` records in a row | the run is `failed` with the reason; rehearse again once the target answers |
| `target_fault` | target api | fault injection is on (`TARGET_FAULT_RATE`) | turn it off unless a failure rehearsal is the point |
| `run_marked_interrupted` | startup, a project's next run | a run's process stopped before it finished | rehearse again; whatever landed is still in that run's namespace |
| `target_ids_unavailable`, `target_counts_unavailable` | reconcile | the target could not be read back | re-check later with `POST .../migrations/{run_id}/reconcile` |
| `staging_purge_failed` | dry run | the staging namespace could not be deleted | `DELETE /target/v1/namespaces/{namespace}`, also named on the run |
| `database_unavailable` | any request | postgres did not answer; the api returned 503 | `docker compose ps db`, then the db container's logs |
| `unhandled_error` | any request | a bug; the api returned 500 with the request id | the `exception` field has the traceback |
| `readiness_summary_rejected`, `readiness_summary_failed` | readiness | the model's draft failed the fact check, or the model was not reachable | nothing to fix: the report used the code-written summary and says why |
| `stage_kept` | any stage | the work was recorded but the project's stage did not move | expected when re-running an earlier stage |

## 6. Record by record

The logs say what happened to a run. Which records were refused, and why, lives in the database, where the api serves it:

- `GET .../migrations/{run_id}/failures`: every record the target refused or the run did not attempt, with the http status, the target's message and the request id the runner sent (`dryrun-<run>-<record id>`), which is also the request id on the target's own log line for that write.
- `GET .../migrations/{run_id}/issues`: every validation issue, with the dataset, the source row, the column and the offending value.
- `GET .../migrations/{run_id}/reconciliation`: every check, and sample identifiers for anything missing or unexpected.
