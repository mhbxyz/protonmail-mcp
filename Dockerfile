# Base images are pinned by digest; Dependabot keeps them current.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# Installs exactly the hash-pinned versions recorded in uv.lock.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONUNBUFFERED=1 \
    HOME=/data \
    PATH=/opt/venv/bin:$PATH

COPY --from=build /opt/venv /opt/venv

RUN useradd --uid 10001 --home-dir /data --create-home app \
    && chown -R app:app /data

USER app

VOLUME ["/data"]

ENTRYPOINT ["protonmail-mcp"]
