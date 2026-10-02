.PHONY: up down logs ps seed migrate test lint typecheck fmt shell hooks

up:            ## build and start postgres, api, ui
	docker compose up --build -d

down:          ## stop everything (keeps the postgres volume)
	docker compose down

logs:          ## follow api + ui logs
	docker compose logs -f api ui

ps:
	docker compose ps

seed:          ## generate a 10k-org synthetic customer into generated/
	docker compose exec api enterprise-onboarding generate-data --rows 10000 --out generated/apex

migrate:       ## apply schema migrations (the api also does this at startup)
	docker compose exec api enterprise-onboarding migrate

test:          ## run the test suite locally (integration tests need `make up`)
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run mypy

fmt:
	uv run ruff format .

shell:
	docker compose exec api bash

hooks:         ## one-time per clone: pre-commit hook that blocks .env files and runs gitleaks
	git config core.hooksPath .githooks
