.PHONY: format lint test check

format:
	uv run ruff format .

lint:
	uv run ruff check --fix .

test:
	uv run pytest

check: lint test
