FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first, so code-only changes don't bust the layer cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY config.toml ./
COPY mcp_server ./mcp_server
RUN uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"
# Binding 0.0.0.0 inside the container is required for Docker's port publishing to
# reach it at all -- docker-compose.yml restricts the published port to the host's
# 127.0.0.1, preserving the "never exposed beyond 127.0.0.1" rule from outside.
ENV OPTIONGUARD_HOST=0.0.0.0

# One image, two entrypoints -- docker-compose.yml's `app` service uses the default
# CMD below; the `mcp` service overrides it with `python -m mcp_server.http_main`.
EXPOSE 8787 4101

CMD ["sh", "-c", "alembic upgrade head && exec optionguard serve"]
