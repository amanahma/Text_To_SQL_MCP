# QueryGuard MCP server image.
#
# Python version comes from pyproject.toml (requires-python >=3.12,<3.13).
# Runs as a non-root user. No secrets and no .env are baked in — every secret
# (reader password, etc.) is supplied at runtime through the environment.
#
# The MCP server speaks over stdio (see src/queryguard/server.py), so it is
# launched per client connection, e.g.
#   docker compose -f docker-compose.demo.yml run --rm -T server python -m queryguard.server
# The default container command is a harmless keep-alive so `docker compose up`
# can hold the image ready; it does not start a listening daemon.

FROM python:3.12-slim

# No .pyc writing, unbuffered stdio (stderr logs appear immediately).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Install only the runtime dependencies + the package. The build needs
#    pyproject.toml, README.md (pyproject readme) and the source tree. Dev/eval
#    dependency-groups are NOT installed (pip install . takes [project] deps only).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 2) Copy the helper scripts and the demo database loader. These are used by the
#    demo compose (verify_setup, smoke_mcp) and are not part of the package.
COPY scripts/ ./scripts/
COPY db/ ./db/

# 3) Drop to a non-root user.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

# Keep-alive default command (the real MCP entrypoint is stdio, launched per
# connection via `python -m queryguard.server`).
CMD ["python", "-c", "import time; print('queryguard image ready; run: python -m queryguard.server (stdio)'); time.sleep(2147483647)"]
