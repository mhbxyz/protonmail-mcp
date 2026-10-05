FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --uid 10001 --home-dir /data --create-home app \
    && chown -R app:app /data

USER app

VOLUME ["/data"]

ENTRYPOINT ["protonmail-mcp"]
