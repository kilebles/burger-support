FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN uv sync --no-dev --locked --no-install-project

COPY . .

RUN uv sync --no-dev --locked

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "app.main"]
