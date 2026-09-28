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

Planned: entity list and relationships land with the domain model.

## API

Planned: the endpoint table lands as endpoints are added.

## AI boundaries

See "The one rule" above. Details (prompt inputs, schema hashing, caching, retries, usage logging) land on Day 3.

## Human review

Planned for Day 3.

## Failure recovery

Planned for Day 4.

## Security considerations

Planned for Day 6: what is implemented versus what production would need.

## Scaling considerations

Planned for Day 6.
