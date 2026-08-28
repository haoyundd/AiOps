.PHONY: install test lint typecheck up down logs lab-config

install:
	uv sync --extra dev

test:
	uv run pytest --cov=app --cov-report=term-missing

lint:
	uv run ruff check app mcp_servers scripts tests

typecheck:
	uv run mypy app

up:
	docker compose -f docker-compose.incident.yml up -d --build

down:
	docker compose -f docker-compose.incident.yml down

logs:
	docker compose -f docker-compose.incident.yml logs -f api worker

lab-config:
	docker compose -f docker-compose.incident.yml config --quiet
