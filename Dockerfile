FROM python:3.13-slim

# uv for fast, reproducible installs. Pinned minor (dev machine runs 0.12.x)
# so the lockfile is interpreted the same way everywhere.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /usr/local/bin/

# ctranslate2 (pulled in by faster-whisper) links against libgomp, which the
# slim image does not ship — without it the kiosk import fails at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer from the lockfile only, so source edits don't re-resolve.
# --locked fails the build if uv.lock is stale rather than silently drifting.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --extra kiosk

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra kiosk

# Journal, artifacts and cassettes are bind-mounted from the host; create them
# so the app starts cleanly even without mounts.
RUN mkdir -p /app/journal /app/artifacts /app/cassettes

# Unbuffered so container logs appear immediately.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "vos.shell"]
