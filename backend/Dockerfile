FROM ghcr.io/astral-sh/uv:0.9.7 AS uv
FROM node:24.22.3-bookworm-slim AS pi
WORKDIR /pi_runtime
COPY pi_runtime/package.json pi_runtime/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=pi /pi_runtime/node_modules ./pi_runtime/node_modules
COPY pi_runtime/runner.mjs ./pi_runtime/
RUN uv sync --frozen --no-dev && useradd --create-home arclight \
    && install -d -o arclight -g arclight /app/asset-objects \
    && install -d -m 0700 -o arclight -g arclight /app/.local/service-secrets
USER arclight
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "arclight.main:app", "--host", "0.0.0.0", "--port", "8000"]
