FROM python:3.13-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first so source edits don't invalidate the install cache.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache .

# Journal and cassettes are bind-mounted from the host; create them so the
# app starts cleanly even on a fresh checkout.
RUN mkdir -p /app/journal /app/artifacts /app/cassettes

# Unbuffered so container logs appear immediately.
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "vos.shell"]
