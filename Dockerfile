# Base images are pinned by digest; Dependabot keeps them current.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build

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

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONUNBUFFERED=1 \
    HOME=/data \
    PATH=/opt/venv/bin:$PATH

COPY --from=build /opt/venv /opt/venv

RUN useradd --uid 10001 --home-dir /data --create-home app \
    && chown -R app:app /data

USER app

VOLUME ["/data"]

ENTRYPOINT ["protonmail-mcp"]
