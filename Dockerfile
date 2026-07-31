FROM node:22-slim AS frontend-build

RUN corepack enable && corepack prepare pnpm@11.5.0 --activate
WORKDIR /build/frontend
COPY trend-desk-app/frontend/package.json \
     trend-desk-app/frontend/pnpm-lock.yaml \
     trend-desk-app/frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY trend-desk-app/frontend/ ./
RUN pnpm exec tsc -b && pnpm exec vite build --base=/trend-desk/


FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/

WORKDIR /app/trend-desk
COPY trend-desk-app/pyproject.toml trend-desk-app/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY trend-desk-app/backend ./backend
COPY trend-desk-app/prompts ./prompts
COPY --from=frontend-build /build/frontend/dist ./frontend/dist

WORKDIR /app/sensory-vocabulary-lab
COPY sensory-vocabulary-lab ./
RUN mkdir -p assets/generated

WORKDIR /app/portal
COPY index.html ./
COPY deployment-manifest.json ./
COPY bracelet ./bracelet
COPY bridge-blocker ./bridge-blocker
COPY cindyzhang ./cindyzhang
COPY health ./health
COPY jinchun ./jinchun
COPY jinduo ./jinduo
COPY kids ./kids
COPY neican ./neican
COPY research ./research
COPY zhuzhu ./zhuzhu

RUN mkdir -p /app/state /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TREND_DESK_STATE_DIR=/app/state \
    TREND_DESK_DATA_DIR=/app/data \
    TREND_DESK_PORTAL_DIR=/app/portal \
    TREND_DAILY_SCHEDULER_ENABLED=false

WORKDIR /app/trend-desk
EXPOSE 8000
CMD ["sh", "-c", ".venv/bin/uvicorn backend.portal_app:app --host 0.0.0.0 --port ${PORT:-8000}"]
