FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY alembic.ini ./
RUN uv sync --frozen --no-dev && useradd --create-home arclight
USER arclight
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "arclight.main:app", "--host", "0.0.0.0", "--port", "8000"]
