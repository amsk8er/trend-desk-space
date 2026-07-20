FROM node:22-slim AS frontend-build

RUN corepack enable
WORKDIR /build/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build


FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TREND_DESK_STATE_DIR=/app/state \
    TREND_DESK_DATA_DIR=/app/data \
    TREND_DAILY_SCHEDULER_ENABLED=false

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend ./backend
COPY prompts ./prompts
COPY --from=frontend-build /build/frontend/dist ./frontend/dist

RUN mkdir -p /app/state /app/data

EXPOSE 8848
CMD ["sh", "-c", ".venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8848}"]
