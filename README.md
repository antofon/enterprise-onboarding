# Enterprise Onboarding

A customer implementation system for mapping, validating, and migrating enterprise data into a SaaS platform: source profiling, AI-assisted schema mapping with human review, deterministic transformation and validation, dry-run migration through the target API, reconciliation, and an implementation readiness report.

The scenario: a B2B SaaS company signed a new enterprise customer. That customer's data lives in a legacy CRM export, a billing system behind a REST API, and a business-rules document nobody has read in a year. Before go-live, all of it has to land in the platform's schema, and somebody has to answer *can this customer safely migrate, what has to change first, and what still needs the customer's input?*

This tool is what an implementation or forward-deployed engineer would use to answer that.

> The customer, the target platform, and every record are fictional and synthetic. This is a portfolio build, not a client engagement. Business impact numbers in the docs are labeled as scenario estimates.

## Status

Day 1 of a 7-day build. What is in place today:

- [x] Docker Compose brings up PostgreSQL, the FastAPI service, and the Streamlit workbench
- [x] structured JSON logging with request ids
- [x] one error envelope for every non-2xx response
- [x] onboarding project + source dataset model and API
- [x] documented target platform schema (organizations, contacts, subscriptions, activities): [target_platform/documentation](target_platform/documentation/README.md)
- [x] reproducible synthetic customer data generator with realistic defects (`enterprise-onboarding generate-data --rows 10000`)
- [ ] source data profiling and quality summary
- [ ] source vs target schema comparison
- [ ] AI-assisted semantic field mapping with confidence and rationale (Anthropic default, OpenAI behind the same interface)
- [ ] human mapping review: approve, reject, edit, ignore, ask the customer
- [ ] customer clarification questions
- [ ] deterministic transformation engine (no generated code is ever executed)
- [ ] validation against the target models, enums, relationships, and business rules
- [ ] dry-run migration through the target REST API with realistic failure modes
- [ ] reconciliation: source vs transformed vs accepted
- [ ] implementation readiness report (READY / READY WITH CONDITIONS / BLOCKED), Markdown + JSON
- [ ] works with no LLM key at all (manual mode)
- [ ] mapping accuracy eval against a golden set
- [ ] unit, integration, and end-to-end tests

Stretch, only after the above is solid: LangGraph (if the workflow earns it), RAG over the target docs, a second customer dataset, batching for large datasets, PDF export.

## Quick start

```bash
cp deploy/env.template .env
docker compose up --build
```

- API: http://localhost:8000
- API docs: http://localhost:8000/docs
- Workbench UI: http://localhost:8501
- PostgreSQL: internal to compose, also on 127.0.0.1:5433 for local tools

Generate a bigger customer than the committed sample (10,000 organizations, ~25,000 contacts):

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

## Stack

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL 16, Pandas, httpx, Typer, structlog, Streamlit, pytest, Docker Compose. The LLM sits behind a provider interface configured by environment variables; no provider or model name is hardcoded into the workflow.

## Layout

```
app/            the service: api, core (config, db, logging, errors), models, schemas, services, ai
ui/             streamlit workbench, talks to the api over http
scripts/        synthetic customer data generator
sample_customer/  the fictional customer's exports, business rules, kickoff notes
target_platform/  the fictional saas platform's schema and documentation
tests/          unit, integration, e2e
evals/          golden mapping set for measuring the ai mapping step
docs/           architecture, build log, case study, implementation plan
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, where AI is and is not allowed
- [docs/BUILD_LOG.md](docs/BUILD_LOG.md): dated decisions with the alternatives that lost

## License

MIT
