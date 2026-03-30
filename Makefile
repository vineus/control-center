.PHONY: run dev format lint test

run:
	uv run control-center

dev:
	uv run uvicorn control_center.main:app --reload --host 0.0.0.0 --port 8000

format:
	uv run ruff format src/
	uv run ruff check --fix src/

lint:
	uv run ruff check src/

test:
	uv run pytest tests/ -v
